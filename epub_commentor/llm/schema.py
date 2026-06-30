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


class KeyTerm(BaseModel):
    """Single term/glossary pair returned by Stage 1."""

    term: str = Field(..., min_length=1, max_length=200)
    gloss: str = Field(..., min_length=1, max_length=1000)


class ChapterMemo(BaseModel):
    """Stage 1 output: a compact summary used to guide Stage 2."""

    core_thesis: str = Field(..., min_length=1, max_length=2000)
    outline: list[str] = Field(..., min_length=3, max_length=7)
    key_terms: list[KeyTerm] = Field(default_factory=list, max_length=15)
    tone: str = Field(..., min_length=1, max_length=200)
    target_audience: str = Field(..., min_length=1, max_length=500)
    reading_anchors: list[str] = Field(default_factory=list, max_length=3)


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


class CommentOrphanPIdError(ValueError):
    """A comment references p_ids outside the block, or a non-contiguous range."""


class CommentOverlapError(ValueError):
    """Two comments inside the same block share one or more p_ids."""


def _format_pids(pids: list[int]) -> str:
    return "[" + ", ".join(str(p) for p in pids) + "]"


def validate_block_annotations(ann: BlockAnnotation, block_size: int) -> list[CommentItem]:
    """Validate Stage 2 output against block boundaries and overlap rules.

    Rules enforced (in order):

    1. Every ``target_p_ids`` value is in ``[0, block_size)``.
    2. ``target_p_ids`` is a contiguous integer range (e.g. ``[3, 4, 5]``).
    3. No two comments in the same block overlap.

    Returns the original ``ann.comments`` list on success so callers can chain
    the result without touching ``ann``.
    """
    used: set[int] = set()

    for comment in ann.comments:
        # Range check
        for pid in comment.target_p_ids:
            if pid < 0 or pid >= block_size:
                raise CommentOrphanPIdError(
                    f"comment references out-of-range p_id {pid} "
                    f"(block_size={block_size}, got {_format_pids(comment.target_p_ids)})"
                )

        # Contiguity check
        sorted_pids = sorted(comment.target_p_ids)
        if sorted_pids != list(range(sorted_pids[0], sorted_pids[-1] + 1)):
            raise CommentOrphanPIdError(
                f"comment target_p_ids must be contiguous, got {_format_pids(comment.target_p_ids)}"
            )

        # Overlap check (within the same block)
        for pid in sorted_pids:
            if pid in used:
                raise CommentOverlapError(
                    f"p_id {pid} is targeted by more than one comment "
                    f"in the same block (duplicate range starts at {sorted_pids[0]})"
                )
            used.add(pid)

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
