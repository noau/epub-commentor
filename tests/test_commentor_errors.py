"""Unit tests for the M4 :mod:`epub_commentor.errors` hierarchy.

Verifies the exception taxonomy, the backward-compatible alias shims in
:mod:`epub_commentor.llm.schema`, and that ``process_chapters`` correctly
skips chapters that have zero ``<p>`` elements (the structural failure
case the pipeline must tolerate on real-world EPUBs).
"""

from __future__ import annotations

import io
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest

from epub_commentor import (
    CommentInvalidJSONError,
    CommentNoParagraphsError,
    CommentorError,
    CommentOrphanPIdError,
    CommentOverlapError,
    CommentReviewFailedError,
    CommentScanFailedError,
    CommentSelectFailedError,
)
from epub_commentor.config import CommentConfig
from epub_commentor.llm.schema import (
    BlockAnnotation,
    CommentItem,
    CommentKind,
    CommentPosition,
    validate_block_annotations,
)
from epub_commentor.llm.schema import (
    CommentOrphanPIdError as SchemaOrphanPIdError,
)
from epub_commentor.llm.schema import (
    CommentOverlapError as SchemaOverlapError,
)
from epub_commentor.pipeline.extract import Chapter
from epub_commentor.xml.xml_like import XMLLikeNode

# ---------------------------------------------------------------------------
# Hierarchy: every concrete subclass is a CommentorError and a ValueError.
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    def test_base_is_value_error_subclass(self) -> None:
        assert issubclass(CommentorError, ValueError)

    @pytest.mark.parametrize(
        "cls",
        [
            CommentInvalidJSONError,
            CommentNoParagraphsError,
            CommentOverlapError,
            CommentOrphanPIdError,
            CommentReviewFailedError,
            CommentScanFailedError,
            CommentSelectFailedError,
        ],
    )
    def test_concrete_subclass_inherits_commentor_error(self, cls: type) -> None:
        assert issubclass(cls, CommentorError)
        assert issubclass(cls, ValueError)

    def test_catch_via_base_class(self) -> None:
        with pytest.raises(CommentorError):
            raise CommentInvalidJSONError("oops")
        with pytest.raises(ValueError):
            raise CommentOrphanPIdError("oops")


# ---------------------------------------------------------------------------
# Schema shims: the legacy names still exist, and they ARE the canonical ones.
# ---------------------------------------------------------------------------


class TestSchemaShims:
    def test_legacy_orphan_pid_error_is_canonical(self) -> None:
        # raising the legacy alias should still be catchable via errors.CommentOrphanPIdError
        with pytest.raises(CommentOrphanPIdError):
            raise SchemaOrphanPIdError("x")
        # and the canonical and shim are in an inheritance relationship
        assert issubclass(SchemaOrphanPIdError, CommentOrphanPIdError)

    def test_legacy_overlap_error_is_canonical(self) -> None:
        with pytest.raises(CommentOverlapError):
            raise SchemaOverlapError("x")
        assert issubclass(SchemaOverlapError, CommentOverlapError)


# ---------------------------------------------------------------------------
# validate_block_annotations: still raises the same error classes after the
# M4 refactor (now they are the canonical CommentorError subclasses).
# ---------------------------------------------------------------------------


class TestValidateBlockAnnotations:
    def _mk(self, pids: list[int], kind: CommentKind = CommentKind.NOTE) -> CommentItem:
        return CommentItem(
            target_p_ids=pids,
            position=CommentPosition.BEFORE,
            kind=kind,
            content="x",
        )

    def test_out_of_range_raises_orphan(self) -> None:
        ann = BlockAnnotation(comments=[self._mk([0, 1, 5])])
        with pytest.raises(CommentOrphanPIdError):
            validate_block_annotations(ann, block_size=3)

    def test_non_contiguous_raises_orphan(self) -> None:
        ann = BlockAnnotation(comments=[self._mk([0, 2])])
        with pytest.raises(CommentOrphanPIdError):
            validate_block_annotations(ann, block_size=3)

    def test_overlap_raises_overlap(self) -> None:
        ann = BlockAnnotation(
            comments=[self._mk([0, 1]), self._mk([1, 2])],
        )
        with pytest.raises(CommentOverlapError):
            validate_block_annotations(ann, block_size=3)

    def test_valid_returns_comments(self) -> None:
        ann = BlockAnnotation(comments=[self._mk([0]), self._mk([2])])
        out = validate_block_annotations(ann, block_size=3)
        assert out is ann.comments


# ---------------------------------------------------------------------------
# process_chapters: zero-<p> chapters are skipped by default, raise when
# config.fail_on_empty_chapter is set.
# ---------------------------------------------------------------------------


def _make_zero_para_chapter() -> Chapter:
    """A chapter whose <body> has no <p> children."""
    root = fromstring("<html><body><div>no paragraphs here</div></body></html>")
    body = root.find("body")
    assert body is not None
    xml_node = XMLLikeNode(io.BytesIO(b"<html></html>"), is_html_like=True)
    xml_node.element = root
    return Chapter(path=Path("cover.xhtml"), title="cover", body=body, xml_node=xml_node)


def _make_text_chapter(n: int) -> Chapter:
    root_xml = "<html><body>" + "".join(f"<p>p{i}</p>" for i in range(n)) + "</body></html>"
    root = fromstring(root_xml)
    body = root.find("body")
    assert body is not None
    xml_node = XMLLikeNode(io.BytesIO(b"<html></html>"), is_html_like=True)
    xml_node.element = root
    return Chapter(path=Path(f"ch_{n}.xhtml"), title=f"ch_{n}", body=body, xml_node=xml_node)


class TestZeroParagraphChapter:
    def test_default_skips_silently(self, caplog: pytest.LogCaptureFixture) -> None:
        from epub_commentor.pipeline.process import process_chapters

        ch = _make_zero_para_chapter()

        # process_chapters would normally call LLM, but with zero paragraphs
        # we expect it to short-circuit before scan_chapter. We assert that
        # by passing a sentinel LLM that must NOT be touched.
        class _BoomLLM:
            def __getattr__(self, name: str) -> None:
                raise AssertionError(f"LLM should not be touched: {name}")

        with caplog.at_level("WARNING", logger="epub_commentor.pipeline.process"):
            anns, _ = process_chapters([ch], book_metadata={}, llm=_BoomLLM(), config=CommentConfig())  # type: ignore[arg-type]
        assert len(anns) == 1
        assert anns[0].comments == []
        assert any("zero <p>" in rec.message for rec in caplog.records)

    def test_fail_on_empty_chapter_raises(self) -> None:
        from epub_commentor.pipeline.process import process_chapters

        ch = _make_zero_para_chapter()
        config = CommentConfig(fail_on_empty_chapter=True)

        class _BoomLLM:
            def __getattr__(self, name: str) -> None:
                raise AssertionError(f"LLM should not be touched: {name}")

        with pytest.raises(CommentNoParagraphsError):
            process_chapters([ch], book_metadata={}, llm=_BoomLLM(), config=config)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Sanity: errors.py is the only home for these classes; the public surface
# re-exports them as expected.
# ---------------------------------------------------------------------------


def test_top_level_re_exports() -> None:
    import epub_commentor
    import epub_commentor.errors as errors_mod

    for name in (
        "CommentorError",
        "CommentInvalidJSONError",
        "CommentOrphanPIdError",
        "CommentOverlapError",
        "CommentReviewFailedError",
        "CommentScanFailedError",
        "CommentSelectFailedError",
        "CommentNoParagraphsError",
    ):
        assert getattr(epub_commentor, name) is getattr(errors_mod, name)
