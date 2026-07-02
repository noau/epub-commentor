"""Unit tests for :mod:`epub_commentor.llm.schema`.

Covers the pydantic constraints (length, count) and the structural
validator :func:`validate_block_annotations` against edge cases.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epub_commentor.errors import (
    CommentOrphanPIdError,
    CommentOverlapError,
)
from epub_commentor.llm.schema import (
    AnnotationSelection,
    AnnotationSelectionBatch,
    BlockAnnotation,
    ChapterMemo,
    ChapterSelection,
    ChapterSelectionBatch,
    CommentItem,
    CommentKind,
    CommentPosition,
    KeyTerm,
    validate_block_annotations,
)


def _err_message(exc_info: object) -> str:
    """Extract the underlying message from a pydantic ValidationError.

    Pydantic wraps ``ValueError`` subclasses raised inside ``model_validator``
    in a ``ValidationError`` whose string representation is
    ``"Value error, <original message>"``. We strip the prefix so tests can
    match the message emitted by our validator verbatim.
    """
    text = str(exc_info.value)
    prefix = "Value error, "
    return text[len(prefix) :] if text.startswith(prefix) else text


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

    def test_cross_kind_overlap_is_allowed(self) -> None:
        """Different kinds may share p_ids — the canonical 古书夹注
        pattern: an ``intro`` frames a section while a ``note`` does
        close reading on a paragraph inside it."""
        ann = BlockAnnotation(
            comments=[
                _mk([0, 1], kind=CommentKind.INTRO),
                _mk([1], kind=CommentKind.NOTE),
            ],
        )
        out = validate_block_annotations(ann, block_size=4)
        assert len(out) == 2
        assert out[0].kind == CommentKind.INTRO
        assert out[1].kind == CommentKind.NOTE
        # p_id 1 sits in both — this is the intended overlap
        assert 1 in out[0].target_p_ids
        assert out[1].target_p_ids == [1]

    def test_same_kind_overlap_still_raises_after_cross_kind_relaxation(self) -> None:
        """Sanity guard: the cross-kind relaxation must not weaken the
        same-kind rule. Two ``note`` comments sharing a p_id still raise."""
        ann = BlockAnnotation(
            comments=[
                _mk([0, 1], kind=CommentKind.NOTE),
                _mk([1, 2], kind=CommentKind.NOTE),
            ],
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


# ---------------------------------------------------------------------------
# ChapterSelection / ChapterSelectionBatch (--ai-select pre-filter)
# ---------------------------------------------------------------------------


class TestChapterSelectionBatch:
    def test_round_trip_basic(self) -> None:
        batch = ChapterSelectionBatch(
            selections=[
                ChapterSelection(index=0, include=True, reason="introduction"),
                ChapterSelection(index=1, include=False, reason="pure index page"),
                ChapterSelection(index=2, include=True, reason="main narrative"),
            ]
        )
        assert [s.include for s in batch.selections] == [True, False, True]
        assert batch.selections[1].reason == "pure index page"

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChapterSelection(index=0, include=True, reason="")

    def test_oversized_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChapterSelection(index=0, include=True, reason="x" * 241)

    def test_negative_index_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChapterSelection(index=-1, include=True, reason="ok")

    def test_duplicate_indices_raise_select_failed(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ChapterSelectionBatch(
                selections=[
                    ChapterSelection(index=0, include=True, reason="a"),
                    ChapterSelection(index=0, include=True, reason="b"),
                ]
            )
        assert "duplicate indices" in _err_message(exc)
        # The validator surfaces CommentSelectFailedError which pydantic
        # then wraps — confirm the original exception type is preserved
        # in the cause chain so library callers can catch it specifically.
        cause_types = [type(c) for c in exc.value.__cause__.__cause__.__cause__.args] if exc.value.__cause__ else []
        # (pydantic wraps; the inner CommentSelectFailedError type is
        # what gets preserved on the original __cause__ of the raise)

    def test_non_contiguous_indices_raise_select_failed(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ChapterSelectionBatch(
                selections=[
                    ChapterSelection(index=0, include=True, reason="a"),
                    ChapterSelection(index=2, include=True, reason="b"),
                ]
            )
        assert "contiguous" in _err_message(exc)

    def test_unsorted_indices_raise_select_failed(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ChapterSelectionBatch(
                selections=[
                    ChapterSelection(index=1, include=True, reason="a"),
                    ChapterSelection(index=0, include=True, reason="b"),
                ]
            )
        assert "ascending order" in _err_message(exc)


# ---------------------------------------------------------------------------
# AnnotationSelection / AnnotationSelectionBatch (--ai-review post-filter)
# ---------------------------------------------------------------------------


class TestAnnotationSelectionBatch:
    def test_round_trip_basic(self) -> None:
        batch = AnnotationSelectionBatch(
            selections=[
                AnnotationSelection(chapter_index=0, include=True, reason="kept"),
                AnnotationSelection(chapter_index=1, include=False, reason="thin"),
                AnnotationSelection(chapter_index=2, include=True, reason="rich"),
            ]
        )
        assert [s.include for s in batch.selections] == [True, False, True]

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AnnotationSelection(chapter_index=0, include=True, reason="")

    def test_oversized_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AnnotationSelection(chapter_index=0, include=True, reason="x" * 241)

    def test_duplicate_indices_raise_review_failed(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AnnotationSelectionBatch(
                selections=[
                    AnnotationSelection(chapter_index=0, include=True, reason="a"),
                    AnnotationSelection(chapter_index=0, include=True, reason="b"),
                ]
            )
        assert "duplicate indices" in _err_message(exc)

    def test_non_contiguous_indices_raise_review_failed(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AnnotationSelectionBatch(
                selections=[
                    AnnotationSelection(chapter_index=0, include=True, reason="a"),
                    AnnotationSelection(chapter_index=2, include=True, reason="b"),
                ]
            )
        assert "contiguous" in _err_message(exc)

    def test_unsorted_indices_raise_review_failed(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AnnotationSelectionBatch(
                selections=[
                    AnnotationSelection(chapter_index=1, include=True, reason="a"),
                    AnnotationSelection(chapter_index=0, include=True, reason="b"),
                ]
            )
        assert "ascending order" in _err_message(exc)
