"""Unit tests for :class:`LLMRateLimiter`.

Zero-network, single-process. Each test owns its own limiter so the
:class:`threading.Condition` and module-global abort flag never leak
between tests. The abort flag is reset in an autouse fixture.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
from tiktoken import Encoding, get_encoding

from epub_commentor.errors import CommentAbortError
from epub_commentor.llm._abort import request_abort, reset_abort
from epub_commentor.llm.rate_limiter import LLMRateLimiter
from epub_commentor.llm.types import Message, MessageRole


@pytest.fixture(autouse=True)
def _reset_abort() -> Iterator[None]:
    """Always start each test with a clean abort flag."""
    reset_abort()
    yield
    reset_abort()


def _encoding() -> Encoding:
    return get_encoding("o200k_base")


def _make_limiter(
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
    concurrency_limit: int | None = None,
    token_count_buffer: float = 1.2,
    abort_check_interval: float = 0.5,
) -> LLMRateLimiter:
    return LLMRateLimiter(
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
        concurrency_limit=concurrency_limit,
        encoding=_encoding(),
        token_count_buffer=token_count_buffer,
        abort_check_interval=abort_check_interval,
    )


class TestRPMLimit:
    def test_blocks_second_request(self) -> None:
        """Second ``acquire`` blocks while the first still holds the slot."""
        limiter = _make_limiter(rpm_limit=1)
        limiter.acquire(0)

        second_done = threading.Event()

        def second_attempt() -> None:
            limiter.acquire(0)
            second_done.set()

        thread = threading.Thread(target=second_attempt, daemon=True)
        thread.start()
        thread.join(timeout=0.5)
        assert not second_done.is_set(), "second acquire must block while first holds slot"

        # RPM window only frees after 60s (test_releases_after_60s covers
        # that path with a mocked clock). We don't release here because
        # release() only drops the concurrency semaphore — it does NOT
        # evict the rpm window, so the test would still block forever.

    def test_releases_after_60s(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_now = [1000.0]
        monkeypatch.setattr(
            "epub_commentor.llm.rate_limiter.time.monotonic",
            lambda: fake_now[0],
        )
        limiter = _make_limiter(rpm_limit=1)
        limiter.acquire(0)
        limiter.release()

        thread_done = threading.Event()

        def attempt() -> None:
            limiter.acquire(0)
            thread_done.set()

        thread = threading.Thread(target=attempt, daemon=True)
        thread.start()
        thread.join(timeout=0.5)
        assert not thread_done.is_set(), "within window, slot must still be occupied"

        fake_now[0] += 60.5
        thread.join(timeout=2.0)
        assert thread_done.is_set(), "after window expiry, slot must free up"
        limiter.release()  # cleanup


class TestTPMLimit:
    def test_blocks_when_budget_exceeded(self) -> None:
        limiter = _make_limiter(tpm_limit=100)
        # Budget is 100; first request charges 60. Release concurrency
        # but the 60 charge stays in the tpm window for 60s.
        limiter.acquire(60)
        limiter.release()

        done = threading.Event()

        def attempt() -> None:
            limiter.acquire(60)
            done.set()

        thread = threading.Thread(target=attempt, daemon=True)
        thread.start()
        thread.join(timeout=0.5)
        assert not done.is_set(), "60 + 60 must exceed 100 budget"

    def test_recovers_after_window_evicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_now = [1000.0]
        monkeypatch.setattr(
            "epub_commentor.llm.rate_limiter.time.monotonic",
            lambda: fake_now[0],
        )
        limiter = _make_limiter(tpm_limit=100)
        limiter.acquire(60)
        limiter.release()

        thread_done = threading.Event()

        def attempt() -> None:
            limiter.acquire(60)
            thread_done.set()

        thread = threading.Thread(target=attempt, daemon=True)
        thread.start()
        thread.join(timeout=0.5)
        assert not thread_done.is_set()

        fake_now[0] += 60.5
        thread.join(timeout=2.0)
        assert thread_done.is_set(), "after window expiry, budget must clear"
        limiter.release()  # cleanup


class TestConcurrencyLimit:
    def test_third_blocks_until_one_releases(self) -> None:
        limiter = _make_limiter(concurrency_limit=2)
        limiter.acquire(0)
        limiter.acquire(0)
        # Two held; third must block.

        third_done = threading.Event()

        def third() -> None:
            limiter.acquire(0)
            third_done.set()

        thread = threading.Thread(target=third, daemon=True)
        thread.start()
        thread.join(timeout=0.5)
        assert not third_done.is_set(), "third acquire must block while two are held"

        limiter.release()
        thread.join(timeout=2.0)
        assert third_done.is_set(), "third acquire must succeed after one release"
        limiter.release()
        limiter.release()  # cleanup


class TestEstimateTokens:
    def test_empty_messages_returns_one(self) -> None:
        limiter = _make_limiter()
        assert limiter.estimate_tokens([]) >= 1

    def test_simple_message_has_tokens(self) -> None:
        limiter = _make_limiter()
        messages = [Message(role=MessageRole.SYSTEM, message="hello world")]
        assert limiter.estimate_tokens(messages) >= 3

    def test_buffer_strictly_increases_estimate(self) -> None:
        no_buffer = _make_limiter(token_count_buffer=1.0)
        with_buffer = _make_limiter(token_count_buffer=2.0)
        messages = [Message(role=MessageRole.USER, message="x" * 100)]
        assert with_buffer.estimate_tokens(messages) >= no_buffer.estimate_tokens(messages)


class TestAbort:
    def test_abort_during_wait_raises(self) -> None:
        # Use a tight abort_check_interval so the test finishes quickly.
        limiter = _make_limiter(rpm_limit=1, abort_check_interval=0.05)
        limiter.acquire(0)
        limiter.release()

        aborted = threading.Event()

        def attempt() -> None:
            try:
                limiter.acquire(0)
            except CommentAbortError:
                aborted.set()

        thread = threading.Thread(target=attempt, daemon=True)
        thread.start()
        time.sleep(0.1)
        request_abort()
        thread.join(timeout=3.0)
        assert aborted.is_set(), "CommentAbortError must propagate within abort_check_interval"


class TestLLMIntegration:
    """Verify the :class:`LLM` constructor wires the rate limiter through."""

    def test_llm_constructs_rate_limiter(self) -> None:
        # No real HTTP: just construct the LLM with dummy creds and
        # assert the limiter and executor are wired together.
        from epub_commentor.llm.core import LLM

        llm = LLM(
            key="dummy",
            url="https://example.invalid/v1",
            model="m",
            token_encoding="o200k_base",
            rpm_limit=10,
            tpm_limit=200000,
            request_concurrency=2,
            token_count_buffer=1.5,
        )
        assert llm.rate_limiter._rpm_limit == 10
        assert llm.rate_limiter._tpm_limit == 200000
        assert llm.rate_limiter._concurrency_limit == 2
        assert llm.rate_limiter._token_count_buffer == 1.5

    def test_llm_default_is_unlimited(self) -> None:
        from epub_commentor.llm.core import LLM

        llm = LLM(
            key="dummy",
            url="https://example.invalid/v1",
            model="m",
            token_encoding="o200k_base",
        )
        assert llm.rate_limiter._rpm_limit is None
        assert llm.rate_limiter._tpm_limit is None
        assert llm.rate_limiter._concurrency_limit is None
        assert llm.rate_limiter._sem is None


class TestNoLimits:
    def test_all_none_is_passthrough(self) -> None:
        limiter = _make_limiter()
        start = time.monotonic()
        for _ in range(100):
            limiter.acquire(0)
            limiter.release()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"no-limit acquire/release should be ~free, took {elapsed:.3f}s"

    def test_release_with_no_concurrency_is_safe(self) -> None:
        limiter = _make_limiter()  # all None → no semaphore
        limiter.release()  # must not raise

    def test_acquire_release_pair_is_idempotent(self) -> None:
        limiter = _make_limiter(concurrency_limit=1)
        for _ in range(5):
            limiter.acquire(0)
            limiter.release()


__all__ = []
