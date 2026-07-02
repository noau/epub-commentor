"""Unit tests for :mod:`epub_commentor.llm.select`.

Drives :func:`select_chapters` through a :class:`MockLLM` to verify the
book-level pre-filter contract:

- returns a parallel ``list[bool]`` mask
- returns a per-index ``reason`` dict
- retries on malformed JSON (reuses ``[[StageError]]`` logging shape)
- raises :class:`CommentSelectFailedError` after exhausting retries
- strips reserved metadata keys before showing the prompt to the LLM
"""

from __future__ import annotations

from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest

from epub_commentor import CommentConfig
from epub_commentor.config import CommentConfig as _CommentConfig
from epub_commentor.errors import CommentSelectFailedError
from epub_commentor.llm.select import (
    _book_hash,
    _chapter_preview,
    _format_select_user,
    _select_seed,
    select_chapters,
)
from epub_commentor.pipeline.extract import Chapter
from tests._mock_llm import MockLLM, json_dumps


def _mk_chapter(
    path: str, title: str, body_xml: str = "<body><p>Para one text.</p><p>Para two text.</p></body>"
) -> Chapter:
    """Build a minimal Chapter with parsed body element."""
    body = fromstring(f"<root>{body_xml}</root>").find("body")
    if body is None:
        body = fromstring(body_xml)
    # `xml_node=None` is fine for read-only flows (select_chapters only
    # calls ``plain_text(chapter.body)`` and ``chapter.body.iter("p")``).
    # The injection layer is the one that needs a live XMLLikeNode.
    return Chapter(
        path=Path(path),
        title=title,
        body=body,
        xml_node=None,  # type: ignore[arg-type]
    )


class TestSelectSeed:
    def test_seed_format_matches_convention(self) -> None:
        cfg = CommentConfig(cache_seed_user_id="alice")
        seed = _select_seed(cfg, "abc123def456")
        # Matches llm.memo._scan_seed / llm.block._annotate_seed shape.
        assert ":select:" in seed
        assert "alice" in seed
        assert "abc123def456" in seed

    def test_book_hash_is_deterministic(self) -> None:
        chapters = [_mk_chapter("c.xhtml", "A"), _mk_chapter("a.xhtml", "B"), _mk_chapter("b.xhtml", "C")]
        h1 = _book_hash(chapters)
        h2 = _book_hash(list(reversed(chapters)))
        # Same set of paths regardless of input order — critical for cache
        # stability when the spine order is re-derived between runs.
        assert h1 == h2
        assert len(h1) == 12


class TestChapterPreview:
    def test_preview_truncates_to_max_chars(self) -> None:
        long_body = "<body>" + ("<p>" + ("x" * 100) + "</p>") * 5 + "</body>"
        ch = _mk_chapter("c.xhtml", "Title", body_xml=long_body)
        preview = _chapter_preview(ch, max_chars=50)
        assert len(preview) <= 50
        assert "x" in preview

    def test_preview_normalises_whitespace(self) -> None:
        body = "<body><p>line1\n\n\nline2\t\t  line3</p></body>"
        ch = _mk_chapter("c.xhtml", "Title", body_xml=body)
        preview = _chapter_preview(ch, max_chars=200)
        assert "\n" not in preview
        assert "\t" not in preview

    def test_preview_falls_back_to_title_for_empty_body(self) -> None:
        ch = _mk_chapter("c.xhtml", "Cover Page", body_xml="<body></body>")
        preview = _chapter_preview(ch, max_chars=200)
        assert "Cover Page" in preview


class TestFormatSelectUser:
    def test_includes_index_title_preview_paragraphs(self) -> None:
        chapters = [_mk_chapter("c1.xhtml", "One"), _mk_chapter("c2.xhtml", "Two")]
        text = _format_select_user(chapters, {"author": "X"}, preview_chars=80)
        assert "0. One" in text
        assert "1. Two" in text
        assert "author: X" in text
        assert "paragraphs: 2" in text
        assert "Para one" in text

    def test_omits_reserved_metadata_keys(self) -> None:
        chapters = [_mk_chapter("c.xhtml", "T")]
        text = _format_select_user(chapters, {"author": "X", "__opf_path__": "/hidden"}, preview_chars=80)
        assert "author: X" in text
        assert "__opf_path__" not in text


class TestSelectChaptersHappyPath:
    def test_returns_parallel_bool_mask(self) -> None:
        body_json = json_dumps(
            {
                "selections": [
                    {"index": 0, "include": True, "reason": "main narrative"},
                    {"index": 1, "include": False, "reason": "structural index"},
                    {"index": 2, "include": True, "reason": "argumentative chapter"},
                ]
            }
        )
        llm = MockLLM(responses_by_seed={"select__response": body_json})
        chapters = [_mk_chapter(f"c{i}.xhtml", f"Ch{i}") for i in range(3)]
        mask, reasons = select_chapters(
            chapters=chapters,
            book_metadata={"author": "X"},
            llm=llm,
            config=_CommentConfig(ai_select_max_retries=2),
        )
        assert mask == [True, False, True]
        assert reasons[0] == "main narrative"
        assert reasons[1] == "structural index"
        assert reasons[2] == "argumentative chapter"

    def test_empty_chapter_list_returns_empty_results(self) -> None:
        llm = MockLLM()
        mask, reasons = select_chapters(
            chapters=[],
            book_metadata={},
            llm=llm,
            config=_CommentConfig(),
        )
        assert mask == []
        assert reasons == {}
        assert llm.calls == []  # never invoked


class TestSelectChaptersRetries:
    def test_retries_on_invalid_json_then_succeeds(self) -> None:
        # First call returns garbage, second returns valid JSON.
        responses = iter(
            [
                "not json at all",
                json_dumps(
                    {
                        "selections": [
                            {"index": 0, "include": True, "reason": "ok"},
                        ]
                    }
                ),
            ]
        )
        llm = MockLLM(
            responses_by_seed={"select__response": ""},  # placeholder
            default_response="",  # placeholder
        )

        # Override _route to serve from iterator
        def _iter_route(seed, messages):
            llm.call_count += 1
            return next(responses)

        llm._route = _iter_route  # type: ignore[assignment]
        chapters = [_mk_chapter("c.xhtml", "Title")]
        mask, reasons = select_chapters(
            chapters=chapters,
            book_metadata={},
            llm=llm,
            config=_CommentConfig(ai_select_max_retries=3),
        )
        assert mask == [True]
        assert reasons[0] == "ok"
        assert llm.call_count == 2

    def test_exhaustion_raises_select_failed(self) -> None:
        llm = MockLLM(default_response="garbage")
        chapters = [_mk_chapter("c.xhtml", "T")]
        with pytest.raises(CommentSelectFailedError, match="could not produce"):
            select_chapters(
                chapters=chapters,
                book_metadata={},
                llm=llm,
                config=_CommentConfig(ai_select_max_retries=2),
            )

    def test_structural_validator_failure_triggers_retry(self) -> None:
        # First call returns wrong-length selections (validator will reject),
        # second returns correct-length mask.
        responses = iter(
            [
                json_dumps({"selections": [{"index": 0, "include": True, "reason": "a"}]}),  # only 1 of 3
                json_dumps(
                    {
                        "selections": [
                            {"index": 0, "include": True, "reason": "a"},
                            {"index": 1, "include": True, "reason": "b"},
                            {"index": 2, "include": False, "reason": "c"},
                        ]
                    }
                ),
            ]
        )
        llm = MockLLM()

        def _iter_route(seed, messages):
            llm.call_count += 1
            return next(responses)

        llm._route = _iter_route  # type: ignore[assignment]
        chapters = [_mk_chapter(f"c{i}.xhtml", f"Ch{i}") for i in range(3)]
        mask, reasons = select_chapters(
            chapters=chapters,
            book_metadata={},
            llm=llm,
            config=_CommentConfig(ai_select_max_retries=3),
        )
        assert mask == [True, True, False]
        assert llm.call_count == 2


class TestSelectChaptersMetadata:
    def test_prompt_includes_stripped_metadata(self) -> None:
        body_json = json_dumps(
            {
                "selections": [
                    {"index": 0, "include": True, "reason": "ok"},
                ]
            }
        )
        llm = MockLLM(responses_by_seed={"select__response": body_json})
        chapters = [_mk_chapter("c.xhtml", "T")]
        select_chapters(
            chapters=chapters,
            book_metadata={"author": "X", "__opf_path__": "/OEBPS/content.xhtml", "title": "Book"},
            llm=llm,
            config=_CommentConfig(),
        )
        user_text = llm.calls[0].messages[1].message
        assert "author: X" in user_text
        assert "title: Book" in user_text
        assert "__opf_path__" not in user_text

    def test_prompt_uses_configured_preview_length(self) -> None:
        body_json = json_dumps(
            {
                "selections": [
                    {"index": 0, "include": True, "reason": "ok"},
                ]
            }
        )
        llm = MockLLM(responses_by_seed={"select__response": body_json})
        chapters = [_mk_chapter("c.xhtml", "T", body_xml="<body><p>" + ("x" * 500) + "</p></body>")]
        select_chapters(
            chapters=chapters,
            book_metadata={},
            llm=llm,
            config=_CommentConfig(ai_select_min_body_chars=50),
        )
        user_text = llm.calls[0].messages[1].message
        # preview is truncated to 50 chars; 500 'x's won't survive.
        assert "x" * 100 not in user_text
