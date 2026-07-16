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
    CommentTranslationFailedError,
    CommentTranslationInvalidJSONError,
)
from ..llm.block import annotate_block
from ..llm.memo import scan_chapter
from ..llm.protocol import LLMProtocol
from ..llm.schema import ChapterMemo, CommentItem
from ..llm.translate import translate_block
from ..progress import ProgressCallback, ProgressEvent
from .extract import Chapter

_logger = logging.getLogger(__name__)

_RESERVED_METADATA_KEYS = frozenset({"__opf_path__"})

# Filter callback invoked between Stage 2 and the injection layer. Receives
# the per-chapter ``ChapterAnnotation`` list plus the book's OPF metadata
# (the same dict Stage 1 sees, sans reserved ``__opf_path__``) and returns
# a parallel ``list[bool]`` mask — ``True`` keeps the annotation,
# ``False`` drops it. ``None`` (the default) skips the gate entirely.
# Mirrors the symmetric :data:`ChapterFilter` defined in
# :mod:`epub_commentor.pipeline.extract` so the pre-process
# (which-chapters-to-generate) and post-process
# (which-annotations-to-inject) gates are uniform in shape.
#
# The second ``book_metadata`` parameter is ignored by the simple user-
# driven ``--review`` picker and consumed by AI-driven ``--ai-review``
# filters that need book-level context. Custom user-supplied lambdas must
# accept it: ``lambda anns, _md: [...]``.
AnnotationFilter = Callable[[list["ChapterAnnotation"], dict[str, str]], list[bool]]


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

    ``translations`` and ``translation_blocks_skipped`` are populated by
    :func:`translate_chapters` (Stage 3) when ``config.enable_translation``
    is ``True``. ``translations`` is sorted by absolute ``p_id`` so the
    injection layer can walk it in order; ``translation_blocks_skipped``
    mirrors ``skipped_blocks`` for the Stage 3 soft-skip policy. When
    Stage 3 is disabled both default to ``[]`` / ``0`` — injection is a
    no-op for them.
    """

    chapter: Chapter
    memo: ChapterMemo
    comments: list[CommentItem] = field(default_factory=list)
    skipped_blocks: int = 0
    has_empty_blocks: int = 0
    translations: list = field(default_factory=list)
    translation_blocks_skipped: int = 0


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
        # Soft-skip notification. Surfaces through the project logger
        # (which the CLI's ``setup_root_logger`` configures); rich bar
        # users also see it via ``--stream-logs``. Deduped from the old
        # ``ProgressEvent(stage="warn")`` path so we have one source of
        # truth and --quiet truly silences everything.
        _logger.warning(msg)
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
            f"Chapter `{chapter.path.as_posix()}` Stage 1 scan failed "
            f"({type(exc).__name__}: {exc}) "
            f"(fail_on_block_error=False → skipping chapter)"
        )
        if getattr(config, "fail_on_block_error", False):
            _logger.error(msg)
            raise
        # Soft-skip notification; see empty-chapter branch above for the
        # single-channel rationale (logger only, no ProgressEvent).
        _logger.warning(msg)
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
                    f"({type(exc).__name__}: {exc}) "
                    f"(fail_on_block_error={fail_block_error})"
                )
                if fail_block_error:
                    _logger.error(msg)
                    raise
                # Soft-skip notification; see empty-chapter branch above
                # for the single-channel rationale.
                _logger.warning(msg)
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
        # Replace memo with the placeholder so _is_chapter_skipped picks it up,
        # but preserve the per-block metrics so the review gate can still show them.
        annotation.memo = _empty_memo()
        annotation.comments = []
        return annotation, blocks_skipped

    return annotation, blocks_skipped


def _is_chapter_skipped(annotation: ChapterAnnotation) -> bool:
    """True if Stage 1 produced the placeholder memo (zero ``<p>`` / scan failure).

    Mirrors the ``(chapter skipped`` prefix used by the CLI summary panel
    so :func:`translate_chapters` can skip the same chapters the
    downstream ``chapters_skipped`` counter would skip. Returning ``True``
    means this chapter has nothing to translate (its body has zero
    paragraphs, by construction).
    """
    return annotation.memo.core_thesis.startswith("(chapter skipped")


def translate_chapters(
    annotations: list[ChapterAnnotation],
    llm: LLMProtocol,
    config: CommentConfig,
    progress_callback: ProgressCallback | None = None,
) -> int:
    """Stage 3 — translate every paragraph in every chapter's body.

    Runs AFTER Stage 2 + the annotation review gate so dropped chapters
    never reach translation. Per-chapter translation uses the same
    :func:`_split_blocks` block size as Stage 2; the same
    :class:`ThreadPoolExecutor` concurrency, rate-limiter and cache-seed
    machinery is reused via :func:`epub_commentor.llm.translate.translate_block`.

    Translation failures follow the same dual policy Stage 2 uses:

    - ``config.fail_on_translation_error=True`` raises
      :class:`~epub_commentor.errors.CommentTranslationFailedError`
      from the offending block.
    - default (``False``) logs a warning, drops the failed block,
      and increments ``ChapterAnnotation.translation_blocks_skipped``;
      the rest of the chapter's translations still get injected.

    Returns ``total_translation_blocks_skipped`` — the aggregate count
    of Stage 3 blocks that were skipped. The caller
    (:func:`epub_commentor.commentor.comment_epub`) populates
    :attr:`CommentorResult.translation_blocks_skipped` from this.

    The translation language is always :attr:`CommentConfig.target_language`
    so commentary and translation stay in lockstep.
    """
    if not config.enable_translation:
        return 0

    chapter_count = len(annotations)
    processing = 0
    total_skipped = 0
    fail_block_error = getattr(config, "fail_on_translation_error", False)

    for annotation in annotations:
        processing += 1
        chapter = annotation.chapter
        title = chapter.title

        # Skip-chapter cascade: zero-paragraph chapters, Stage 1 scan
        # failures, and chapters already soft-skipped via
        # ``skip_chapter_on_empty_annotation`` all carry the placeholder
        # memo and have nothing to translate.
        paragraphs = list(chapter.body.iter("p"))
        if not paragraphs or _is_chapter_skipped(annotation):
            continue

        blocks = _split_blocks(chapter, config.block_size)
        chapter_hash = _chapter_hash(chapter.path)
        block_count = len(blocks)
        processed_block = 0

        _emit(
            progress_callback,
            ProgressEvent(
                stage="process",
                substage="translate",
                current=processing,
                total=chapter_count,
                message=title,
            ),
        )
        _emit(
            progress_callback,
            ProgressEvent(
                stage="process",
                substage="translate",
                current=0,
                total=max(block_count, 1),
            ),
        )

        translations: list = []
        skipped = 0

        if not blocks:
            annotation.translations = translations
            annotation.translation_blocks_skipped = skipped
            total_skipped += skipped
            continue

        with ThreadPoolExecutor(max_workers=config.concurrency) as executor:
            futures = {
                executor.submit(
                    translate_block,
                    block_ps=block_ps,
                    block_start_idx=start_idx,
                    chapter_hash=chapter_hash,
                    llm=llm,
                    config=config,
                ): start_idx
                for start_idx, block_ps in blocks
            }
            for future in as_completed(futures):
                block_start = futures[future]
                try:
                    block_translations = future.result()
                except CommentAbortError:
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except CommentTranslationInvalidJSONError as exc:
                    msg = (
                        f"Stage 3 translate failed for block starting at p_id {block_start} "
                        f"in {chapter.path.as_posix()} after retries "
                        f"({type(exc).__name__}: {exc}) "
                        f"(fail_on_translation_error={fail_block_error})"
                    )
                    if fail_block_error:
                        _logger.error(msg)
                        raise CommentTranslationFailedError(
                            f"Stage 3 translation failed: {exc}"
                        ) from exc
                    _logger.warning(msg)
                    skipped += 1
                    block_translations = []

                # Shift block-local p_ids to absolute paragraph indices
                # so inject_chapter can map them via body.iter("p") directly.
                for tr in block_translations:
                    tr.p_id = tr.p_id + block_start
                translations.extend(block_translations)

                processed_block += 1
                _emit(
                    progress_callback,
                    ProgressEvent(
                        stage="process",
                        substage="translate",
                        current=processed_block,
                        total=block_count,
                    ),
                )

        # Sort by absolute p_id so the injection layer can walk in order.
        translations.sort(key=lambda tr: tr.p_id)
        annotation.translations = translations
        annotation.translation_blocks_skipped = skipped
        total_skipped += skipped

    return total_skipped


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


__all__ = ["AnnotationFilter", "ChapterAnnotation", "process_chapters", "translate_chapters"]
