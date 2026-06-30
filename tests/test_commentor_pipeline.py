"""End-to-end tests for :mod:`epub_commentor.pipeline.process` driven by
the :class:`MockLLM` test double.

Covers:

* Full chapter processing produces a :class:`ChapterAnnotation` with
  the expected memo and comments.
* Block-local p_ids in Stage 2 are translated to absolute paragraph
  indices in the returned ``ChapterAnnotation.comments``.
* Multi-block chapters handle the offset correctly.
"""

from __future__ import annotations

import io
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest
from _mock_llm import MockLLM, json_dumps

from epub_commentor.config import CommentConfig
from epub_commentor.errors import CommentInvalidJSONError
from epub_commentor.pipeline.extract import Chapter
from epub_commentor.pipeline.process import ChapterAnnotation, process_chapters
from epub_commentor.xml.xml_like import XMLLikeNode


def _mk_chapter(n_paragraphs: int, path: str = "ch.xhtml") -> Chapter:
    body_xml = "".join(f"<p>p{i}</p>" for i in range(n_paragraphs))
    root = fromstring(f"<html><body>{body_xml}</body></html>")
    body = root.find("body")
    assert body is not None
    xml_node = XMLLikeNode(io.BytesIO(b"<html></html>"), is_html_like=True)
    xml_node.element = root
    return Chapter(path=Path(path), title=path, body=body, xml_node=xml_node)


def _memo_json() -> str:
    return json_dumps(
        {
            "core_thesis": "x",
            "outline": ["a", "b", "c"],
            "tone": "t",
            "target_audience": "g",
        }
    )


class TestProcessChapters:
    def test_single_chapter_single_block(self) -> None:
        chapter = _mk_chapter(3)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "c"}]}
                ),
            }
        )
        anns = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert len(anns) == 1
        ann = anns[0]
        assert isinstance(ann, ChapterAnnotation)
        assert ann.memo.core_thesis == "x"
        assert len(ann.comments) == 1
        assert ann.comments[0].target_p_ids == [0]

    def test_block_local_p_ids_translated_to_absolute(self) -> None:
        # 8 paragraphs, block_size=4 -> 2 blocks of (p0..p3) and (p4..p7)
        # The mock Stage 2 returns block-local p_id=0 for block 1, which
        # should land at absolute index 4.
        chapter = _mk_chapter(8)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "c"}]}
                ),
            }
        )
        anns = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig(block_size=4))
        # Both blocks return one comment; the block-0 comment is at p0,
        # the block-1 comment is at p4 (absolute).
        abs_pids = sorted(c.target_p_ids[0] for c in anns[0].comments)
        assert abs_pids == [0, 4]

    def test_comments_sorted_by_p_id(self) -> None:
        chapter = _mk_chapter(8)
        # Block 0 returns a comment on p_id 3, Block 1 on p_id 0.
        # Both blocks share the same `annotate__response` because the
        # mock dispatches by stage prefix. The contents are identical
        # ("samelabel") for both; what we verify is the absolute p_id
        # translation: block-local 3 in block 0 -> absolute 3, and
        # block-local 0 in block 1 -> absolute 4.
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "samelabel"}]}
                ),
            }
        )
        anns = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig(block_size=4))
        # 2 comments (one per block), sorted by absolute p_id
        abs_pids = [c.target_p_ids[0] for c in anns[0].comments]
        assert abs_pids == [0, 4]
        # All comments carry the same content
        assert all(c.content == "samelabel" for c in anns[0].comments)

    def test_invalid_block_raises_invalid_json(self) -> None:
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
            },
            default_response="not json",
        )
        with pytest.raises(CommentInvalidJSONError):
            process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig(max_json_retries=2))

    def test_empty_chapter_skipped_by_default(self) -> None:
        # 0 paragraphs
        chapter = _mk_chapter(0)
        llm = MockLLM()  # nothing should be called
        anns = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert len(anns) == 1
        assert anns[0].comments == []
        assert anns[0].memo.core_thesis.startswith("(chapter skipped")
