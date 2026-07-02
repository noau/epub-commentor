"""Unit tests for the post-process annotation review gate.

The review gate lives in :func:`epub_commentor.commentor._review_gate`
and is wired into :func:`comment_epub` between Stage 2 and injection.
These tests drive ``_review_gate`` directly with synthetic
:class:`ChapterAnnotation` lists and stub filters — no LLM calls,
no EPUB I/O.

Covers three concerns:

- **Trigger / no-op paths**: empty annotations, ``annotation_filter is
  None``, all annotations clean (handled by CLI's smart trigger —
  but ``_review_gate`` itself is policy-free and always invokes the
  filter when one is supplied).
- **Mask validation**: parallel ``list[bool]`` of length N or
  :class:`ValueError`. Mirrors :func:`chapter_filter`'s contract at
  commentor.py:197-207.
- **Progress lifecycle**: the live Rich progress bar (when reachable
  via ``progress_callback.__self__.close``) is closed *before* the
  filter opens any interactive UI, so the two never share terminal
  ownership.
"""

from __future__ import annotations

import inspect
from unittest import mock
from xml.etree.ElementTree import fromstring

import pytest

from epub_commentor import comment_epub
from epub_commentor.commentor import _review_gate
from epub_commentor.llm.schema import ChapterMemo, CommentItem
from epub_commentor.pipeline import ChapterAnnotation
from epub_commentor.pipeline.extract import Chapter


def _mk_annotation(
    i: int,
    *,
    comments: list[CommentItem] | None = None,
    skipped_blocks: int = 0,
    has_empty_blocks: int = 0,
) -> ChapterAnnotation:
    """Build a bare ChapterAnnotation for ``_review_gate`` tests."""
    body = fromstring(f"<html><body><p>p{i}</p></body></html>").find("body")
    assert body is not None
    chapter = Chapter(path=f"ch{i}.xhtml", title=f"Chapter {i}", body=body, xml_node=None)
    memo = ChapterMemo(
        core_thesis=f"thesis {i}",
        outline=["a", "b", "c"],
        tone="t",
        target_audience="g",
    )
    return ChapterAnnotation(
        chapter=chapter,
        memo=memo,
        comments=comments or [],
        skipped_blocks=skipped_blocks,
        has_empty_blocks=has_empty_blocks,
    )


class TestReviewGateNoOp:
    """Paths where the gate is a no-op — filter not invoked."""

    def test_no_filter_returns_unchanged(self) -> None:
        """``annotation_filter=None`` (CLI passed ``--no-review`` or the
        default short-circuit fired) — gate returns annotations as-is
        and never invokes anything."""
        annotations = [_mk_annotation(0), _mk_annotation(1)]
        out = _review_gate(annotations, annotation_filter=None, progress_callback=None)
        assert out == annotations

    def test_empty_annotations_no_op(self) -> None:
        """Empty annotations list — gate returns ``[]`` without
        invoking the filter (avoids opening a picker with zero choices)."""
        called = []

        def _f(annotations: list[ChapterAnnotation]) -> list[bool]:
            called.append(annotations)
            return []

        out = _review_gate([], annotation_filter=_f, progress_callback=None)
        assert out == []
        assert called == []

    def test_filter_invoked_when_supplied(self) -> None:
        """Positive control: filter is invoked when both annotations
        and the filter are supplied."""
        annotations = [_mk_annotation(0), _mk_annotation(1)]

        def _f(anns: list[ChapterAnnotation]) -> list[bool]:
            assert anns == annotations
            return [True, False]

        out = _review_gate(annotations, annotation_filter=_f, progress_callback=None)
        assert out == [annotations[0]]


class TestMaskValidation:
    """``_review_gate`` validates the filter's mask before applying it.

    Mirrors the :data:`ChapterFilter` contract at commentor.py:197-207:
    a parallel ``list[bool]`` of length N. Anything else raises
    :class:`ValueError` (not :class:`CommentorError` — this is a
    programmer error, not a pipeline outcome).
    """

    def test_mask_length_mismatch_raises_value_error(self) -> None:
        annotations = [_mk_annotation(0), _mk_annotation(1), _mk_annotation(2)]
        with pytest.raises(ValueError, match=r"parallel list\[bool\]"):
            _review_gate(annotations, annotation_filter=lambda _: [True, False], progress_callback=None)

    def test_mask_non_bool_raises_value_error(self) -> None:
        annotations = [_mk_annotation(0), _mk_annotation(1)]
        with pytest.raises(ValueError, match=r"parallel list\[bool\]"):
            _review_gate(annotations, annotation_filter=lambda _: [1, 0], progress_callback=None)

    def test_mask_non_list_raises_value_error(self) -> None:
        annotations = [_mk_annotation(0)]
        with pytest.raises(ValueError, match=r"parallel list\[bool\]"):
            _review_gate(annotations, annotation_filter=lambda _: (True,), progress_callback=None)


class TestProgressInteraction:
    """Progress bar lifecycle around the filter invocation.

    When the filter is invoked, the live Rich progress bar (reachable
    via ``progress_callback.__self__.close``) is closed first so the
    interactive selector never shares terminal ownership with it. The
    CLI's outer ``finally`` block calls ``close()`` again — it's
    idempotent, so the double-close is a no-op.
    """

    def test_progress_closed_before_filter_runs(self) -> None:
        annotations = [_mk_annotation(0)]
        order: list[str] = []

        # Use a custom close that records into `order`; Mock gives us
        # `assert_called_once` automatically when we don't override the
        # method.
        fake_progress = mock.Mock()
        fake_progress.close.side_effect = lambda: order.append("close")

        def _f(anns: list[ChapterAnnotation]) -> list[bool]:
            order.append("filter")
            return [True]

        # Simulate cli.py's bound-method callback where __self__ is the
        # renderer. Build a mock whose __self__ attribute exposes a
        # close() method.
        callback = mock.Mock()
        callback.__self__ = fake_progress

        out = _review_gate(annotations, annotation_filter=_f, progress_callback=callback)
        assert out == annotations
        assert order == ["close", "filter"]
        fake_progress.close.assert_called_once()

    def test_progress_not_closed_when_filter_is_none(self) -> None:
        """No-op path → no progress interaction."""
        annotations = [_mk_annotation(0)]
        callback = mock.Mock()
        callback.__self__ = mock.Mock()
        _review_gate(annotations, annotation_filter=None, progress_callback=callback)
        callback.__self__.close.assert_not_called()

    def test_progress_not_closed_when_annotations_empty(self) -> None:
        callback = mock.Mock()
        callback.__self__ = mock.Mock()
        _review_gate([], annotation_filter=lambda anns: [], progress_callback=callback)
        callback.__self__.close.assert_not_called()

    def test_no_self_attribute_does_not_crash(self) -> None:
        """Plain-callable progress callbacks (custom user hooks that
        don't expose ``__self__``) must not break the gate — the
        attribute probe degrades to a no-op."""
        original = _mk_annotation(0)

        def _f(anns: list[ChapterAnnotation]) -> list[bool]:
            return [True]

        def _plain_progress(event) -> None:  # no __self__ attribute
            pass

        # Should not raise AttributeError.
        out = _review_gate([original], annotation_filter=_f, progress_callback=_plain_progress)
        assert out == [original]


class TestReviewGatePublicAPI:
    """Sanity checks that :func:`comment_epub` exposes the new kwarg."""

    def test_comment_epub_accepts_annotation_filter_kwarg(self) -> None:
        """Smoke test: ``comment_epub`` should accept the new kwarg
        without raising TypeError. Doesn't run a full pipeline —
        verifies the signature only."""
        sig = inspect.signature(comment_epub)
        assert "annotation_filter" in sig.parameters
        # Default is None — backward-compatible.
        assert sig.parameters["annotation_filter"].default is None
