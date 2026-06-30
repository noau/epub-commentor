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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CommentConfig
from ..errors import CommentNoParagraphsError
from ..llm.block import annotate_block
from ..llm.core import LLM
from ..llm.memo import scan_chapter
from ..llm.schema import ChapterMemo, CommentItem
from .extract import Chapter

_logger = logging.getLogger(__name__)

_RESERVED_METADATA_KEYS = frozenset({"__opf_path__"})


@dataclass
class ChapterAnnotation:
    """All annotation outputs for a single chapter.

    ``comments`` is sorted by their position in the chapter so the inject
    layer can walk it in order without re-sorting.
    """

    chapter: Chapter
    memo: ChapterMemo
    comments: list[CommentItem] = field(default_factory=list)


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


def _process_chapter(
    chapter: Chapter,
    book_metadata: dict[str, str],
    llm: LLM,
    config: CommentConfig,
) -> ChapterAnnotation:
    """Stage 1 + Stage 2 for a single chapter (sequential per chapter).

    A chapter with zero ``<p>`` elements (e.g. a cover page, a list-only
    table of contents, an image-only page) is silently skipped: a warning
    is logged and an empty :class:`ChapterAnnotation` is returned. The
    caller's :func:`process_chapters` accumulates these without raising.
    Set ``config.fail_on_empty_chapter=True`` to make this raise
    :class:`~epub_commentor.errors.CommentNoParagraphsError` instead.
    """
    paragraphs = list(chapter.body.iter("p"))
    if not paragraphs:
        msg = f"chapter has zero <p> elements, skipping: {chapter.path.as_posix()}"
        if getattr(config, "fail_on_empty_chapter", False):
            raise CommentNoParagraphsError(msg)
        _logger.warning(msg)
        return ChapterAnnotation(chapter=chapter, memo=_empty_memo(), comments=[])

    prompt_metadata = {k: v for k, v in book_metadata.items() if k not in _RESERVED_METADATA_KEYS}
    memo = scan_chapter(
        body=chapter.body,
        chapter_path=chapter.path,
        chapter_title=chapter.title,
        book_metadata=prompt_metadata,
        llm=llm,
        config=config,
    )

    blocks = _split_blocks(chapter, config.block_size)
    chapter_hash = _chapter_hash(chapter.path)

    comments: list[CommentItem] = []
    if not blocks:
        return ChapterAnnotation(chapter=chapter, memo=memo, comments=comments)

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
            block_comments = future.result()
            # Translate block-local p_ids to absolute paragraph indices so
            # downstream injection can map them via body.iter("p") directly.
            # Validation in annotate_block already passed on block-local values.
            block_start = futures[future]
            for c in block_comments:
                c.target_p_ids = [pid + block_start for pid in c.target_p_ids]
            comments.extend(block_comments)

    # Sort by first target_p_id then by position so inject.py can walk in order.
    comments.sort(key=lambda c: (c.target_p_ids[0], c.position.value))
    return ChapterAnnotation(chapter=chapter, memo=memo, comments=comments)


def process_chapters(
    chapters: list[Chapter],
    book_metadata: dict[str, str],
    llm: LLM,
    config: CommentConfig,
) -> list[ChapterAnnotation]:
    """Run Stage 1 + Stage 2 across all chapters.

    Chapters are processed sequentially to keep Stage 1 simple and avoid
    Stage 1/Stage 2 inter-chapter races. Within a chapter, blocks run
    concurrently via :class:`ThreadPoolExecutor`.
    """
    annotations: list[ChapterAnnotation] = []
    for chapter in chapters:
        annotations.append(
            _process_chapter(
                chapter=chapter,
                book_metadata=book_metadata,
                llm=llm,
                config=config,
            )
        )
    return annotations


__all__ = ["ChapterAnnotation", "process_chapters"]
