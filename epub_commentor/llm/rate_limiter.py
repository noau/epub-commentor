"""RPM / TPM / Concurrency gate for outbound LLM HTTP requests.

Three independent limits share a single :class:`threading.Condition` so
``wait()`` re-checks every gate on notify, preventing lost wakeups:

* **RPM** — sliding 60-second window of request-start timestamps.
* **TPM** — sliding 60-second window of ``(timestamp, estimated_tokens)``
  pairs; charged against the budget using a tiktoken-estimated token
  count scaled by a safety buffer (default 1.2x).
* **Concurrency** — :class:`threading.Semaphore` gating the number of
  in-flight HTTP requests at any instant.

``acquire(estimated_tokens)`` takes the semaphore first (slow path), then
blocks on the condition until both RPM and TPM windows can fit this
request. Aborts are observed via :func:`is_abort_requested` — re-checked
between waits so Ctrl-C breaks out within ``abort_check_interval``.

Constructed with all three limits ``None`` becomes a no-op: ``acquire``
returns immediately and ``release`` is a no-op, so call sites need no
``is None`` checks at the boundary.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from collections.abc import Iterable

from tiktoken import Encoding

from ..errors import CommentAbortError
from ._abort import is_abort_requested
from .types import Message, MessageRole

_WINDOW_SECONDS: float = 60.0


def _role_to_str(role: MessageRole) -> str:
    """Map :class:`MessageRole` to the OpenAI wire-format role string."""
    if role == MessageRole.SYSTEM:
        return "system"
    if role == MessageRole.USER:
        return "user"
    if role == MessageRole.ASSISTANT:
        return "assistant"
    raise ValueError(f"Unsupported MessageRole: {role!r}")


class LLMRateLimiter:
    """Three-gate rate limiter for LLM HTTP calls.

    Parameters
    ----------
    rpm_limit:
        Max requests in any rolling 60-second window. ``None`` = no limit.
    tpm_limit:
        Max estimated tokens in any rolling 60-second window. ``None`` = no limit.
    concurrency_limit:
        Max simultaneous in-flight HTTP requests. ``None`` = no limit.
    encoding:
        tiktoken encoding used by :meth:`estimate_tokens` for TPM budgeting.
    token_count_buffer:
        Safety multiplier on top of the raw tiktoken count (default 1.2).
        Useful when the provider's tokenizer differs from tiktoken — e.g.
        Zhipu / GLM, where ``o200k_base`` is only an approximation.
    abort_check_interval:
        How often the wait loop rechecks :func:`is_abort_requested`, in
        seconds (default 0.5).
    """

    def __init__(
        self,
        rpm_limit: int | None,
        tpm_limit: int | None,
        concurrency_limit: int | None,
        encoding: Encoding,
        token_count_buffer: float = 1.2,
        abort_check_interval: float = 0.5,
    ) -> None:
        self._rpm_limit = rpm_limit
        self._tpm_limit = tpm_limit
        self._concurrency_limit = concurrency_limit
        self._encoding = encoding
        self._token_count_buffer = token_count_buffer
        self._abort_check_interval = abort_check_interval

        self._rpm_window: deque[float] = deque()
        self._tpm_window: deque[tuple[float, int]] = deque()
        self._cond = threading.Condition(threading.Lock())
        # Semaphore is created only when concurrency_limit is set; when it
        # is None we skip both acquire()/release() at call sites instead of
        # carrying an unused BoundedSemaphore around.
        self._sem: threading.Semaphore | None
        if concurrency_limit is not None:
            self._sem = threading.Semaphore(concurrency_limit)
        else:
            self._sem = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def estimate_tokens(self, messages: Iterable[Message]) -> int:
        """Estimate the token cost of ``messages`` for TPM budgeting.

        Serialises the messages to the same JSON shape the OpenAI SDK
        sends, encodes with :attr:`_encoding`, and applies the safety
        buffer. Always returns at least 1 so the budget accounting never
        silently grants zero-cost requests.
        """
        rendered = [{"role": _role_to_str(m.role), "content": m.message} for m in messages]
        text = json.dumps(rendered, ensure_ascii=False)
        raw = len(self._encoding.encode(text))
        return max(1, math.ceil(raw * self._token_count_buffer))

    def acquire(self, estimated_tokens: int) -> None:
        """Block until RPM, TPM, and concurrency all permit this request.

        Raises
        ------
        CommentAbortError
            If the abort flag becomes set while waiting.
        """
        if self._sem is not None:
            self._sem.acquire()
        try:
            self._wait_for_budget(estimated_tokens)
        except BaseException:
            # Release the semaphore if the budget wait did NOT record us,
            # so a future retry / next thread isn't blocked by our death.
            if self._sem is not None:
                self._sem.release()
            raise

    def release(self) -> None:
        """Release one concurrency slot.

        No-op when ``concurrency_limit`` is ``None``. MUST be called from
        a ``finally`` block to avoid deadlock under exceptions or
        cooperative aborts.
        """
        if self._sem is None:
            return
        self._sem.release()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _wait_for_budget(self, estimated_tokens: int) -> None:
        with self._cond:
            while True:
                if is_abort_requested():
                    raise CommentAbortError("aborted by user")
                now = time.monotonic()
                self._evict_expired(now)
                if self._rpm_fits() and self._tpm_fits(estimated_tokens):
                    self._record_acquire(now, estimated_tokens)
                    return
                self._cond.wait(timeout=self._abort_check_interval)

    def _evict_expired(self, now: float) -> None:
        """Drop timestamps older than 60s from both windows."""
        cutoff = now - _WINDOW_SECONDS
        while self._rpm_window and self._rpm_window[0] <= cutoff:
            self._rpm_window.popleft()
        while self._tpm_window and self._tpm_window[0][0] <= cutoff:
            self._tpm_window.popleft()

    def _rpm_fits(self) -> bool:
        if self._rpm_limit is None:
            return True
        return len(self._rpm_window) < self._rpm_limit

    def _tpm_fits(self, estimated_tokens: int) -> bool:
        if self._tpm_limit is None:
            return True
        current = sum(tokens for _, tokens in self._tpm_window)
        return current + estimated_tokens <= self._tpm_limit

    def _record_acquire(self, now: float, estimated_tokens: int) -> None:
        if self._rpm_limit is not None:
            self._rpm_window.append(now)
        if self._tpm_limit is not None:
            self._tpm_window.append((now, estimated_tokens))
        # Notify other waiters; they re-check both gates under the lock.
        self._cond.notify_all()


__all__ = ["LLMRateLimiter"]
