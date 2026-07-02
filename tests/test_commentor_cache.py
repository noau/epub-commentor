"""Tests for ``LLMContext.discard_last()`` — the cache-eviction seam used by
retry-loop ``except`` blocks to keep invalid LLM responses out of the
on-disk cache.

These tests exercise the real :class:`LLMContext` against a stub
``_NullExecutor`` (mirroring the pattern in
:mod:`tests.test_commentor_log`). The MockLLM does not implement disk
caching, so this file is the only place where the commit/evict
semantics are verified end-to-end.
"""

from __future__ import annotations

from pathlib import Path

from epub_commentor.llm._debug_logger import make_request_logger
from epub_commentor.llm.context import LLMContext
from epub_commentor.llm.increasable import Increasable
from epub_commentor.llm.types import Message, MessageRole


class _NullExecutor:
    """Stand-in executor that records a single fixed response and exposes it.

    Each instance is configured with the response it returns; tests can
    vary it across calls by mutating ``self.next_response`` if needed.
    """

    def __init__(self, response: str = "response-body") -> None:
        self.next_response = response

    def request(self, messages, max_tokens, temperature, top_p, cache_key, logger=None):  # noqa: ARG002
        if logger is not None:
            logger.debug("[[Parameters]]:\n\t\ntemperature=None\n")
            logger.debug("[[Request]]:\nSystem:\ns\nUser:\nu\n")
            logger.debug("[[Response]]:\nresponse-body\n")
        return self.next_response


def _make_ctx(cache_path: Path | None, log_dir: Path | None) -> LLMContext:
    """Build a real :class:`LLMContext` against the supplied cache + log dirs."""

    def _create_logger():
        if log_dir is None:
            return None
        return make_request_logger(log_dir, prefix="test-request")

    return LLMContext(
        executor=_NullExecutor(),
        cache_path=cache_path,
        cache_seed_content="seed-x",
        top_p=Increasable(None),
        temperature=Increasable(None),
        create_logger=_create_logger,
    )


def _collect_log_text(log_dir: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(log_dir.glob("*.log")))


def _msgs() -> list[Message]:
    return [Message(MessageRole.SYSTEM, "s"), Message(MessageRole.USER, "u")]


class TestDiscardLastOnMiss:
    """cache miss path: this run wrote a temp file; discard must prevent commit."""

    def test_discard_prevents_temp_from_being_committed(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache"
        ctx = _make_ctx(cache_path=cache_path, log_dir=None)

        with ctx as entered:
            entered.request(_msgs())
            entered.discard_last()

        # No permanent file should exist: discard removed the temp before
        # __exit__ ran commit.
        permanent = list(cache_path.glob("*.txt"))
        assert permanent == [], f"unexpected permanent cache files: {permanent}"

    def test_discard_clears_last_cache_key(self, tmp_path: Path) -> None:
        """A second ``discard_last()`` immediately after the first must be safe."""
        cache_path = tmp_path / "cache"
        ctx = _make_ctx(cache_path=cache_path, log_dir=None)

        with ctx as entered:
            entered.request(_msgs())
            entered.discard_last()
            # Second call should not raise or touch any file.
            entered.discard_last()

        assert list(cache_path.glob("*.txt")) == []

    def test_commit_after_discard_writes_only_subsequent_requests(self, tmp_path: Path) -> None:
        """Discarding attempt 1 must not poison the cache for attempt 2."""
        cache_path = tmp_path / "cache"
        ctx = _make_ctx(cache_path=cache_path, log_dir=None)

        with ctx as entered:
            # First request — we treat its response as invalid and discard.
            entered.request(_msgs())
            entered.discard_last()
            # Second request has different messages (longer) — different
            # cache key, so it goes through normally and gets committed.
            msgs2 = _msgs() + [Message(MessageRole.ASSISTANT, "bad")]
            entered.request(msgs2)

        permanent = list(cache_path.glob("*.txt"))
        # Exactly one permanent file: attempt-2's good response.
        assert len(permanent) == 1, f"expected exactly 1 permanent file, got {permanent}"


class TestDiscardLastOnHit:
    """cache hit path: this run read an existing poisoned permanent file; discard must unlink it."""

    def test_discard_evicts_permanent_file_on_cache_hit(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache"
        # Pre-seed the cache with a poisoned entry by writing a file
        # named {cache_key}.txt under cache_path. We have to discover
        # the cache key the context will use, so run a no-discard cycle
        # first to learn it.
        ctx1 = _make_ctx(cache_path=cache_path, log_dir=None)
        with ctx1 as entered:
            entered.request(_msgs())
        # Now the permanent file exists. Re-enter with the same input
        # and verify a cache hit followed by discard removes it.
        ctx2 = _make_ctx(cache_path=cache_path, log_dir=None)
        with ctx2 as entered:
            response = entered.request(_msgs())
            assert response == "response-body"
            # discard on a hit removes the permanent file.
            entered.discard_last()

        permanent = list(cache_path.glob("*.txt"))
        assert permanent == [], f"expected poisoned entry to be evicted, found {permanent}"

    def test_re_request_after_eviction_calls_executor_fresh(self, tmp_path: Path) -> None:
        """A re-run after discard must hit the executor (not the cache)."""
        cache_path = tmp_path / "cache"
        executor = _NullExecutor(response="v1")

        def _create_logger():
            return None

        ctx1 = LLMContext(
            executor=executor,
            cache_path=cache_path,
            cache_seed_content="seed-x",
            top_p=Increasable(None),
            temperature=Increasable(None),
            create_logger=_create_logger,
        )
        with ctx1 as entered:
            entered.request(_msgs())
            entered.discard_last()

        # Swap executor response; the next request must see v2, proving
        # it went through the executor instead of replaying v1 from disk.
        executor.next_response = "v2"
        ctx2 = LLMContext(
            executor=executor,
            cache_path=cache_path,
            cache_seed_content="seed-x",
            top_p=Increasable(None),
            temperature=Increasable(None),
            create_logger=_create_logger,
        )
        with ctx2 as entered:
            response = entered.request(_msgs())
        assert response == "v2"


class TestDiscardLastIsSafeNoOp:
    """discard_last() must never raise when called outside its happy path."""

    def test_no_op_when_cache_disabled(self, tmp_path: Path) -> None:
        ctx = _make_ctx(cache_path=None, log_dir=None)
        with ctx as entered:
            entered.request(_msgs())
            # Should not raise even though cache_path is None.
            entered.discard_last()
            entered.discard_last()

    def test_no_op_before_any_request(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache"
        ctx = _make_ctx(cache_path=cache_path, log_dir=None)
        with ctx as entered:
            # No request() yet — _last_cache_key is None.
            entered.discard_last()

    def test_no_op_after_context_exit(self, tmp_path: Path) -> None:
        """Calling discard_last after __exit__ is harmless (uses None key)."""
        cache_path = tmp_path / "cache"
        ctx = _make_ctx(cache_path=cache_path, log_dir=None)
        with ctx as entered:
            entered.request(_msgs())
            entered.discard_last()
        # After __exit__, _last_cache_key has been reset to None by the
        # discard itself, and the context is closed anyway. A stray
        # post-exit call must not raise.
        ctx.discard_last()


class TestCacheEvictLogging:
    """discard_last() must write a [[CacheEvict]] section when a logger is set."""

    def test_cache_evict_section_written(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache"
        log_dir = tmp_path / "logs"
        ctx = _make_ctx(cache_path=cache_path, log_dir=log_dir)

        with ctx as entered:
            entered.request(_msgs())
            entered.discard_last()

        log_text = _collect_log_text(log_dir)
        assert "[[CacheEvict]]" in log_text
        assert "reason=validation_failed" in log_text

    def test_cache_evict_logged_alongside_cache_check(self, tmp_path: Path) -> None:
        """Both the miss (``hit=false``) and the eviction appear in one log file."""
        cache_path = tmp_path / "cache"
        log_dir = tmp_path / "logs"
        ctx = _make_ctx(cache_path=cache_path, log_dir=log_dir)

        with ctx as entered:
            entered.request(_msgs())
            entered.discard_last()

        log_text = _collect_log_text(log_dir)
        assert "[[CacheCheck]]" in log_text
        assert "hit=false" in log_text
        assert "[[CacheEvict]]" in log_text

    def test_cache_evict_not_logged_when_cache_disabled(self, tmp_path: Path) -> None:
        """Without a cache_path, discard_last is a no-op and writes nothing."""
        log_dir = tmp_path / "logs"
        ctx = _make_ctx(cache_path=None, log_dir=log_dir)

        with ctx as entered:
            entered.request(_msgs())
            entered.discard_last()

        log_text = _collect_log_text(log_dir) if log_dir.exists() else ""
        assert "[[CacheEvict]]" not in log_text
