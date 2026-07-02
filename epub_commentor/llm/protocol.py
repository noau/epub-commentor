"""Structural typing for the LLM.

:mod:`epub_commentor.pipeline.process` and :mod:`epub_commentor.llm.block`
only use a small slice of :class:`~epub_commentor.llm.core.LLM` — a
``template(name)`` accessor and a ``context(seed)`` context manager. We
expose that surface as a :class:`Protocol` so test doubles (see
:mod:`tests._mock_llm`) can satisfy pyright without inheriting from the
real class.

The ``Context`` and ``ContextProtocol`` types are also defined here so
test doubles don't need to subclass the real :class:`LLMContext` (which
requires an executor, cache path and Increasable).
"""

from __future__ import annotations

from logging import Logger
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from jinja2 import Template


class ContextProtocol(Protocol):
    """The minimal context-manager surface used by Stage 1 / Stage 2."""

    def __enter__(self) -> ContextProtocol: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    def request(self, input: Any = None, **kwargs: Any) -> str: ...

    def discard_last(self) -> None:
        """Drop the cache entry written or read by the most recent ``request()``.

        No-op when caching is disabled or before any ``request()`` has
        been issued. Retry loops (Stage 1 / Stage 2 / AI gates) call this
        from their ``except`` block to prevent invalid responses from
        poisoning the on-disk cache.
        """
        ...

    @property
    def logger(self) -> Logger | None: ...


class LLMProtocol(Protocol):
    """The minimal LLM surface the pipeline depends on."""

    def template(self, template_name: str) -> Template: ...

    def context(self, cache_seed_content: str | None = None) -> ContextProtocol: ...


__all__ = ["ContextProtocol", "LLMProtocol"]
