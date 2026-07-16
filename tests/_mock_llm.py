"""Test double for :class:`~epub_commentor.llm.core.LLM`.

The pipeline code only uses two surfaces of the real LLM:

1. ``llm.template(name)`` returning a Jinja :class:`Template` (from
   :mod:`epub_commentor.template`).
2. ``llm.context(cache_seed_content=seed)`` acting as a context manager
   whose ``request([Message, ...])`` returns a string of LLM output.

Both are implemented here in plain Python, with the response keyed off
the cache seed: Stage 1 (scan) and Stage 2 (annotate) are distinguished
by the seed prefix set in :mod:`epub_commentor.llm.memo` and
:mod:`epub_commentor.llm.block`. Tests construct a :class:`MockLLM` and
hand it to :func:`process_chapters` instead of a real OpenAI-backed
client.

The mock also exposes a request log so tests can assert on call order
and message shape. When ``log_dir_path`` is supplied the mock also
writes per-context debug logs into that directory using the same
``[[Parameters]] / [[Request]] / [[Response]] / [[StageError]] /
[[FinalError]] / [[CacheCheck]]`` format as the production :class:`LLM`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import Any, Self

from jinja2 import Environment

from epub_commentor.llm._debug_logger import make_request_logger
from epub_commentor.llm.types import Message, MessageRole
from epub_commentor.template import create_env

# Cache-seed prefixes mirror the ones in memo.py / block.py / select.py /
# review.py so the mock can route the right canned response to the right
# stage. The book-level gates (select / review) use `:select:` and
# `:review:` so a cached ``select`` response can never collide with a
# cached ``review`` response — different seeds, different stages.
_SCAN_PREFIX = ":scan:"
_ANNOTATE_PREFIX = ":annotate:"
_TRANSLATE_PREFIX = ":translate:"
_SELECT_PREFIX = ":select:"
_REVIEW_PREFIX = ":review:"


@dataclass
class _MockCall:
    """One recorded call to :meth:`MockLLMContext.request`."""

    cache_seed: str | None
    messages: list[Message]
    response: str


class _MockLLMContext:
    """A no-network stand-in for :class:`LLMContext`.

    The optional ``create_logger`` factory is invoked on
    :meth:`__enter__` exactly the way :class:`LLMContext` does so
    ``ctx.logger`` returns a real Logger (or ``None`` when no log dir
    was supplied) — letting ``block.py`` / ``memo.py`` write
    ``[[StageError]]`` / ``[[FinalError]]`` sections without conditional
    branching on mock vs. production.
    """

    def __init__(
        self,
        parent: MockLLM,
        cache_seed: str | None,
        create_logger: Any | None = None,
    ) -> None:
        self._parent = parent
        self._cache_seed = cache_seed
        self._create_logger = create_logger
        self._logger: Logger | None = None

    @property
    def logger(self) -> Logger | None:
        return self._logger

    def __enter__(self) -> Self:
        if self._create_logger is not None:
            self._logger = self._create_logger()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def request(
        self,
        input: str | list[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> str:
        messages: list[Message]
        if isinstance(input, str):
            messages = [Message(role=MessageRole.USER, message=input)]
        else:
            messages = list(input)
        response = self._parent._route(self._cache_seed, messages)
        self._parent.calls.append(_MockCall(self._cache_seed, messages, response))
        return response

    def discard_last(self) -> None:
        """No-op — the mock has no on-disk cache to evict.

        Kept so :class:`ContextProtocol` is satisfied and so a future
        mock-with-disk-cache change can wire this through without
        touching any caller.
        """


class MockLLM:
    """A configurable LLM double.

    Two ways to drive responses:

    * Per-cache-seed factory: ``responses_by_seed[seed] = json_string``
      (exact match wins).
    * Catch-all: ``default_response = json_string`` (used when no seed
      matches and a default is set; otherwise :class:`AssertionError`).

    The :attr:`calls` list records every invocation for assertions.

    Parameters
    ----------
    log_dir_path:
        Optional path. When supplied, every context built by
        :meth:`context` opens a per-request debug log file under that
        directory using the shared logger factory, mirroring
        :class:`epub_commentor.llm.LLM`.
    """

    responses_by_seed: dict[str, str]
    default_response: str | None
    call_count: int
    calls: list[_MockCall]
    _env: Environment
    _log_dir_path: Path | None

    def __init__(
        self,
        responses_by_seed: dict[str, str] | None = None,
        default_response: str | None = None,
        log_dir_path: Path | str | None = None,
        # Rate-limit knobs are accepted for source-level compatibility
        # with :class:`LLM` but the mock never invokes an HTTP executor,
        # so the values are stored and ignored — production-only fields.
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        request_concurrency: int | None = None,
        token_count_buffer: float = 1.2,
    ) -> None:
        self.responses_by_seed = dict(responses_by_seed or {})
        self.default_response = default_response
        self.call_count = 0
        self.calls = []
        # Real Jinja env, so the templates can be rendered exactly the
        # way the production code does. This avoids accidental drift
        # between the mock and the real thing.
        self._env = create_env(self._prompts_path())
        self._log_dir_path = Path(log_dir_path) if log_dir_path is not None else None
        # Stash the rate-limit knobs so tests can assert on them; the
        # mock's _MockLLMContext.request never goes through an executor,
        # so no gate is ever acquired.
        self._rpm_limit = rpm_limit
        self._tpm_limit = tpm_limit
        self._request_concurrency = request_concurrency
        self._token_count_buffer = token_count_buffer

    @staticmethod
    def _prompts_path() -> Any:
        from importlib.resources import files
        from pathlib import Path

        return Path(str(files("epub_commentor"))) / "data"

    def template(self, template_name: str) -> Any:
        return self._env.get_template(template_name)

    def context(self, cache_seed_content: str | None = None) -> _MockLLMContext:
        return _MockLLMContext(self, cache_seed_content, create_logger=self._create_logger)

    def _create_logger(self) -> Logger | None:
        """Build a per-context debug logger when ``log_dir_path`` is set."""
        if self._log_dir_path is None:
            return None
        return make_request_logger(self._log_dir_path, prefix="mock-request")

    def _route(self, seed: str | None, messages: list[Message]) -> str:
        self.call_count += 1
        if seed is not None:
            # 1. Exact seed match
            if seed in self.responses_by_seed:
                return self.responses_by_seed[seed]
            # 2. Stage-prefix dispatch: keys like "scan__response" or
            #    "annotate__response" match any seed whose body contains
            #    the corresponding stage marker. The book-level gates
            #    (select / review) share the same pattern.
            stage_keys = {
                _SCAN_PREFIX: "scan__response",
                _ANNOTATE_PREFIX: "annotate__response",
                _TRANSLATE_PREFIX: "translate__response",
                _SELECT_PREFIX: "select__response",
                _REVIEW_PREFIX: "review__response",
            }
            for marker, key in stage_keys.items():
                if marker in seed and key in self.responses_by_seed:
                    return self.responses_by_seed[key]
        if self.default_response is not None:
            return self.default_response
        raise AssertionError(
            f"MockLLM has no response registered for cache_seed={seed!r}; known seeds: {sorted(self.responses_by_seed)}"
        )

    # ---- helpers used by the challenge case files ----

    @staticmethod
    def scan_seed(config_user_id: str, chapter_hash: str) -> str:
        """Reconstruct the cache seed a real Stage 1 would use."""
        # Matches llm.memo._scan_seed (without the version prefix, which the
        # mock intentionally ignores — the real prefix is opaque to routing).
        return f"commentor::scan:{config_user_id}:{chapter_hash}"

    @staticmethod
    def annotate_seed(config_user_id: str, chapter_hash: str, block_hash: str) -> str:
        """Reconstruct the cache seed a real Stage 2 would use."""
        return f"commentor::annotate:{config_user_id}:{chapter_hash}::{block_hash}"

    @staticmethod
    def translate_seed(config_user_id: str, chapter_hash: str, block_hash: str) -> str:
        """Reconstruct the cache seed a real Stage 3 would use.

        Mirrors :meth:`annotate_seed` so tests can register per-block
        canned translations without computing the block hash by hand.
        """
        return f"commentor::translate:{config_user_id}:{chapter_hash}::{block_hash}"

    @staticmethod
    def select_seed(config_user_id: str, book_hash: str) -> str:
        """Reconstruct the cache seed a real ``--ai-select`` would use.

        Mirrors :func:`epub_commentor.llm.select._select_seed` (with the
        version prefix omitted — the mock ignores the opaque prefix).
        """
        return f"commentor::select:{config_user_id}:{book_hash}"

    @staticmethod
    def review_seed(config_user_id: str, book_hash: str) -> str:
        """Reconstruct the cache seed a real ``--ai-review`` would use.

        Mirrors :func:`epub_commentor.llm.review._review_seed`.
        """
        return f"commentor::review:{config_user_id}:{book_hash}"


def json_dumps(obj: Any) -> str:
    """Serialise ``obj`` to a JSON string the LLM can echo back."""
    return json.dumps(obj, ensure_ascii=False)


__all__ = ["MockLLM", "json_dumps"]
