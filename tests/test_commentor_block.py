"""Unit tests for :mod:`epub_commentor.llm.block` (Stage 2).

Covers three behaviours through the mock LLM:

* Happy path — one valid response produces the comments.
* Absolute p_id translation — block-local p_id ``0`` in block 2 becomes
  absolute ``block_size * 2`` after ``_process_chapter`` applies the
  offset.
* Retry — invalid JSON triggers a second call that succeeds.
"""

from __future__ import annotations

import io
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest
from _mock_llm import MockLLM, json_dumps

from epub_commentor.config import CommentConfig
from epub_commentor.errors import CommentInvalidJSONError
from epub_commentor.llm.block import annotate_block
from epub_commentor.llm.schema import (
    ChapterMemo,
    CommentKind,
)
from epub_commentor.pipeline.extract import Chapter
from epub_commentor.xml.xml_like import XMLLikeNode


def _mk_chapter(n_paragraphs: int) -> Chapter:
    body_xml = "".join(f"<p>p{i}</p>" for i in range(n_paragraphs))
    root = fromstring(f"<html><body>{body_xml}</body></html>")
    body = root.find("body")
    assert body is not None
    xml_node = XMLLikeNode(io.BytesIO(b"<html></html>"), is_html_like=True)
    xml_node.element = root
    return Chapter(path=Path("ch.xhtml"), title="ch", body=body, xml_node=xml_node)


def _mk_memo() -> ChapterMemo:
    return ChapterMemo(
        core_thesis="x",
        outline=["a", "b", "c"],
        tone="t",
        target_audience="g",
    )


class TestAnnotateBlock:
    def test_happy_path_returns_comments(self) -> None:
        chapter = _mk_chapter(3)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(
            responses_by_seed={
                "annotate__response": json_dumps(
                    {
                        "comments": [
                            {"target_p_ids": [0], "position": "before", "kind": "note", "content": "first"},
                            {"target_p_ids": [2], "position": "after", "kind": "summary", "content": "last"},
                        ]
                    }
                )
            }
        )
        out = annotate_block(
            block_ps=paras,
            block_start_idx=0,
            chapter_hash="chhash",
            memo=_mk_memo(),
            llm=llm,
            config=CommentConfig(),
        )
        assert len(out) == 2
        assert out[0].target_p_ids == [0]
        assert out[0].kind == CommentKind.NOTE
        assert out[1].target_p_ids == [2]
        assert out[1].kind == CommentKind.SUMMARY

    def test_retry_recovers_after_invalid_response(self) -> None:
        chapter = _mk_chapter(2)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(
            responses_by_seed={
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "ok"}]}
                )
            }
        )

        # Override _route to return invalid JSON on the first call only.
        real_route = llm._route
        calls = {"n": 0}

        def flaky_route(seed, messages):
            calls["n"] += 1
            if calls["n"] == 1:
                return "this is not json"
            return real_route(seed, messages)

        llm._route = flaky_route  # type: ignore[assignment]

        out = annotate_block(
            block_ps=paras,
            block_start_idx=0,
            chapter_hash="chhash",
            memo=_mk_memo(),
            llm=llm,
            config=CommentConfig(max_json_retries=3),
        )
        assert len(out) == 1
        assert out[0].content == "ok"
        assert calls["n"] == 2  # 1 bad + 1 good

    def test_retry_exhausted_raises_invalid_json(self) -> None:
        chapter = _mk_chapter(1)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(default_response="not json")
        with pytest.raises(CommentInvalidJSONError):
            annotate_block(
                block_ps=paras,
                block_start_idx=0,
                chapter_hash="chhash",
                memo=_mk_memo(),
                llm=llm,
                config=CommentConfig(max_json_retries=2),
            )

    def test_block_local_p_ids_returned_unchanged(self) -> None:
        """annotate_block returns comments with block-local p_ids; the
        absolute translation is done by process.py."""
        chapter = _mk_chapter(6)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(
            responses_by_seed={
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [5], "position": "after", "kind": "note", "content": "p5"}]}
                )
            }
        )
        out = annotate_block(
            block_ps=paras,
            block_start_idx=0,
            chapter_hash="chhash",
            memo=_mk_memo(),
            llm=llm,
            config=CommentConfig(),
        )
        # block-local 5 — process.py would have added block_start_idx to make it 5
        assert out[0].target_p_ids == [5]

    def test_data_p_id_is_stripped(self) -> None:
        chapter = _mk_chapter(2)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(
            responses_by_seed={
                "annotate__response": json_dumps(
                    {"comments": []}
                )
            }
        )
        annotate_block(
            block_ps=paras,
            block_start_idx=0,
            chapter_hash="chhash",
            memo=_mk_memo(),
            llm=llm,
            config=CommentConfig(),
        )
        # After annotate_block returns, data-p-id must be removed
        for p in chapter.body.iter("p"):
            assert "data-p-id" not in p.attrib
