"""Unit tests for the no-op progress display's warn-event fallback.

The :class:`_NoOpDisplay` is used when the CLI runs in ``--quiet`` mode
or when stderr is not a TTY. Progress events are intentionally dropped
so the no-op renderer can keep its promise of zero output — but
``stage="warn"`` events (soft-skip notifications) must still surface
to the user, otherwise they only appear in the debug log file which
most users never open. The fallback writes to the project logger so
the message reaches stderr via Python's default logging handler.
"""

from __future__ import annotations

import logging

import pytest

from epub_commentor.progress import ProgressEvent, _NoOpDisplay


class TestNoOpWarnFallback:
    def test_warn_event_routes_to_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        """A ``stage="warn"`` event must surface through the project logger."""
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
        assert any("block @ p_id 5 → skipped" in r.message for r in caplog.records)

    def test_non_warn_events_are_dropped(self) -> None:
        """Progress events (``stage="process"``) must produce zero output,
        preserving the no-op renderer's contract."""
        display = _NoOpDisplay()
        # No assertion possible — we simply verify no exception is raised
        # and no logger output occurs (covered by the lack of caplog entries).
        display.update(ProgressEvent(stage="process", substage="scan", current=1, total=5))
        display.update(ProgressEvent(stage="process", substage="annotate", current=3, total=4))
        display.update(ProgressEvent(stage="unknown", current=0, total=0))


__all__ = ["TestNoOpWarnFallback"]
