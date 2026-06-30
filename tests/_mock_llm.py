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
and message shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Self

from jinja2 import Environment

from epub_commentor.llm.types import Message, MessageRole
from epub_commentor.template import create_env

# Cache-seed prefixes mirror the ones in memo.py / block.py so the mock
# can route the right canned response to the right stage.
_SCAN_PREFIX = ":scan:"
_ANNOTATE_PREFIX = ":annotate:"


@dataclass
class _MockCall:
    """One recorded call to :meth:`MockLLMContext.request`."""

    cache_seed: str | None
    messages: list[Message]
    response: str


class _MockLLMContext:
    """A no-network stand-in for :class:`LLMContext`."""

    def __init__(self, parent: MockLLM, cache_seed: str | None) -> None:
        self._parent = parent
        self._cache_seed = cache_seed

    def __enter__(self) -> Self:
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


class MockLLM:
    """A configurable LLM double.

    Two ways to drive responses:

    * Per-cache-seed factory: ``responses_by_seed[seed] = json_string``
      (exact match wins).
    * Catch-all: ``default_response = json_string`` (used when no seed
      matches and a default is set; otherwise :class:`AssertionError`).

    The :attr:`calls` list records every invocation for assertions.
    """

    responses_by_seed: dict[str, str]
    default_response: str | None
    call_count: int
    calls: list[_MockCall]
    _env: Environment

    def __init__(
        self,
        responses_by_seed: dict[str, str] | None = None,
        default_response: str | None = None,
    ) -> None:
        self.responses_by_seed = dict(responses_by_seed or {})
        self.default_response = default_response
        self.call_count = 0
        self.calls = []
        # Real Jinja env, so the templates can be rendered exactly the
        # way the production code does. This avoids accidental drift
        # between the mock and the real thing.
        self._env = create_env(self._prompts_path())

    @staticmethod
    def _prompts_path() -> Any:
        from importlib.resources import files
        from pathlib import Path

        return Path(str(files("epub_commentor"))) / "data"

    def template(self, template_name: str) -> Any:
        return self._env.get_template(template_name)

    def context(self, cache_seed_content: str | None = None) -> _MockLLMContext:
        return _MockLLMContext(self, cache_seed_content)

    def _route(self, seed: str | None, messages: list[Message]) -> str:
        self.call_count += 1
        if seed is not None:
            # 1. Exact seed match
            if seed in self.responses_by_seed:
                return self.responses_by_seed[seed]
            # 2. Stage-prefix dispatch: keys like "scan__response" or
            #    "annotate__response" match any seed whose body contains
            #    the corresponding stage marker.
            stage_keys = {
                _SCAN_PREFIX: "scan__response",
                _ANNOTATE_PREFIX: "annotate__response",
            }
            for marker, key in stage_keys.items():
                if marker in seed and key in self.responses_by_seed:
                    return self.responses_by_seed[key]
        if self.default_response is not None:
            return self.default_response
        raise AssertionError(
            f"MockLLM has no response registered for cache_seed={seed!r}; "
            f"known seeds: {sorted(self.responses_by_seed)}"
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


def json_dumps(obj: Any) -> str:
    """Serialise ``obj`` to a JSON string the LLM can echo back."""
    return json.dumps(obj, ensure_ascii=False)


__all__ = ["MockLLM", "json_dumps"]
