"""Stream logger setup for batch / cloud-server use.

Three concerns live here, kept out of :mod:`epub_commentor.progress` so the
display layer stays focused on rendering:

1. :func:`setup_root_logger` — attach a single ``StreamHandler`` to the
   ``epub_commentor`` namespace logger with the user's chosen level,
   format (text / JSON), and stream (stdout / stderr). Idempotent: a
   second call with the same arguments is a no-op, so test suites that
   import :mod:`epub_commentor` after the CLI already ran are safe.

2. :class:`TextFormatter` — one-line records with ISO 8601 UTC
   timestamps, padded level, dotted logger name, ``|`` separator. Built
   to be grep / journalctl / ``awk`` friendly.

3. :class:`JsonFormatter` — single-line JSON per record, ready for
   ``jq`` / Vector / Fluent Bit / Loki ingest. ``default=str`` keeps
   ``Path`` / ``datetime`` extras from crashing the formatter.

Design notes
------------
We attach the handler directly to ``logging.getLogger("epub_commentor")``
rather than calling :func:`logging.basicConfig` with ``force=True``,
because the latter would clobber any handlers a downstream user (notebook,
test runner, web app) already installed on the root logger. Attaching to
a child namespace keeps our config isolated and survives alongside the
caller's setup.

Convention: project modules use ``logging.getLogger(__name__)`` so this
handler sees every record emitted under the ``epub_commentor.*``
hierarchy.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# Marker attribute set on handlers installed by ``setup_root_logger`` so
# repeated calls (e.g. across CLI re-invocations within a single process)
# can be detected without relying on handler identity. Namespaced with
# an underscore to discourage external code from inspecting it.
_SETUP_MARKER = "_epub_commentor_setup"


class TextFormatter(logging.Formatter):
    """One-line text formatter with ISO 8601 UTC timestamps.

    Format::

        2026-07-02T14:23:01.123Z INFO     epub_commentor.pipeline.process | message body

    Pipe separator keeps ``cut -d'|'`` trivial; left-padded level name
    aligns columns for ``grep``.
    """

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S.%fZ",
        )

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        # Override so we always emit UTC regardless of the host TZ.
        dt = datetime.fromtimestamp(record.created, tz=UTC)
        # ``%f`` gives microseconds (6 digits); trim to milliseconds for
        # readability. Keep the trailing Z so log shippers recognise it.
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    def format(self, record: logging.LogRecord) -> str:
        record.asctime = self.formatTime(record, self.datefmt)
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """One-line JSON formatter suitable for ``jq`` / log aggregators.

    Output schema::

        {"ts": "2026-07-02T14:23:01.123Z",
         "level": "INFO",
         "logger": "epub_commentor.pipeline.process",
         "message": "...",
         ...extras...}

    ``extras`` are merged into the top-level object so consumers can
    filter on structured fields without parsing the message string.
    Non-JSON-serializable values are coerced via ``default=str``.
    """

    _RESERVED_KEYS = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "taskName",
            "asctime",
            "message",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        # ``getMessage`` resolves ``%s`` placeholders against ``args``.
        payload: dict = {
            "ts": TextFormatter().formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._RESERVED_KEYS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def setup_root_logger(level: str = "WARNING", fmt: str = "text", stream: str = "stderr") -> logging.Logger:
    """Attach a single :class:`logging.StreamHandler` to the project logger.

    Parameters
    ----------
    level:
        One of ``"DEBUG"`` / ``"INFO"`` / ``"WARNING"`` / ``"ERROR"`` /
        ``"CRITICAL"``. Case-insensitive. Default ``"WARNING"`` mirrors
        Python's :func:`logging.basicConfig` default.
    fmt:
        ``"text"`` for the human-readable one-liner, ``"json"`` for
        machine-readable records.
    stream:
        ``"stdout"`` or ``"stderr"``.

    Returns
    -------
    logging.Logger
        The project logger (``epub_commentor``) after the handler is
        attached. Useful for direct ``logger.info(...)`` calls.

    Raises
    ------
    ValueError
        When ``level`` is not one of the recognised names.
    """
    normalised_level = level.upper()
    if normalised_level not in _VALID_LEVELS:
        raise ValueError(
            f"unknown log level {level!r}; expected one of {', '.join(_VALID_LEVELS)}"
        )

    stream_obj = sys.stdout if stream == "stdout" else sys.stderr
    root = logging.getLogger("epub_commentor")

    # Idempotency: if a previous call already attached a marked handler,
    # just refresh its level and return. Avoids duplicate log lines when
    # the CLI is re-invoked inside the same process (e.g. tests).
    for existing in root.handlers:
        if getattr(existing, _SETUP_MARKER, False):
            existing.setLevel(normalised_level)
            root.setLevel(normalised_level)
            return root

    handler: logging.StreamHandler = logging.StreamHandler(stream_obj)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.setLevel(normalised_level)
    setattr(handler, _SETUP_MARKER, True)
    root.addHandler(handler)
    root.setLevel(normalised_level)
    return root


__all__ = ["JsonFormatter", "TextFormatter", "setup_root_logger"]
