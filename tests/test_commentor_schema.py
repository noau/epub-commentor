"""Unit tests for :mod:`epub_commentor.llm.schema`.

Covers the pydantic constraints (length, count) and the structural
validator :func:`validate_block_annotations` against edge cases.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epub_commentor.errors import CommentOrphanPIdError, CommentOverlapError
from epub_commentor.llm.schema import (
    BlockAnnotation,
    ChapterMemo,
    CommentItem,
    CommentKind,
    CommentPosition,
    KeyTerm,
    validate_block_annotations,
)

# ---------------------------------------------------------------------------
# ChapterMemo constraints
# ---------------------------------------------------------------------------


class TestChapterMemo:
    def test_minimum_outline_required(self) -> None:
        with pytest.raises(ValidationError):
            ChapterMemo(
                core_thesis="x",
                outline=["a", "b"],  # need 3-7
                tone="t",
                target_audience="g",
            )

    def test_maximum_outline(self) -> None:
        with pytest.raises(ValidationError):
            ChapterMemo(
                core_thesis="x",
                outline=["a"] * 8,  # max 7
                tone="t",
                target_audience="g",
            )

    def test_optional_key_terms_default_empty(self) -> None:
        m = ChapterMemo(
            core_thesis="x",
            outline=["a", "b", "c"],
            tone="t",
            target_audience="g",
        )
        assert m.key_terms == []
        assert m.reading_anchors == []

    def test_key_term_validation(self) -> None:
        # empty term rejected
        with pytest.raises(ValidationError):
            KeyTerm(term="", gloss="g")
        # empty gloss rejected
        with pytest.raises(ValidationError):
            KeyTerm(term="t", gloss="")


# ---------------------------------------------------------------------------
# CommentItem constraints
# ---------------------------------------------------------------------------


class TestCommentItem:
    def test_empty_target_p_ids_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CommentItem(target_p_ids=[], position=CommentPosition.BEFORE, kind=CommentKind.NOTE, content="x")

    def test_too_long_content_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CommentItem(
                target_p_ids=[0],
                position=CommentPosition.BEFORE,
                kind=CommentKind.NOTE,
                content="x" * 2001,
            )


# ---------------------------------------------------------------------------
# validate_block_annotations edge cases
# ---------------------------------------------------------------------------


def _mk(pids: list[int], kind: CommentKind = CommentKind.NOTE) -> CommentItem:
    return CommentItem(target_p_ids=pids, position=CommentPosition.BEFORE, kind=kind, content="x")


class TestValidateBlockAnnotationsEdgeCases:
    def test_empty_annotations_returns_empty(self) -> None:
        out = validate_block_annotations(BlockAnnotation(), block_size=4)
        assert out == []

    def test_single_pid_comment(self) -> None:
        ann = BlockAnnotation(comments=[_mk([0])])
        out = validate_block_annotations(ann, block_size=4)
        assert out[0].target_p_ids == [0]

    def test_full_block_anchor(self) -> None:
        # pids span the entire block
        ann = BlockAnnotation(comments=[_mk([0, 1, 2, 3, 4, 5])])
        out = validate_block_annotations(ann, block_size=6)
        assert out[0].target_p_ids == [0, 1, 2, 3, 4, 5]

    def test_overlap_across_comments(self) -> None:
        ann = BlockAnnotation(
            comments=[_mk([0, 1, 2]), _mk([2, 3])],
        )
        with pytest.raises(CommentOverlapError):
            validate_block_annotations(ann, block_size=4)

    def test_negative_pid_raises(self) -> None:
        ann = BlockAnnotation(comments=[_mk([-1, 0])])
        with pytest.raises(CommentOrphanPIdError):
            validate_block_annotations(ann, block_size=4)

    def test_pid_at_block_size_raises(self) -> None:
        # block_size=4 means valid pids are 0..3
        ann = BlockAnnotation(comments=[_mk([3, 4])])
        with pytest.raises(CommentOrphanPIdError):
            validate_block_annotations(ann, block_size=4)
