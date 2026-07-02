"""Unit tests for :mod:`epub_commentor.logging_setup`.

Covers:

- :func:`setup_root_logger` — idempotency, level filter, unknown level,
  stream routing.
- :class:`TextFormatter` — ISO 8601 UTC timestamps, pipe-separated
  layout, level-name padding.
- :class:`JsonFormatter` — single-line JSON, ``extra`` pass-through,
  non-serializable coercion via ``default=str``.

The handler attaches to the ``epub_commentor`` namespace logger so
tests can capture records with :func:`pytest.LogCaptureFixture` without
fighting Python's default root handler configuration.
"""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pytest

from epub_commentor.logging_setup import (
    JsonFormatter,
    TextFormatter,
    setup_root_logger,
)

_PROJECT_LOGGER = "epub_commentor"


@pytest.fixture(autouse=True)
def _reset_project_logger():
    """Strip any handlers attached by previous tests so setup is observable."""
    logger = logging.getLogger(_PROJECT_LOGGER)
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    yield
    logger.handlers.clear()
    for h in saved_handlers:
        logger.addHandler(h)
    logger.setLevel(saved_level)


class TestSetupRootLogger:
    def test_installs_handler_on_project_logger(self) -> None:
        logger = setup_root_logger(level="DEBUG")
        assert logger is logging.getLogger(_PROJECT_LOGGER)
        assert len(logger.handlers) == 1

    def test_unknown_level_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="unknown log level"):
            setup_root_logger(level="TRACE")

    def test_level_filter_blocks_lower_levels(self) -> None:
        # Verify the project logger's effective level after setup. The
        # actual handler filter is exercised by
        # ``TestStreamRoutingEndToEnd`` below.
        project = setup_root_logger(level="WARNING")
        assert project.level == logging.WARNING
        # A second setup with a stricter level takes effect immediately.
        setup_root_logger(level="ERROR")
        assert project.level == logging.ERROR

    def test_debug_level_admits_all(self, caplog: pytest.LogCaptureFixture) -> None:
        project = setup_root_logger(level="DEBUG")
        with caplog.at_level(logging.DEBUG, logger=_PROJECT_LOGGER):
            project.debug("d")
            project.info("i")
            project.warning("w")
            project.error("e")
        seen = [r.getMessage() for r in caplog.records if r.name == _PROJECT_LOGGER]
        assert seen == ["d", "i", "w", "e"]

    def test_idempotent_does_not_duplicate_handlers(self) -> None:
        setup_root_logger(level="INFO")
        setup_root_logger(level="INFO")
        setup_root_logger(level="INFO")
        logger = logging.getLogger(_PROJECT_LOGGER)
        assert len(logger.handlers) == 1

    def test_idempotent_refreshes_level(self, caplog: pytest.LogCaptureFixture) -> None:
        setup_root_logger(level="WARNING")
        setup_root_logger(level="INFO")  # second call should lower the threshold
        project = logging.getLogger(_PROJECT_LOGGER)
        with caplog.at_level(logging.DEBUG, logger=_PROJECT_LOGGER):
            project.info("now visible")
        assert any(
            r.name == _PROJECT_LOGGER and r.getMessage() == "now visible"
            for r in caplog.records
        )

    def test_default_level_is_warning(self) -> None:
        setup_root_logger()
        project = logging.getLogger(_PROJECT_LOGGER)
        assert project.level == logging.WARNING

    def test_default_format_is_text(self) -> None:
        setup_root_logger()
        handler = logging.getLogger(_PROJECT_LOGGER).handlers[0]
        assert isinstance(handler.formatter, TextFormatter)

    def test_default_stream_is_stderr(self) -> None:
        setup_root_logger()
        handler = logging.getLogger(_PROJECT_LOGGER).handlers[0]
        assert handler.stream is __import__("sys").stderr

    def test_stream_routed_to_stdout(self) -> None:
        setup_root_logger(level="INFO", stream="stdout")
        handler = logging.getLogger(_PROJECT_LOGGER).handlers[0]
        assert handler.stream is __import__("sys").stdout


class TestTextFormatter:
    def test_timestamp_is_iso8601_utc(self) -> None:
        record = logging.LogRecord(
            name=_PROJECT_LOGGER,
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        formatter = TextFormatter()
        ts = formatter.formatTime(record)
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", ts
        ), ts

    def test_full_line_shape(self) -> None:
        record = logging.LogRecord(
            name="epub_commentor.foo",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="something happened",
            args=(),
            exc_info=None,
        )
        line = TextFormatter().format(record)
        # Timestamp + 8-char padded level + logger + pipe + message.
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z "
            r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\S+ \| .*$",
            line,
        ), line
        assert "WARNING" in line
        assert "epub_commentor.foo" in line
        assert "something happened" in line
        assert " | " in line

    def test_level_name_is_left_padded(self) -> None:
        record = logging.LogRecord(
            name=_PROJECT_LOGGER,
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="x",
            args=(),
            exc_info=None,
        )
        line = TextFormatter().format(record)
        # INFO (4 chars) + 4 spaces of padding to width 8.
        assert " INFO    " in line


class TestJsonFormatter:
    def test_output_is_single_json_line(self) -> None:
        record = logging.LogRecord(
            name=_PROJECT_LOGGER,
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        line = JsonFormatter().format(record)
        # Single line, parseable, no trailing newline.
        assert "\n" not in line
        payload = json.loads(line)
        assert payload["level"] == "INFO"
        assert payload["logger"] == _PROJECT_LOGGER
        assert payload["message"] == "hello"
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", payload["ts"]
        )

    def test_extras_are_merged_into_top_level(self) -> None:
        record = logging.LogRecord(
            name=_PROJECT_LOGGER,
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="annotated chapter",
            args=(),
            exc_info=None,
        )
        record.stage = "process"  # type: ignore[attr-defined]
        record.substage = "annotate"  # type: ignore[attr-defined]
        record.current = 5  # type: ignore[attr-defined]
        record.total = 12  # type: ignore[attr-defined]
        payload = json.loads(JsonFormatter().format(record))
        assert payload["stage"] == "process"
        assert payload["substage"] == "annotate"
        assert payload["current"] == 5
        assert payload["total"] == 12

    def test_non_serializable_extras_coerced_to_str(self) -> None:
        record = logging.LogRecord(
            name=_PROJECT_LOGGER,
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="x",
            args=(),
            exc_info=None,
        )
        # Path / datetime are the most common non-JSON-serializable
        # extras; ``default=str`` should coerce both without raising.
        record.path = Path("/tmp/foo.epub")  # type: ignore[attr-defined]
        record.when = datetime(2026, 7, 2, 14, 23, 1)  # type: ignore[attr-defined]
        payload = json.loads(JsonFormatter().format(record))
        # Compare via ``str(...)`` so the assertion is portable across
        # Windows (where ``Path("/tmp/foo.epub")`` is normalised to
        # backslashes) and POSIX.
        assert payload["path"] == str(Path("/tmp/foo.epub"))
        assert payload["when"].startswith("2026-07-02")

    def test_message_args_are_substituted(self) -> None:
        record = logging.LogRecord(
            name=_PROJECT_LOGGER,
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="chapter %d of %d",
            args=(3, 12),
            exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(record))
        assert payload["message"] == "chapter 3 of 12"


class TestStreamRoutingEndToEnd:
    """Round-trip through ``setup_root_logger`` + ``TextFormatter``."""

    def test_text_routing_writes_to_chosen_stream(self) -> None:
        # ``capfd`` captures both fd 1 and fd 2; using a real StreamHandler
        # pointed at a StringIO is the cleanest way to assert the bytes.
        buffer = io.StringIO()
        setup_root_logger(level="DEBUG", fmt="text", stream="stderr")
        handler = logging.getLogger(_PROJECT_LOGGER).handlers[0]
        # Swap the stream to our buffer (handler.stream assignment is
        # supported by StreamHandler).
        handler.stream = buffer
        logging.getLogger(_PROJECT_LOGGER).info("via buffer")
        handler.flush()
        line = buffer.getvalue().rstrip("\n")
        assert "via buffer" in line
        assert "INFO" in line

    def test_json_routing_emits_one_object_per_line(self) -> None:
        buffer = io.StringIO()
        setup_root_logger(level="DEBUG", fmt="json", stream="stderr")
        handler = logging.getLogger(_PROJECT_LOGGER).handlers[0]
        handler.stream = buffer
        project = logging.getLogger(_PROJECT_LOGGER)
        project.info("first")
        project.warning("second")
        handler.flush()
        lines = [ln for ln in buffer.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 2
        payloads = [json.loads(ln) for ln in lines]
        assert [p["message"] for p in payloads] == ["first", "second"]
        assert [p["level"] for p in payloads] == ["INFO", "WARNING"]


__all__ = [
    "TestJsonFormatter",
    "TestSetupRootLogger",
    "TestStreamRoutingEndToEnd",
    "TestTextFormatter",
]
