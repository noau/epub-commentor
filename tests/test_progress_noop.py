"""Unit tests for the silent / stream-log progress displays.

Three display classes live in :mod:`epub_commentor.progress`:

- :class:`_NoOpDisplay` — used by ``--quiet``. Truly silent: drops every
  event, including ``stage="warn"`` soft-skip notifications.
- :class:`_StreamLogDisplay` — used by ``--stream-logs`` or non-TTY.
  Routes each :class:`ProgressEvent` to a single log record on the
  ``epub_commentor.progress`` logger so the project's stream logger
  handler can format / filter / dispatch it.
- :class:`RichProgressDisplay` — used by TTY default. Drives a two-row
  :class:`rich.progress.Progress`; covered separately by integration
  smoke tests.

The factory :func:`make_default_progress_callback` selects between
them based on ``quiet`` / ``stream_logs`` flags and the TTY state of
stderr. The matrix is exercised by ``TestFactory`` below.
"""

from __future__ import annotations

import json
import logging

import pytest

from epub_commentor.progress import (
    ProgressEvent,
    RichProgressDisplay,
    _NoOpDisplay,
    _StreamLogDisplay,
    make_default_progress_callback,
)


class TestNoOpIsTrulySilent:
    """``--quiet`` swallows every event, including soft-skip warns."""

    def test_warn_event_does_not_emit_log_record(self, caplog: pytest.LogCaptureFixture) -> None:
        """A ``stage="warn"`` event must produce zero log records."""
        display = _NoOpDisplay()
        with caplog.at_level(logging.WARNING, logger="epub_commentor.progress"):
            display.update(
                ProgressEvent(
                    stage="warn",
                    current=0,
                    total=0,
                    message="block @ p_id 5 → skipped",
                )
            )
        # caplog sees no records on this logger — _NoOpDisplay.update is
        # genuinely a no-op now (the warn-channel was deduped out).
        assert caplog.records == []

    def test_non_warn_events_are_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        """Every non-warn stage produces zero log records."""
        display = _NoOpDisplay()
        with caplog.at_level(logging.DEBUG, logger="epub_commentor.progress"):
            display.update(ProgressEvent(stage="process", substage="scan", current=1, total=5))
            display.update(ProgressEvent(stage="process", substage="annotate", current=3, total=4))
            display.update(ProgressEvent(stage="unknown", current=0, total=0))
            display.update(ProgressEvent(stage="extract", current=0, total=0))
        assert caplog.records == []

    def test_close_is_noop(self) -> None:
        display = _NoOpDisplay()
        # No exception, no return value to check beyond None.
        assert display.close() is None


class TestStreamLogDisplay:
    """``--stream-logs`` / non-TTY: each event becomes one log record."""

    def test_extract_event_emits_info_with_stage_extra(self, caplog: pytest.LogCaptureFixture) -> None:
        display = _StreamLogDisplay()
        with caplog.at_level(logging.DEBUG, logger="epub_commentor.progress"):
            display.update(ProgressEvent(stage="extract", current=2, total=10, message="hello"))
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_records) == 1
        record = info_records[0]
        assert record.name == "epub_commentor.progress"
        assert record.getMessage() == "hello"
        assert getattr(record, "stage") == "extract"
        assert getattr(record, "current") == 2
        assert getattr(record, "total") == 10

    def test_process_scan_event_emits_info_with_substage(self, caplog: pytest.LogCaptureFixture) -> None:
        display = _StreamLogDisplay()
        with caplog.at_level(logging.DEBUG, logger="epub_commentor.progress"):
            display.update(
                ProgressEvent(
                    stage="process",
                    substage="scan",
                    current=3,
                    total=12,
                    message="Chapter Three",
                )
            )
        record = next(r for r in caplog.records if r.levelno == logging.INFO)
        assert getattr(record, "substage") == "scan"
        assert getattr(record, "current") == 3
        assert getattr(record, "total") == 12

    def test_process_annotate_event_emits_info_with_substage(self, caplog: pytest.LogCaptureFixture) -> None:
        display = _StreamLogDisplay()
        with caplog.at_level(logging.DEBUG, logger="epub_commentor.progress"):
            display.update(
                ProgressEvent(
                    stage="process",
                    substage="annotate",
                    current=5,
                    total=8,
                    message=None,
                )
            )
        record = next(r for r in caplog.records if r.levelno == logging.INFO)
        assert getattr(record, "substage") == "annotate"
        # No message → empty payload (formatter falls back to "").
        assert record.getMessage() == ""

    def test_warn_event_emits_warning_level(self, caplog: pytest.LogCaptureFixture) -> None:
        display = _StreamLogDisplay()
        with caplog.at_level(logging.DEBUG, logger="epub_commentor.progress"):
            display.update(
                ProgressEvent(
                    stage="warn",
                    current=0,
                    total=0,
                    message="soft skip",
                )
            )
        warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn_records) == 1
        record = warn_records[0]
        assert getattr(record, "stage") == "warn"
        assert record.getMessage() == "soft skip"

    def test_done_event_emits_info(self, caplog: pytest.LogCaptureFixture) -> None:
        display = _StreamLogDisplay()
        with caplog.at_level(logging.DEBUG, logger="epub_commentor.progress"):
            display.update(ProgressEvent(stage="done", current=10, total=10, message="all done"))
        record = next(r for r in caplog.records if r.levelno == logging.INFO)
        assert getattr(record, "stage") == "done"
        assert record.getMessage() == "all done"

    def test_extras_are_json_serializable(self, caplog: pytest.LogCaptureFixture) -> None:
        """The extras carried on every record must survive ``JsonFormatter``."""
        display = _StreamLogDisplay()
        with caplog.at_level(logging.DEBUG, logger="epub_commentor.progress"):
            display.update(
                ProgressEvent(
                    stage="process",
                    substage="scan",
                    current=1,
                    total=1,
                    message="chapter.xhtml",
                )
            )
        record = next(r for r in caplog.records if r.levelno == logging.INFO)
        extras = {key: getattr(record, key) for key in ("stage", "substage", "current", "total", "message_event")}
        # No exception raised → extras are JSON-friendly even when
        # ``JsonFormatter`` later coerces non-serializable values via
        # ``default=str`` (e.g. Path / datetime objects).
        json.dumps(extras, default=str)

    def test_emitted_logger_name_is_progress(self, caplog: pytest.LogCaptureFixture) -> None:
        display = _StreamLogDisplay()
        with caplog.at_level(logging.DEBUG):
            display.update(ProgressEvent(stage="process", current=1, total=1))
        assert all(r.name == "epub_commentor.progress" for r in caplog.records)

    def test_close_is_noop(self) -> None:
        display = _StreamLogDisplay()
        assert display.close() is None


class TestFactory:
    """Selection matrix for :func:`make_default_progress_callback`."""

    def test_quiet_returns_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force TTY=True so isatty is not the deciding factor.
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        cb = make_default_progress_callback(quiet=True)
        assert cb.__self__ is not None
        assert isinstance(cb.__self__, _NoOpDisplay)

    def test_quiet_overrides_stream_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``quiet`` wins even if the user also passes ``stream_logs=True``.
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        cb = make_default_progress_callback(quiet=True, stream_logs=True)
        assert isinstance(cb.__self__, _NoOpDisplay)

    def test_stream_logs_returns_stream_log_display(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # TTY=True but explicit stream_logs → still _StreamLogDisplay.
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        cb = make_default_progress_callback(stream_logs=True)
        assert isinstance(cb.__self__, _StreamLogDisplay)

    def test_non_tty_returns_stream_log_display(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Auto-detect: stderr not a TTY → _StreamLogDisplay even
        # without an explicit flag.
        monkeypatch.setattr("sys.stderr.isatty", lambda: False)
        cb = make_default_progress_callback()
        assert isinstance(cb.__self__, _StreamLogDisplay)

    def test_tty_without_stream_logs_returns_rich(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # TTY=True and no flags → RichProgressDisplay.
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        cb = make_default_progress_callback()
        assert isinstance(cb.__self__, RichProgressDisplay)

    def test_default_quiet_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Calling without any args mirrors the old single-arg signature.
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        cb = make_default_progress_callback()
        # Quiet=False → RichProgressDisplay on TTY.
        assert isinstance(cb.__self__, RichProgressDisplay)


__all__ = ["TestFactory", "TestNoOpIsTrulySilent", "TestStreamLogDisplay"]
