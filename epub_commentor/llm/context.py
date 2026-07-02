import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from logging import Logger
from pathlib import Path
from typing import Self

from ..errors import CommentAbortError
from ._abort import is_abort_requested
from .executor import LLMExecutor
from .increasable import Increasable, Increaser
from .types import Message, MessageRole

# Global lock for cache file commit operations
_CACHE_COMMIT_LOCK = threading.Lock()


class LLMContext:
    def __init__(
        self,
        executor: LLMExecutor,
        cache_path: Path | None,
        cache_seed_content: str | None,
        top_p: Increasable,
        temperature: Increasable,
        create_logger: Callable[[], Logger | None] | None = None,
    ) -> None:
        self._executor = executor
        self._cache_path = cache_path
        self._cache_seed_content = cache_seed_content
        self._top_p: Increaser = top_p.context()
        self._temperature: Increaser = temperature.context()
        self._context_id = uuid.uuid4().hex[:12]
        self._temp_files: set[Path] = set()
        # Tracks the cache key of the most recent ``request()`` call so
        # retry loops can drop the corresponding entry on validation
        # failure (see :meth:`discard_last`). Reset to ``None`` after
        # each ``request()`` so a stale key from a prior attempt cannot
        # accidentally evict a still-valid response.
        self._last_cache_key: str | None = None
        # ``create_logger`` returns a fresh FileHandler each invocation;
        # we call it once per context so retries share one log file.
        self._create_logger = create_logger
        self._logger: Logger | None = None
        # Ensure the cache directory exists so callers that construct
        # ``LLMContext`` directly (e.g. tests) don't have to mkdir first.
        if self._cache_path is not None and not self._cache_path.exists():
            self._cache_path.mkdir(parents=True, exist_ok=True)

    @property
    def logger(self) -> Logger | None:
        """Debug logger for this context (None when no log_dir_path is set).

        Stage 1 / Stage 2 error paths use this to write ``[[StageError]]``
        and ``[[FinalError]]`` sections into the same file that the
        executor writes ``[[Parameters]]/[[Request]]/[[Response]]``
        into, so a post-mortem of a bad run needs only one file per
        chapter or block.
        """
        return self._logger

    def __enter__(self) -> Self:
        if self._create_logger is not None:
            self._logger = self._create_logger()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            # Success: commit all temporary cache files
            self._commit()
        else:
            # Failure: rollback (delete) all temporary cache files
            self._rollback()

    def request(
        self,
        input: str | list[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> str:
        # Short-circuit before doing any work: if the user has asked
        # to abort, the executor would only see a wasted network call.
        if is_abort_requested():
            raise CommentAbortError("aborted by user")

        messages: list[Message]
        if isinstance(input, str):
            messages = [Message(role=MessageRole.USER, message=input)]
        else:
            messages = input

        try:
            cache_key: str | None = None
            if self._cache_path is not None:
                cache_key = self._compute_messages_hash(messages)
                # Remember this key so a subsequent ``discard_last()`` (e.g.
                # called from a retry-loop except block) can target the same
                # entry — regardless of whether the current call hit or
                # missed the cache.
                self._last_cache_key = cache_key
                permanent_cache_file = self._cache_path / f"{cache_key}.txt"
                if permanent_cache_file.exists():
                    if self._logger is not None:
                        self._logger.debug(f"[[CacheCheck]] cache_key={cache_key[:12]}; hit=true\n")
                    cached_content = permanent_cache_file.read_text(encoding="utf-8")
                    return cached_content

            if self._logger is not None:
                short_key = cache_key[:12] if cache_key is not None else "(disabled)"
                self._logger.debug(f"[[CacheCheck]] cache_key={short_key}; hit=false\n")

            if temperature is None:
                temperature = self._temperature.current
            if top_p is None:
                top_p = self._top_p.current

            # Make the actual request
            response = self._executor.request(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                cache_key=cache_key,
                logger=self._logger,
            )
            # Save to temporary cache if cache_path is set
            if self._cache_path is not None and cache_key is not None:
                temp_cache_file = self._cache_path / f"{cache_key}.{self._context_id}.txt"
                if temp_cache_file.exists():
                    temp_cache_file.unlink()
                temp_cache_file.write_text(response, encoding="utf-8")
                self._temp_files.add(temp_cache_file)

            return response

        finally:
            self._temperature.increase()
            self._top_p.increase()

    def _compute_messages_hash(self, messages: list[Message]) -> str:
        messages_dict = [{"role": msg.role.value, "message": msg.message} for msg in messages]
        hash_data = {
            "messages": messages_dict,
            "cache_seed": self._cache_seed_content,
        }
        hash_json = json.dumps(hash_data, ensure_ascii=False, sort_keys=True)
        return hashlib.sha512(hash_json.encode("utf-8")).hexdigest()

    def _commit(self) -> None:
        for temp_file in sorted(self._temp_files):
            if temp_file.exists():
                # Remove the .[context-id].txt suffix to get permanent name
                permanent_name = temp_file.name.rsplit(".", 2)[0] + ".txt"
                permanent_file = temp_file.parent / permanent_name

                with _CACHE_COMMIT_LOCK:  # 多线程下的线程安全
                    if permanent_file.exists():
                        temp_file.unlink()
                    else:
                        temp_file.rename(permanent_file)

    def _rollback(self) -> None:
        for temp_file in self._temp_files:
            if temp_file.exists():
                temp_file.unlink()

    def discard_last(self) -> None:
        """Discard the cache entry for the most recent :meth:`request` call.

        Used by retry-loop except blocks (Stage 1/2 + AI gates) to keep
        invalid LLM responses from poisoning the on-disk cache. Two
        scenarios are covered:

        - **Cache miss (this run wrote a temp file)** — the temp file is
          removed from :attr:`_temp_files` and unlinked on disk so
          :meth:`_commit` cannot rename it to a permanent entry.
        - **Cache hit (this run read an existing poisoned permanent
          file)** — that permanent ``{cache_key}.txt`` is unlinked so a
          future run gets a fresh response from the executor instead of
          replaying the historical garbage.

        A no-op when caching is disabled, or before any ``request()``
        has been issued, or after a previous ``discard_last()`` cleared
        the key. Writes a ``[[CacheEvict]]`` section to the per-context
        logger so a post-mortem of a failed run shows the eviction
        alongside the existing ``[[StageError]]`` / ``[[FinalError]]``
        entries.
        """
        if self._cache_path is None or self._last_cache_key is None:
            return
        cache_key = self._last_cache_key
        # Serialise against ``_commit`` so a concurrent context cannot
        # rename a temp file we're about to unlink (and vice versa).
        with _CACHE_COMMIT_LOCK:
            permanent_file = self._cache_path / f"{cache_key}.txt"
            if permanent_file.exists():
                permanent_file.unlink()
            temp_file = self._cache_path / f"{cache_key}.{self._context_id}.txt"
            self._temp_files.discard(temp_file)
            if temp_file.exists():
                temp_file.unlink()
        if self._logger is not None:
            self._logger.warning(f"[[CacheEvict]] cache_key={cache_key[:12]}; reason=validation_failed\n")
        # Clear so a subsequent stray ``discard_last()`` is a safe no-op.
        self._last_cache_key = None
