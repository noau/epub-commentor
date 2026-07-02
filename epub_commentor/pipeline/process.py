"""Drive the two-stage annotation pipeline for an extracted chapter list.

The orchestrator takes the chapters produced by
:func:`~epub_commentor.pipeline.extract.extract_chapters`, runs Stage 1
(full-chapter scan) per chapter, then Stage 2 (per-block annotation) with
intra-chapter parallelism via :class:`concurrent.futures.ThreadPoolExecutor`.

Stage 1 is intentionally **sequential across chapters** — the PRD notes that
v1 avoids the cross-chapter dependency bookkeeping that would let Stage 2 of
chapter N race ahead of Stage 1 of chapter N+1. Inside a single chapter,
Stage 2 runs blocks concurrently because they are independent of each other.
"""

import hashlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CommentConfig
from ..errors import (
    CommentAbortError,
    CommentInvalidJSONError,
    CommentNoParagraphsError,
    CommentScanFailedError,
)
from ..llm.block import annotate_block
from ..llm.memo import scan_chapter
from ..llm.protocol import LLMProtocol
from ..llm.schema import ChapterMemo, CommentItem
from ..progress import ProgressCallback, ProgressEvent
from .extract import Chapter

_logger = logging.getLogger(__name__)

_RESERVED_METADATA_KEYS = frozenset({"__opf_path__"})

# Filter callback invoked between Stage 2 and the injection layer. Receives
# the per-chapter ``ChapterAnnotation`` list and returns a parallel
# ``list[bool]`` mask — ``True`` keeps the annotation, ``False`` drops it.
# ``None`` (the default) skips the gate entirely. Mirrors the symmetric
# :data:`ChapterFilter` defined in :mod:`epub_commentor.pipeline.extract`
# so the pre-process (which-chapters-to-generate) and post-process
# (which-annotations-to-inject) gates are uniform in shape.
AnnotationFilter = Callable[[list["ChapterAnnotation"]], list[bool]]


@dataclass
class ChapterAnnotation:
    """All annotation outputs for a single chapter.

    ``comments`` is sorted by their position in the chapter so the inject
    layer can walk it in order without re-sorting.

    ``skipped_blocks`` counts Stage 2 blocks where JSON validation failed
    after exhausting retries and were skipped (only populated when
    ``config.fail_on_block_error`` is False). ``has_empty_blocks`` counts
    Stage 2 blocks where the LLM returned a valid but empty response
    (``{"comments": []}``) — useful to surface in the post-process
    review gate so the user can decide whether to inject the chapter or
    regenerate it. Both default to 0 for chapters that bypassed Stage 2
    entirely (zero ``<p>``, scan failure).
    """

    chapter: Chapter
    memo: ChapterMemo
    comments: list[CommentItem] = field(default_factory=list)
    skipped_blocks: int = 0
    has_empty_blocks: int = 0


def _empty_memo() -> ChapterMemo:
    """A sentinel :class:`ChapterMemo` used when a chapter is skipped.

    The values are intentionally minimal but still satisfy the pydantic
    constraints (``outline`` must have 3..7 items). The placeholder text
    makes it obvious in logs that the chapter was skipped.
    """
    return ChapterMemo(
        core_thesis="(chapter skipped — no <p> elements)",
        outline=["(skipped)", "(skipped)", "(skipped)"],
        tone="(unknown)",
        target_audience="(unknown)",
    )


def _chapter_hash(chapter_path: Path) -> str:
    return hashlib.sha1(chapter_path.as_posix().encode("utf-8")).hexdigest()[:8]


def _split_blocks(chapter: Chapter, block_size: int) -> list[tuple[int, list]]:
    """Split the chapter's paragraphs into consecutive ``block_size`` chunks.

    Returns a list of ``(start_index, paragraphs)`` so callers can recover
    the absolute paragraph offset (used as a cache seed component and for
    stable logging).
    """
    paragraphs = list(chapter.body.iter("p"))
    blocks: list[tuple[int, list]] = []
    for start in range(0, len(paragraphs), block_size):
        blocks.append((start, paragraphs[start : start + block_size]))
    return blocks


def _emit(
    callback: ProgressCallback | None,
    event: ProgressEvent,
) -> None:
    """Mirror the commentor-level helper so this module is self-contained."""
    if callback is None:
        return
    try:
        callback(event)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("progress callback raised %s: %s", type(exc).__name__, exc)


def _process_chapter(
    chapter: Chapter,
    book_metadata: dict[str, str],
    llm: LLMProtocol,
    config: CommentConfig,
    progress_callback: ProgressCallback | None = None,
) -> tuple[ChapterAnnotation, int]:
    """Stage 1 + Stage 2 for a single chapter (sequential per chapter).

    A chapter with zero ``<p>`` elements (e.g. a cover page, a list-only
    table of contents, an image-only page) is silently skipped: a warning
    is logged and an empty :class:`ChapterAnnotation` is returned. The
    caller's :func:`process_chapters` accumulates these without raising.
    Set ``config.fail_on_empty_chapter=True`` to make this raise
    :class:`~epub_commentor.errors.CommentNoParagraphsError` instead.

    Returns ``(annotation, blocks_skipped)`` where ``blocks_skipped`` is
    the number of Stage-2 blocks that failed JSON validation after
    exhausting retries and were skipped (counted only when
    ``config.fail_on_block_error`` is False; otherwise the corresponding
    exception propagates). Stage 1 scan failures count as 0 here — they
    produce a sentinel placeholder annotation and are reported via
    ``chapters_skipped`` instead.

    ``annotation.has_empty_blocks`` is set to the number of Stage 2
    blocks that returned a valid but empty ``{"comments": []}`` response
    (distinct from the JSON-validation-failure count tracked in
    ``annotation.skipped_blocks``). The post-process review gate uses
    both metrics to decide whether to surface the chapter to the user.
    """
    paragraphs = list(chapter.body.iter("p"))
    if not paragraphs:
        msg = f"chapter has zero <p> elements, skipping: {chapter.path.as_posix()}"
        if getattr(config, "fail_on_empty_chapter", False):
            raise CommentNoParagraphsError(msg)
        _logger.warning(msg)
        _emit(
            progress_callback,
            ProgressEvent(
                stage="warn",
                current=0,
                total=0,
                message=f"Chapter {chapter.title}: no <p> elements → skipped",
            ),
        )
        return ChapterAnnotation(chapter=chapter, memo=_empty_memo(), comments=[]), 0

    prompt_metadata = {k: v for k, v in book_metadata.items() if k not in _RESERVED_METADATA_KEYS}
    try:
        memo = scan_chapter(
            body=chapter.body,
            chapter_path=chapter.path,
            chapter_title=chapter.title,
            book_metadata=prompt_metadata,
            llm=llm,
            config=config,
        )
    except CommentScanFailedError as exc:
        msg = (
            f"Chapter `{chapter.path.as_posix()}` Stage 1 scan failed"
            f"(fail_on_block_error=False → skipping chapter): {exc}"
        )
        if getattr(config, "fail_on_block_error", False):
            _logger.error(msg)
            raise
        _logger.warning(msg)
        _emit(
            progress_callback,
            ProgressEvent(
                stage="warn",
                current=0,
                total=0,
                message=f"Chapter {chapter.title}: scan failed → skipped ({type(exc).__name__}: {exc})",
            ),
        )
        # Reuse the empty-chapter placeholder so the chapter counts in
        # chapters_skipped via the existing _is_chapter_skipped prefix.
        return ChapterAnnotation(chapter=chapter, memo=_empty_memo(), comments=[]), 0

    blocks = _split_blocks(chapter, config.block_size)
    chapter_hash = _chapter_hash(chapter.path)

    block_count = len(blocks)
    processed_block = 0
    blocks_skipped = 0
    has_empty_blocks = 0
    chapter_tainted = False  # any block failed or returned empty
    _emit(
        progress_callback,
        ProgressEvent(stage="process", substage="annotate", current=0, total=max(block_count, 1)),
    )

    comments: list[CommentItem] = []
    if not blocks:
        return (
            ChapterAnnotation(chapter=chapter, memo=memo, comments=comments),
            0,
        )

    fail_block_error = getattr(config, "fail_on_block_error", False)
    skip_on_empty = getattr(config, "skip_chapter_on_empty_annotation", False)

    with ThreadPoolExecutor(max_workers=config.concurrency) as executor:
        futures = {
            executor.submit(
                annotate_block,
                block_ps=block_ps,
                block_start_idx=start_idx,
                chapter_hash=chapter_hash,
                memo=memo,
                llm=llm,
                config=config,
            ): start_idx
            for start_idx, block_ps in blocks
        }
        for future in as_completed(futures):
            block_start = futures[future]
            block_succeeded = True
            try:
                block_comments = future.result()
            except CommentAbortError:
                # First aborted future: cancel any not-yet-started work
                # so the executor doesn't drag in more blocks, then let
                # in-flight workers bail out via the abort flag (each
                # worker's ``for chunk in stream`` checks the flag and
                # closes the httpx stream). ``wait=False`` returns
                # immediately; the ``with`` block's ``__exit__`` will
                # then ``wait=True`` join the (already-exiting) workers.
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            except CommentInvalidJSONError as exc:
                msg = (
                    f"Stage 2 annotate failed for block starting at p_id {block_start} "
                    f"in {chapter.path.as_posix()} after retries "
                    f"(fail_on_block_error={fail_block_error}): {exc}"
                )
                if fail_block_error:
                    _logger.error(msg)
                    raise
                _logger.warning(msg)
                _emit(
                    progress_callback,
                    ProgressEvent(
                        stage="warn",
                        current=0,
                        total=0,
                        message=(
                            f"Chapter {chapter.title} block @ p_id {block_start} → skipped "
                            f"(retries exhausted: {type(exc).__name__})"
                        ),
                    ),
                )
                blocks_skipped += 1
                chapter_tainted = True
                block_succeeded = False
                block_comments = []

            if block_succeeded and not block_comments:
                # Successful call but the LLM produced zero comments —
                # also counts as tainted when skip_chapter_on_empty_annotation
                # is set, and surfaces as has_empty_blocks for the review gate.
                chapter_tainted = True
                has_empty_blocks += 1

            processed_block += 1
            _emit(
                progress_callback,
                ProgressEvent(
                    stage="process",
                    substage="annotate",
                    current=processed_block,
                    total=block_count,
                ),
            )

            # Translate block-local p_ids to absolute paragraph indices so
            # downstream injection can map them via body.iter("p") directly.
            # Validation in annotate_block already passed on block-local values.
            for c in block_comments:
                c.target_p_ids = [pid + block_start for pid in c.target_p_ids]
            comments.extend(block_comments)

    # Sort by first target_p_id then by position so inject.py can walk in order.
    comments.sort(key=lambda c: (c.target_p_ids[0], c.position.value))

    annotation = ChapterAnnotation(
        chapter=chapter,
        memo=memo,
        comments=comments,
        skipped_blocks=blocks_skipped,
        has_empty_blocks=has_empty_blocks,
    )

    # When the user opted into skip_chapter_on_empty_annotation, any block
    # failure or empty result taints the whole chapter so it shows up in
    # chapters_skipped and can be re-selected via the chapter filter for retry.
    if chapter_tainted and skip_on_empty:
        _logger.warning(
            "chapter tainted by block failures / empty results, "
            "skip_chapter_on_empty_annotation=True → marking chapter as skipped: %s",
            chapter.path.as_posix(),
        )
        _emit(
            progress_callback,
            ProgressEvent(
                stage="warn",
                current=0,
                total=0,
                message=f"Chapter {chapter.title}: chapter tainted → skipped",
            ),
        )
        # Replace memo with the placeholder so _is_chapter_skipped picks it up,
        # but preserve the per-block metrics so the review gate can still show them.
        annotation.memo = _empty_memo()
        annotation.comments = []
        return annotation, blocks_skipped

    return annotation, blocks_skipped


def process_chapters(
    chapters: list[Chapter],
    book_metadata: dict[str, str],
    llm: LLMProtocol,
    config: CommentConfig,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[ChapterAnnotation], int]:
    """Run Stage 1 + Stage 2 across all chapters.

    Chapters are processed sequentially to keep Stage 1 simple and avoid
    Stage 1/Stage 2 inter-chapter races. Within a chapter, blocks run
    concurrently via :class:`ThreadPoolExecutor`.

    When ``progress_callback`` is supplied, two event flavours are
    emitted per chapter: a ``process / scan`` event (chapter
    scan completion) and one ``process / annotate`` event per
    block, plus a synthetic 0-of-N event right after splitting.

    Returns ``(annotations, blocks_skipped)`` where ``blocks_skipped`` is
    the aggregate count of Stage-2 blocks that failed after exhausting
    retries and were skipped (only counted when
    ``config.fail_on_block_error`` is False; otherwise the relevant
    exception propagates out of this call). Stage 1 scan failures are
    not counted here — they manifest as empty annotations and are
    reported downstream via ``chapters_skipped``.
    """
    chapter_count = len(chapters)
    processing = 0

    annotations: list[ChapterAnnotation] = []
    blocks_skipped_total = 0
    for chapter in chapters:
        processing += 1
        _emit(
            progress_callback,
            ProgressEvent(
                stage="process",
                substage="scan",
                current=processing,
                total=chapter_count,
                message=chapter.title,
            ),
        )

        annotation, blocks_skipped = _process_chapter(
            chapter=chapter,
            book_metadata=book_metadata,
            llm=llm,
            config=config,
            progress_callback=progress_callback,
        )
        annotations.append(annotation)
        blocks_skipped_total += blocks_skipped
    return annotations, blocks_skipped_total


__all__ = ["AnnotationFilter", "ChapterAnnotation", "process_chapters"]
