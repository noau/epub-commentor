"""Unit tests for :mod:`epub_commentor.llm.review`.

Drives :func:`review_annotations` through a :class:`MockLLM` to verify the
book-level post-filter contract.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest

from epub_commentor.config import CommentConfig
from epub_commentor.errors import CommentReviewFailedError
from epub_commentor.llm.review import (
    _book_hash_from_annotations,
    _format_review_user,
    _is_skipped_memo,
    _memo_summary,
    _review_seed,
    review_annotations,
)
from epub_commentor.llm.schema import ChapterMemo, CommentItem, CommentKind, CommentPosition
from epub_commentor.pipeline.extract import Chapter
from epub_commentor.pipeline.process import ChapterAnnotation
from tests._mock_llm import MockLLM, json_dumps


def _mk_chapter(path: str, title: str = "stub") -> Chapter:
    body = fromstring("<html><body><p>Para one text.</p><p>Para two text.</p></body></html>").find("body")
    assert body is not None
    return Chapter(path=Path(path), title=title, body=body, xml_node=None)  # type: ignore[arg-type]


def _mk_memo(thesis: str = "A chapter about things.") -> ChapterMemo:
    return ChapterMemo(
        core_thesis=thesis,
        outline=["topic 1", "topic 2", "topic 3"],
        tone="analytical",
        target_audience="general",
    )


def _skipped_memo() -> ChapterMemo:
    """Placeholder memo produced by ``process._empty_memo``."""
    return ChapterMemo(
        core_thesis="(chapter skipped — no <p> elements)",
        outline=["(skipped)", "(skipped)", "(skipped)"],
        tone="(unknown)",
        target_audience="(unknown)",
    )


def _mk_annotation(
    path: str,
    title: str,
    memo: ChapterMemo,
    n_comments: int = 2,
) -> ChapterAnnotation:
    comments = [
        CommentItem(
            target_p_ids=[0],
            position=CommentPosition.BEFORE,
            kind=CommentKind.NOTE,
            content=f"annotation for {title} #{i}",
        )
        for i in range(n_comments)
    ]
    return ChapterAnnotation(chapter=_mk_chapter(path, title), memo=memo, comments=comments)


class TestReviewSeed:
    def test_seed_format_matches_convention(self) -> None:
        cfg = CommentConfig(cache_seed_user_id="bob")
        seed = _review_seed(cfg, "abc123def456")
        assert ":review:" in seed
        assert "bob" in seed
        assert "abc123def456" in seed

    def test_book_hash_is_deterministic(self) -> None:
        ann1 = _mk_annotation("a.xhtml", "A", _mk_memo())
        ann2 = _mk_annotation("b.xhtml", "B", _mk_memo())
        h1 = _book_hash_from_annotations([ann1, ann2])
        h2 = _book_hash_from_annotations([ann2, ann1])
        assert h1 == h2
        assert len(h1) == 12


class TestIsSkippedMemo:
    def test_detects_placeholder_prefix(self) -> None:
        assert _is_skipped_memo(_skipped_memo()) is True

    def test_does_not_flag_normal_memo(self) -> None:
        assert _is_skipped_memo(_mk_memo()) is False


class TestMemoSummary:
    def test_includes_thesis_and_outline(self) -> None:
        m = ChapterMemo(
            core_thesis="A thesis.",
            outline=["a", "b", "c"],
            tone="t",
            target_audience="g",
        )
        s = _memo_summary(m, max_chars=200)
        assert "A thesis." in s
        assert "a" in s and "b" in s and "c" in s

    def test_truncates_long_memo(self) -> None:
        m = ChapterMemo(
            core_thesis="x" * 500,
            outline=["a", "b", "c"],
            tone="t",
            target_audience="g",
        )
        s = _memo_summary(m, max_chars=50)
        assert len(s) <= 50


class TestFormatReviewUser:
    def test_omits_reserved_metadata_keys(self) -> None:
        anns = [_mk_annotation("c.xhtml", "T", _mk_memo())]
        text = _format_review_user(anns, {"author": "X", "__opf_path__": "/hidden"})
        assert "author: X" in text
        assert "__opf_path__" not in text

    def test_includes_memo_and_comment_snippets(self) -> None:
        anns = [_mk_annotation("c.xhtml", "T", _mk_memo(), n_comments=2)]
        text = _format_review_user(anns, {})
        assert "memo:" in text
        assert "kind=note" in text
        assert "annotation for T" in text


class TestReviewAnnotationsEmptyInput:
    def test_empty_list_returns_empty_results(self) -> None:
        llm = MockLLM()
        mask, reasons = review_annotations(
            annotations=[],
            book_metadata={},
            llm=llm,
            config=CommentConfig(),
        )
        assert mask == []
        assert reasons == {}
        assert llm.calls == []  # never invoked


class TestReviewAnnotationsHappyPath:
    def test_returns_parallel_bool_mask(self) -> None:
        body_json = json_dumps(
            {
                "selections": [
                    {"chapter_index": 0, "include": True, "reason": "good annotations"},
                    {"chapter_index": 1, "include": False, "reason": "thin content"},
                ]
            }
        )
        llm = MockLLM(responses_by_seed={"review__response": body_json})
        anns = [
            _mk_annotation("c0.xhtml", "Ch0", _mk_memo(), n_comments=3),
            _mk_annotation("c1.xhtml", "Ch1", _mk_memo(), n_comments=3),
        ]
        mask, reasons = review_annotations(
            annotations=anns,
            book_metadata={"author": "X"},
            llm=llm,
            config=CommentConfig(ai_review_max_retries=2),
        )
        assert mask == [True, False]
        assert reasons[0] == "good annotations"
        assert reasons[1] == "thin content"


class TestReviewAnnotationsAutoDrop:
    def test_placeholder_memo_dropped_without_llm_call(self) -> None:
        # Register a response for the *consulted subset* (1 chapter here):
        # the placeholder chapter is auto-dropped and never reaches the LLM,
        # but the real chapter does, so we still need a response.
        body_json = json_dumps(
            {
                "selections": [
                    {"chapter_index": 0, "include": True, "reason": "kept"},
                ]
            }
        )
        llm = MockLLM(responses_by_seed={"review__response": body_json})
        anns = [
            _mk_annotation("c0.xhtml", "Ch0", _skipped_memo(), n_comments=0),
            _mk_annotation("c1.xhtml", "Ch1", _mk_memo(), n_comments=3),
        ]
        mask, reasons = review_annotations(
            annotations=anns,
            book_metadata={},
            llm=llm,
            config=CommentConfig(),
        )
        assert mask == [False, True]
        assert "skipped at Stage 1" in reasons[0]
        assert reasons[1] == "kept"
        # Exactly one chapter consulted → exactly one LLM call.
        assert len(llm.calls) == 1

    def test_all_zero_comments_skips_llm_completely(self) -> None:
        llm = MockLLM()
        anns = [
            _mk_annotation("c0.xhtml", "Ch0", _mk_memo(), n_comments=0),
            _mk_annotation("c1.xhtml", "Ch1", _mk_memo(), n_comments=0),
        ]
        mask, reasons = review_annotations(
            annotations=anns,
            book_metadata={},
            llm=llm,
            config=CommentConfig(ai_review_min_comments_per_chapter=1),
        )
        assert mask == [False, False]
        assert llm.calls == []  # everything auto-dropped, no network call


class TestReviewAnnotationsRetries:
    def test_retries_on_invalid_json_then_succeeds(self) -> None:
        responses = iter(
            [
                "garbage",
                json_dumps(
                    {
                        "selections": [
                            {"chapter_index": 0, "include": True, "reason": "ok"},
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
        anns = [_mk_annotation("c.xhtml", "T", _mk_memo(), n_comments=2)]
        mask, reasons = review_annotations(
            annotations=anns,
            book_metadata={},
            llm=llm,
            config=CommentConfig(ai_review_max_retries=3),
        )
        assert mask == [True]
        assert reasons[0] == "ok"
        assert llm.call_count == 2

    def test_exhaustion_raises_review_failed(self) -> None:
        llm = MockLLM(default_response="garbage")
        anns = [_mk_annotation("c.xhtml", "T", _mk_memo(), n_comments=2)]
        with pytest.raises(CommentReviewFailedError, match="could not produce"):
            review_annotations(
                annotations=anns,
                book_metadata={},
                llm=llm,
                config=CommentConfig(ai_review_max_retries=2),
            )


class TestReviewAnnotationsMetadata:
    def test_prompt_strips_reserved_metadata_keys(self) -> None:
        body_json = json_dumps(
            {
                "selections": [
                    {"chapter_index": 0, "include": True, "reason": "ok"},
                ]
            }
        )
        llm = MockLLM(responses_by_seed={"review__response": body_json})
        anns = [_mk_annotation("c.xhtml", "T", _mk_memo(), n_comments=2)]
        review_annotations(
            annotations=anns,
            book_metadata={"author": "X", "__opf_path__": "/OEBPS/content.xhtml", "title": "Book"},
            llm=llm,
            config=CommentConfig(),
        )
        user_text = llm.calls[0].messages[1].message
        assert "author: X" in user_text
        assert "title: Book" in user_text
        assert "__opf_path__" not in user_text
