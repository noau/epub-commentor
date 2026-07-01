"""Pydantic models and validators for the two-stage annotation pipeline.

This module defines the JSON contract between the LLM and the pipeline:

- :class:`ChapterMemo` is produced by Stage 1 (full-chapter scan).
- :class:`BlockAnnotation` is produced by Stage 2 (per-block annotation); it
  contains a list of :class:`CommentItem`, each pinning a contiguous range of
  paragraphs inside the block.

Stage 2 also relies on :func:`validate_block_annotations` to enforce structural
invariants that the pydantic model alone cannot express (in-block overlaps,
contiguous p_id ranges, p_id falling inside the block).
"""

from enum import Enum

from pydantic import BaseModel, Field

from .. import errors as _errors


class KeyTerm(BaseModel):
    """Single term/glossary pair returned by Stage 1."""

    term: str = Field(..., min_length=1, max_length=200)
    gloss: str = Field(..., min_length=1, max_length=1000)


class ChapterMemo(BaseModel):
    """Stage 1 output: a compact summary used to guide Stage 2.

    In addition to the reader-facing summary fields (``core_thesis``,
    ``outline`` ...), three **internal-hint** fields carry private working
    notes for Stage 2 — they are never rendered into the final EPUB:

    - ``motifs`` — recurring images / symbols / ideas worth tracking.
    - ``foreshadowing`` — earlier beats that pay off later.
    - ``interpretive_warnings`` — places where a careless reader will misread.
    """

    core_thesis: str = Field(...)
    outline: list[str] = Field(...)
    key_terms: list[KeyTerm] = Field(default_factory=list)
    tone: str = Field(...)
    target_audience: str = Field(...)
    reading_anchors: list[str] = Field(default_factory=list)
    # NEW — internal hints for Stage 2, never rendered to the final reader
    motifs: list[str] = Field(default_factory=list)
    foreshadowing: list[str] = Field(default_factory=list)
    interpretive_warnings: list[str] = Field(default_factory=list)


class CommentPosition(str, Enum):
    """Where a comment sits relative to its target paragraphs."""

    BEFORE = "before"
    AFTER = "after"


class CommentKind(str, Enum):
    """The three supported annotation kinds."""

    INTRO = "intro"
    SUMMARY = "summary"
    NOTE = "note"


class CommentItem(BaseModel):
    """A single annotation targeting a contiguous range of paragraphs."""

    target_p_ids: list[int] = Field(..., min_length=1)
    position: CommentPosition
    kind: CommentKind
    content: str = Field(..., min_length=1, max_length=2000)


class BlockAnnotation(BaseModel):
    """Stage 2 output: all comments for a single block."""

    comments: list[CommentItem] = Field(default_factory=list)


class CommentOrphanPIdError(_errors.CommentOrphanPIdError):
    """Deprecated shim — kept for backward compatibility.

    The canonical definition lives in :mod:`epub_commentor.errors`. This
    alias exists so existing ``except CommentOrphanPIdError`` blocks (in
    tests and external callers) keep working; new code should import the
    class from :mod:`epub_commentor.errors` directly.
    """


class CommentOverlapError(_errors.CommentOverlapError):
    """Deprecated shim — see :class:`CommentOrphanPIdError` above."""


def _format_pids(pids: list[int]) -> str:
    return "[" + ", ".join(str(p) for p in pids) + "]"


def validate_block_annotations(ann: BlockAnnotation, block_size: int) -> list[CommentItem]:
    """Validate Stage 2 output against block boundaries and overlap rules.

    Rules enforced (in order):

    1. Every ``target_p_ids`` value is in ``[0, block_size)``.
    2. ``target_p_ids`` is a contiguous integer range (e.g. ``[3, 4, 5]``).
    3. No two comments of the **same** ``kind`` overlap. Comments of
       *different* kinds are allowed to share ``target_p_ids`` — this is
       the canonical "古书夹注" pattern: an ``intro`` framing a section
       while a ``note`` does close reading on a paragraph inside it,
       for example. The reader-facing CSS already renders the three
       kinds as visually distinct voices, so the juxtaposition is part
       of the intended annotation aesthetic.

    Returns the original ``ann.comments`` list on success so callers can chain
    the result without touching ``ann``.
    """
    used: dict[CommentKind, set[int]] = {}

    for comment in ann.comments:
        # Range check
        for pid in comment.target_p_ids:
            if pid < 0 or pid >= block_size:
                raise _errors.CommentOrphanPIdError(
                    f"comment references out-of-range p_id {pid} "
                    f"(block_size={block_size}, got {_format_pids(comment.target_p_ids)})"
                )

        # Contiguity check
        sorted_pids = sorted(comment.target_p_ids)
        if sorted_pids != list(range(sorted_pids[0], sorted_pids[-1] + 1)):
            raise _errors.CommentOrphanPIdError(
                f"comment target_p_ids must be contiguous, got {_format_pids(comment.target_p_ids)}"
            )

        # Overlap check — same kind only; different kinds may share p_ids
        bucket = used.setdefault(comment.kind, set())
        for pid in sorted_pids:
            if pid in bucket:
                raise _errors.CommentOverlapError(
                    f"p_id {pid} is targeted by more than one {comment.kind.value} comment "
                    f"in the same block (duplicate range starts at {sorted_pids[0]})"
                )
            bucket.add(pid)

    return ann.comments


__all__ = [
    "BlockAnnotation",
    "ChapterMemo",
    "CommentItem",
    "CommentKind",
    "CommentOrphanPIdError",
    "CommentOverlapError",
    "CommentPosition",
    "KeyTerm",
    "validate_block_annotations",
]
