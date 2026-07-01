"""Tests for Stage 2 partial-success salvaging.

The salvage path in
:mod:`epub_commentor.llm._salvage` is the last line of defense after
the multi-turn retry loop in :func:`epub_commentor.llm.block.annotate_block`
exhausts its attempts. A typical failure looks like: the LLM returns 6
``CommentItem`` objects, 5 of them valid, 1 with a non-contiguous
``target_p_ids`` like ``[0, 5]``. Without salvage the strict
``validate_block_annotations`` would throw and the block would lose
all 5 valid comments too. Salvage keeps what it can repair, drops what
it cannot, and only returns ``None`` when every comment is broken.
"""

from __future__ import annotations

import io
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest
from _mock_llm import MockLLM, json_dumps

from epub_commentor.config import CommentConfig
from epub_commentor.errors import CommentInvalidJSONError
from epub_commentor.llm._salvage import (
    _fix_anchor,
    salvage_block_annotations,
)
from epub_commentor.llm.schema import (
    BlockAnnotation,
    CommentItem,
    CommentKind,
    CommentPosition,
)
from epub_commentor.pipeline.extract import Chapter
from epub_commentor.pipeline.process import process_chapters
from epub_commentor.xml.xml_like import XMLLikeNode

# ---- pure-function tests ---------------------------------------------------


class TestFixAnchor:
    def test_before_takes_min(self) -> None:
        assert _fix_anchor([0, 1, 5, 6], CommentPosition.BEFORE) == [0]

    def test_after_takes_max(self) -> None:
        assert _fix_anchor([0, 1, 5, 6], CommentPosition.AFTER) == [6]

    def test_singleton_unchanged(self) -> None:
        # Length-1 lists are already anchors; the function is idempotent.
        assert _fix_anchor([3], CommentPosition.BEFORE) == [3]
        assert _fix_anchor([3], CommentPosition.AFTER) == [3]

    def test_unsorted_input_uses_min_max(self) -> None:
        # _fix_anchor doesn't sort; the caller is expected to feed
        # pre-sorted p_ids. min/max work on whatever the input is.
        assert _fix_anchor([7, 2, 5], CommentPosition.BEFORE) == [2]
        assert _fix_anchor([7, 2, 5], CommentPosition.AFTER) == [7]

    def test_empty_returns_empty(self) -> None:
        # Defensive: an empty list passes through unchanged. The caller
        # should already have filtered empty target_p_ids via pydantic
        # (``min_length=1``), but we don't crash on it.
        assert _fix_anchor([], CommentPosition.BEFORE) == []


def _comment(
    pids: list[int], kind: CommentKind, position: CommentPosition, content: str = "x"
) -> CommentItem:
    return CommentItem(
        target_p_ids=pids,
        position=position,
        kind=kind,
        content=content,
    )


class TestSalvageBlockAnnotations:
    def test_keeps_all_when_all_valid(self) -> None:
        ann = BlockAnnotation(
            comments=[
                _comment([0], CommentKind.INTRO, CommentPosition.BEFORE, "i"),
                _comment([1, 2], CommentKind.NOTE, CommentPosition.AFTER, "n"),
                _comment([3], CommentKind.SUMMARY, CommentPosition.AFTER, "s"),
            ]
        )
        result = salvage_block_annotations(ann, block_size=4)
        assert result is not None
        assert len(result) == 3
        # Order preserved.
        assert [c.content for c in result] == ["i", "n", "s"]

    def test_drops_out_of_range_comments(self) -> None:
        ann = BlockAnnotation(
            comments=[
                _comment([0], CommentKind.NOTE, CommentPosition.BEFORE, "ok"),
                _comment([10], CommentKind.NOTE, CommentPosition.BEFORE, "bad"),
            ]
        )
        result = salvage_block_annotations(ann, block_size=4)
        assert result is not None
        assert [c.content for c in result] == ["ok"]

    def test_fixes_non_contiguous_via_min_for_before(self) -> None:
        ann = BlockAnnotation(
            comments=[
                # Non-contiguous [0, 3], position=before → anchor at min=0.
                _comment([0, 3], CommentKind.NOTE, CommentPosition.BEFORE, "fixed"),
            ]
        )
        result = salvage_block_annotations(ann, block_size=4)
        assert result is not None
        assert len(result) == 1
        assert result[0].target_p_ids == [0]
        assert result[0].content == "fixed"

    def test_fixes_non_contiguous_via_max_for_after(self) -> None:
        ann = BlockAnnotation(
            comments=[
                # Non-contiguous [0, 3], position=after → anchor at max=3.
                _comment([0, 3], CommentKind.NOTE, CommentPosition.AFTER, "fixed"),
            ]
        )
        result = salvage_block_annotations(ann, block_size=4)
        assert result is not None
        assert len(result) == 1
        assert result[0].target_p_ids == [3]

    def test_drops_overlapping_same_kind(self) -> None:
        ann = BlockAnnotation(
            comments=[
                # First claim on p_id 2 wins.
                _comment([2], CommentKind.NOTE, CommentPosition.BEFORE, "first"),
                _comment([2], CommentKind.NOTE, CommentPosition.AFTER, "second"),
            ]
        )
        result = salvage_block_annotations(ann, block_size=4)
        assert result is not None
        assert [c.content for c in result] == ["first"]

    def test_keeps_overlapping_different_kinds(self) -> None:
        # The strict validator also allows this — the "古书夹注" pattern.
        ann = BlockAnnotation(
            comments=[
                _comment([2], CommentKind.INTRO, CommentPosition.BEFORE, "intro"),
                _comment([2], CommentKind.NOTE, CommentPosition.AFTER, "note"),
            ]
        )
        result = salvage_block_annotations(ann, block_size=4)
        assert result is not None
        assert len(result) == 2

    def test_returns_none_when_all_invalid(self) -> None:
        ann = BlockAnnotation(
            comments=[
                _comment([10], CommentKind.NOTE, CommentPosition.BEFORE, "bad1"),
                _comment([99], CommentKind.NOTE, CommentPosition.BEFORE, "bad2"),
                _comment([100], CommentKind.NOTE, CommentPosition.AFTER, "bad3"),
            ]
        )
        assert salvage_block_annotations(ann, block_size=4) is None

    def test_mixed_keeps_valid_fixes_repairable_drops_unfixable(self) -> None:
        ann = BlockAnnotation(
            comments=[
                _comment([0], CommentKind.INTRO, CommentPosition.BEFORE, "intro"),  # valid
                _comment([1, 4], CommentKind.NOTE, CommentPosition.BEFORE, "noncontig"),  # → [1]
                _comment([10], CommentKind.NOTE, CommentPosition.BEFORE, "outofrange"),  # drop
                _comment([2], CommentKind.SUMMARY, CommentPosition.AFTER, "summary"),  # valid
            ]
        )
        result = salvage_block_annotations(ann, block_size=6)
        assert result is not None
        assert [c.content for c in result] == ["intro", "noncontig", "summary"]

    def test_preserves_input_order(self) -> None:
        ann = BlockAnnotation(
            comments=[
                _comment([0], CommentKind.INTRO, CommentPosition.BEFORE, "a"),
                _comment([1, 3], CommentKind.NOTE, CommentPosition.AFTER, "b"),
                _comment([2], CommentKind.SUMMARY, CommentPosition.AFTER, "c"),
            ]
        )
        result = salvage_block_annotations(ann, block_size=4)
        assert result is not None
        assert [c.content for c in result] == ["a", "b", "c"]


# ---- annotate_block integration --------------------------------------------


def _mk_chapter(n_paragraphs: int, tmp_path: Path) -> Chapter:
    body_xml = "<html><body>" + "".join(f"<p>p{i}</p>" for i in range(n_paragraphs)) + "</body></html>"
    root = fromstring(body_xml)
    body = root.find("body")
    assert body is not None
    xml_node = XMLLikeNode(io.BytesIO(b"<html></html>"), is_html_like=True)
    xml_node.element = root
    return Chapter(path=tmp_path / "ch.xhtml", title="ch", body=body, xml_node=xml_node)


def _memo_json() -> str:
    return json_dumps(
        {
            "core_thesis": "x",
            "outline": ["a", "b", "c"],
            "tone": "t",
            "target_audience": "g",
        }
    )


class TestAnnotateBlockSalvage:
    def test_annotate_block_returns_salvaged_after_retries_exhausted(self, tmp_path: Path) -> None:
        """When retries are exhausted but the last parsed object has
        salvageable comments, ``annotate_block`` should return them
        instead of raising ``CommentInvalidJSONError``.
        """
        from epub_commentor.llm.block import annotate_block

        # Build a block with 4 paragraphs and seed the LLM to always
        # return 3 comments: 2 valid + 1 with non-contiguous p_ids
        # (the validator throws, the salvage path turns it into a
        # length-1 anchor and returns all 3).
        block_ps = [fromstring(f"<p>p{i}</p>") for i in range(4)]
        bad_response = json_dumps(
            {
                "comments": [
                    {
                        "target_p_ids": [0],
                        "position": "before",
                        "kind": "intro",
                        "content": "intro",
                    },
                    {
                        # Non-contiguous — the LLM keeps doing this.
                        "target_p_ids": [1, 3],
                        "position": "after",
                        "kind": "note",
                        "content": "note",
                    },
                    {
                        "target_p_ids": [3],
                        "position": "after",
                        "kind": "summary",
                        "content": "summary",
                    },
                ]
            }
        )
        # max_json_retries=2 to keep the test fast: 1 bad attempt → salvage.
        llm = MockLLM(responses_by_seed={"annotate__response": bad_response})
        config = CommentConfig(max_json_retries=2, block_size=4)

        # Use a stub ChapterMemo so annotate_block can format its private context.
        from epub_commentor.llm.schema import ChapterMemo

        memo = ChapterMemo(
            core_thesis="x",
            outline=["a", "b", "c"],
            tone="t",
            target_audience="g",
        )
        result = annotate_block(
            block_ps=block_ps,
            block_start_idx=0,
            chapter_hash="deadbeef",
            memo=memo,
            llm=llm,
            config=config,
        )
        # Salvage returned 3 comments — the broken one was repaired to [3].
        assert len(result) == 3
        contents = [c.content for c in result]
        assert contents == ["intro", "note", "summary"]
        # The non-contiguous comment was collapsed to its `after` anchor.
        note = next(c for c in result if c.content == "note")
        assert note.target_p_ids == [3]

    def test_annotate_block_raises_when_salvage_returns_none(self, tmp_path: Path) -> None:
        """When every comment is broken (e.g. all out-of-range) and
        retries are exhausted, ``annotate_block`` should raise
        ``CommentInvalidJSONError`` as before.
        """
        from epub_commentor.llm.block import annotate_block
        from epub_commentor.llm.schema import ChapterMemo

        block_ps = [fromstring(f"<p>p{i}</p>") for i in range(3)]
        all_bad = json_dumps(
            {
                "comments": [
                    {
                        "target_p_ids": [99],  # out of range
                        "position": "before",
                        "kind": "note",
                        "content": "x",
                    },
                    {
                        "target_p_ids": [50],  # out of range
                        "position": "after",
                        "kind": "note",
                        "content": "y",
                    },
                ]
            }
        )
        llm = MockLLM(responses_by_seed={"annotate__response": all_bad})
        config = CommentConfig(max_json_retries=2, block_size=3)
        memo = ChapterMemo(
            core_thesis="x",
            outline=["a", "b", "c"],
            tone="t",
            target_audience="g",
        )
        with pytest.raises(CommentInvalidJSONError):
            annotate_block(
                block_ps=block_ps,
                block_start_idx=0,
                chapter_hash="deadbeef",
                memo=memo,
                llm=llm,
                config=config,
            )

    def test_process_chapters_uses_salvaged_comments(self, tmp_path: Path) -> None:
        """End-to-end: process_chapters with a chapter that triggers
        salvage should accumulate the salvaged comments in the final
        ``ChapterAnnotation.comments`` list.
        """
        chapter = _mk_chapter(4, tmp_path)
        # 1 block of 4 paragraphs, all in one chapter.
        bad_response = json_dumps(
            {
                "comments": [
                    {
                        "target_p_ids": [0],
                        "position": "before",
                        "kind": "intro",
                        "content": "intro",
                    },
                    {
                        # Non-contiguous.
                        "target_p_ids": [1, 3],
                        "position": "after",
                        "kind": "note",
                        "content": "note",
                    },
                ]
            }
        )
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": bad_response,
            }
        )
        config = CommentConfig(max_json_retries=2, block_size=4, concurrency=1)
        annotations, blocks_skipped = process_chapters(
            chapters=[chapter],
            book_metadata={},
            llm=llm,
            config=config,
        )
        assert len(annotations) == 1
        # No block was skipped — salvage rescued the partial output.
        assert blocks_skipped == 0
        assert len(annotations[0].comments) == 2
        contents = [c.content for c in annotations[0].comments]
        assert contents == ["intro", "note"]
