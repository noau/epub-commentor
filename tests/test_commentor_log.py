"""Tests for the debug-logging sections emitted by the LLM pipeline.

When ``MockLLM(log_dir_path=...)`` is supplied the mock opens the same
per-context debug log files as production. These tests exercise the
``[[StageError]]`` / ``[[FinalError]]`` / ``[[CacheCheck]]`` sections
that ``block.py`` / ``memo.py`` / ``context.py`` now write, without
ever touching the network.
"""

from __future__ import annotations

import io
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest
from _mock_llm import MockLLM, json_dumps

from epub_commentor.config import CommentConfig
from epub_commentor.errors import CommentInvalidJSONError
from epub_commentor.pipeline.process import process_chapters
from epub_commentor.xml.xml_like import XMLLikeNode


def _mk_chapter(n_paragraphs: int, path: str = "ch.xhtml"):
    body_xml = "".join(f"<p>p{i}</p>" for i in range(n_paragraphs))
    root = fromstring(f"<html><body>{body_xml}</body></html>")
    body = root.find("body")
    assert body is not None
    xml_node = XMLLikeNode(io.BytesIO(b"<html></html>"), is_html_like=True)
    xml_node.element = root
    from epub_commentor.pipeline.extract import Chapter

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


def _collect_log_text(log_dir: Path) -> str:
    """Concatenate every ``*.log`` under ``log_dir``."""
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(log_dir.glob("*.log")))


class TestStageErrorLogging:
    def test_block_stage_error_emitted_on_validation_failure(self, tmp_path: Path) -> None:
        """Stage 2 retries must record ``[[StageError]]`` and ``[[FinalError]]``."""
        log_dir = tmp_path / "logs"
        chapter = _mk_chapter(2)
        # scan succeeds, annotate fails with garbage → max_json_retries=2
        # so we expect 1 [[StageError]] line and 1 [[FinalError]] line.
        llm = MockLLM(
            responses_by_seed={"scan__response": _memo_json()},
            default_response="not json",
            log_dir_path=log_dir,
        )

        with pytest.raises(CommentInvalidJSONError):
            process_chapters(
                [chapter],
                book_metadata={},
                llm=llm,
                config=CommentConfig(max_json_retries=2, fail_on_block_error=True),
            )

        # Scan creates its own mock-request *.log; annotate creates
        # another (with the failed response). StageError + FinalError
        # sections live in the annotate one.
        log_text = _collect_log_text(log_dir)
        assert "[[StageError]]" in log_text
        assert "stage=annotate" in log_text
        assert "[[FinalError]]" in log_text
        assert "attempts_exhausted=true" in log_text

    def test_block_stage_error_records_raw_excerpt(self, tmp_path: Path) -> None:
        """The truncated raw response must appear under ``Raw excerpt:``."""
        log_dir = tmp_path / "logs"
        chapter = _mk_chapter(2)
        # We want the raw text distinguishable in the log so we can
        # assert on its presence.
        raw_marker = "RAW_MARKER_SENTINEL_42"
        llm = MockLLM(
            responses_by_seed={"scan__response": _memo_json()},
            default_response=f"{raw_marker} {{not json}}",
            log_dir_path=log_dir,
        )

        with pytest.raises(CommentInvalidJSONError):
            process_chapters(
                [chapter],
                book_metadata={},
                llm=llm,
                config=CommentConfig(max_json_retries=1, fail_on_block_error=True),
            )

        log_text = _collect_log_text(log_dir)
        assert raw_marker in log_text


class TestCacheCheckLogging:
    def test_cache_check_section_written_when_cache_path_set(self, tmp_path: Path) -> None:
        """When cache_path is set on the mock, ``[[CacheCheck]]`` is logged."""
        # The mock context does not currently implement the cache
        # short-circuit (that lives on the real LLMContext), but the
        # mock does go through the same ``LLMContext`` codepath when
        # the production LLM is used. We test the section format here
        # by importing the real LLM with a stubbed executor, skipping
        # the network path entirely.
        from epub_commentor.llm.context import LLMContext
        from epub_commentor.llm.increasable import Increasable
        from epub_commentor.llm.types import Message, MessageRole

        cache_path = tmp_path / "cache"
        log_dir = tmp_path / "logs"

        class _NullExecutor:
            """Stand-in executor that records a single fixed response."""

            def request(self, messages, max_tokens, temperature, top_p, cache_key, logger=None):  # noqa: ARG002
                if logger is not None:
                    logger.debug("[[Parameters]]:\n\t\ntemperature=None\n")
                    logger.debug("[[Request]]:\nSystem:\ns\nUser:\nu\n")
                    logger.debug("[[Response]]:\nresponse-body\n")
                return "response-body"

        ctx = LLMContext(
            executor=_NullExecutor(),
            cache_path=cache_path,
            cache_seed_content="seed-x",
            top_p=Increasable(None),
            temperature=Increasable(None),
            create_logger=lambda: _make_logger(log_dir),
        )

        with ctx as entered:
            entered.request(
                [Message(MessageRole.SYSTEM, "s"), Message(MessageRole.USER, "u")],
            )

        log_text = _collect_log_text(log_dir)
        assert "[[CacheCheck]]" in log_text
        assert "hit=false" in log_text


class TestCacheEvictAlongsideStageError:
    """Verify ``[[CacheEvict]]`` lands in the same log file as ``[[StageError]]``.

    Simulates the production retry-loop pattern manually against a real
    :class:`LLMContext` + stub executor: the LLM returns garbage every
    time, the retry-loop calls ``ctx.discard_last()`` after each failed
    validation, and we confirm the per-request log file ends up with
    one ``[[StageError]]`` + one ``[[CacheEvict]]`` per attempt.
    """

    def test_cache_evict_written_alongside_stage_error(self, tmp_path: Path) -> None:
        from pydantic import BaseModel, ValidationError

        from epub_commentor.llm.context import LLMContext
        from epub_commentor.llm.increasable import Increasable
        from epub_commentor.llm.types import Message, MessageRole

        class _StrictModel(BaseModel):
            """A model the executor's garbage response will never satisfy."""

            must_be_present: str

        class _GarbageExecutor:
            def request(self, messages, max_tokens, temperature, top_p, cache_key, logger=None):  # noqa: ARG002
                if logger is not None:
                    logger.debug("[[Parameters]]:\n\t\ntemperature=None\n")
                    logger.debug("[[Request]]:\nSystem:\ns\nUser:\nu\n")
                    logger.debug("[[Response]]:\nresponse-body\n")
                return "not json at all"  # always fails validation

        cache_path = tmp_path / "cache"
        log_dir = tmp_path / "logs"
        ctx = LLMContext(
            executor=_GarbageExecutor(),
            cache_path=cache_path,
            cache_seed_content="seed-x",
            top_p=Increasable(None),
            temperature=Increasable(None),
            create_logger=lambda: _make_logger(log_dir),
        )

        with ctx as entered:
            for attempt in range(2):
                raw = entered.request([Message(MessageRole.SYSTEM, "s"), Message(MessageRole.USER, "u")])
                try:
                    _StrictModel.model_validate_json(raw)
                except ValidationError as exc:
                    if entered.logger is not None:
                        entered.logger.warning(
                            f"[[StageError]] stage=annotate; "
                            f"attempt={attempt + 1}/2; "
                            f"error={type(exc).__name__}: {exc}\n"
                        )
                    # Mirror the production retry loop's eviction call.
                    entered.discard_last()

        log_text = _collect_log_text(log_dir)
        # Both sections coexist in the same log file.
        assert log_text.count("[[StageError]]") == 2
        assert log_text.count("[[CacheEvict]]") == 2
        assert "reason=validation_failed" in log_text
        # And the cache stayed empty — no permanent file was committed.
        assert list(cache_path.glob("*.txt")) == []


def _make_logger(log_dir: Path):
    """Build a debug logger against ``log_dir`` using the shared helper."""
    from epub_commentor.llm._debug_logger import make_request_logger

    return make_request_logger(log_dir, prefix="test-request")
