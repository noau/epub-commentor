"""Shared per-request debug-logger factory.

Both :class:`epub_commentor.llm.LLM` (production) and
:class:`tests._mock_llm.MockLLM` (tests) need to write per-request log
files with the same format (``[[Parameters]]`` / ``[[Request]]`` /
``[[Response]]`` / ``[[CacheCheck]]`` / ``[[StageError]]`` /
``[[FinalError]]`` sections). This module keeps the timestamp collision
logic and the FileHandler construction in one place so the two
implementations don't drift.

The helpers here are private — callers should use
:func:`make_request_logger` and :func:`cache_check_logger_factory` from
the LLM or MockLLM constructors.
"""

from __future__ import annotations

import datetime
import threading
from logging import DEBUG, FileHandler, Formatter, Logger, getLogger
from pathlib import Path

# Module-level state for second-collision suffix allocation.
# Shared across LLM and MockLLM in the same Python process so that
# production / test logs do not collide when both run together.
_LOGGER_LOCK = threading.Lock()
_LAST_TIMESTAMP: str | None = None
_LOGGER_SUFFIX_ID: int = 1


def _ensure_dir(path: Path | None) -> Path | None:
    if path is None:
        return None
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        return None
    return path.resolve()


def make_request_logger(log_dir_path: Path | str | None, prefix: str = "request") -> Logger | None:
    """Create a fresh FileHandler-backed logger.

    Files are named ``<prefix> YYYY-MM-DD HH-MM-SS.log`` (or
    ``..._N.log`` on same-second collisions). Each call returns a fresh
    logger so a context that retries inside it accumulates into the same
    file.
    """
    global _LAST_TIMESTAMP, _LOGGER_SUFFIX_ID

    dir_path = _ensure_dir(Path(log_dir_path) if log_dir_path is not None else None)
    if dir_path is None:
        return None

    now = datetime.datetime.now(datetime.UTC)
    timestamp_key = now.strftime("%Y-%m-%d %H-%M-%S")

    with _LOGGER_LOCK:
        if _LAST_TIMESTAMP == timestamp_key:
            _LOGGER_SUFFIX_ID += 1
            suffix_id = _LOGGER_SUFFIX_ID
        else:
            _LAST_TIMESTAMP = timestamp_key
            _LOGGER_SUFFIX_ID = 1
            suffix_id = 1

    if suffix_id == 1:
        file_name = f"{prefix} {timestamp_key}.log"
        logger_name = f"{prefix} {timestamp_key}"
    else:
        file_name = f"{prefix} {timestamp_key}_{suffix_id}.log"
        logger_name = f"{prefix} {timestamp_key}_{suffix_id}"

    file_path = dir_path / file_name
    logger = getLogger(logger_name)
    logger.setLevel(DEBUG)
    handler = FileHandler(file_path, encoding="utf-8")
    handler.setLevel(DEBUG)
    handler.setFormatter(Formatter("%(asctime)s    %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)

    return logger


__all__ = ["make_request_logger"]
