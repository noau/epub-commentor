"""CLI-facing progress display built on :mod:`rich.progress`.

The pipeline emits :class:`ProgressEvent` instances through a callback
that the host application (CLI, notebook, library user) supplies. The
default renderer installed by :func:`make_default_progress_callback`
selects one of three display classes based on the runtime context:

================== ======================================================
Context                       Display
================== ======================================================
``--quiet``                   :class:`_NoOpDisplay` (truly silent)
``--stream-logs``             :class:`_StreamLogDisplay`
non-TTY stderr                :class:`_StreamLogDisplay` (auto-detect)
TTY + no flag                 :class:`RichProgressDisplay`
================== ======================================================

The two display dimensions — *logging config* (``--log-level``,
``--log-format``, ``--log-stream`` via :mod:`epub_commentor.logging_setup`)
and *display selection* (``--quiet``, ``--stream-logs``, TTY auto-detect)
— are orthogonal so a TTY user can opt into chatty JSON logs without
losing the rich progress bar.

The :class:`RichProgressDisplay` mounts events onto a single
:class:`rich.progress.Progress` with two task rows stacked vertically —
the top row tracks chapter progress, the bottom row tracks block progress
within the current chapter::

    ⠋ Ch. 3/28: The Little Prince   ████████░░░░░  3/28  02:34
    ⠙ (block 12/24)                 ██████░░░░░░░ 12/24  00:31

Scope
-----
The callback only sees ``process``-stage events — the long LLM-driven
phase. The ``extract`` and ``inject`` stages are short enough that
``commentor.py`` prints single status lines to stderr directly; they
do not flow through this callback. This keeps the progress renderer
strictly after any user interaction (e.g. ``chapter_filter``'s
rich-selector picker), so rich and rich-selector never share terminal
ownership.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressEvent:
    """One progress update from the pipeline.

    ``stage`` is ``"process"`` in practice. ``substage`` is set only
    for the process stage: ``"scan"`` advances the chapter-level
    task and resets the block task; ``"annotate"`` advances the
    block-level task. The dataclass still accepts any ``stage``
    string for forward compatibility. ``stage="warn"`` is the soft-skip
    notification channel — when a Stage 1 scan or Stage 2 annotate
    block fails and the pipeline is configured to keep going, the
    process layer emits a warn event. ``RichProgressDisplay`` renders
    it via :meth:`rich.console.Console.log` above the live bar;
    ``_StreamLogDisplay`` re-emits it as a ``logger.warning`` record so
    non-TTY / cloud users see soft-skip messages through the standard
    project logger. ``stage="done"`` is reserved for terminal events
    (final progress callback before the bar closes).
    """

    stage: str
    current: int
    total: int
    substage: str | None = None
    message: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


class _NoOpDisplay:
    """Truly silent renderer used when ``--quiet`` is set.

    Drops every event, including ``stage="warn"``. The ``--quiet`` flag
    is the user saying "I want zero output" — even soft-skip warnings
    are intentionally swallowed so cron / discarded-output scenarios
    see exactly what they asked for. Users who want warnings but no
    rich bar should pair ``--stream-logs --log-level=WARNING``.
    """

    def update(self, event: ProgressEvent) -> None:  # pragma: no cover - trivial
        return

    def close(self) -> None:  # pragma: no cover - trivial
        return


class _StreamLogDisplay:
    """Renderer that routes events to the project logger.

    Used when the user opts into ``--stream-logs`` or when stderr is not
    a TTY (piped / redirected). Each :class:`ProgressEvent` is mapped
    onto a single :mod:`logging` call so the configured
    :func:`~epub_commentor.logging_setup.setup_root_logger` handler —
    text or JSON, stdout or stderr — gets exactly one record per event.
    Rich and stream-logger are mutually exclusive: rich needs a TTY to
    redraw; the stream logger needs a clean text stream so log lines
    stay grep-friendly.

    Event → log level mapping
    -------------------------
    ``stage="warn"``
        :meth:`logging.Logger.warning` — soft-skip notifications.
    ``stage="done"`` or ``stage ∈ {"process", "extract", "inject"}``
        :meth:`logging.Logger.info` — normal progress.
    Any other stage
        :meth:`logging.Logger.info` — best-effort, forwarded as-is.
    """

    def _extras(self, event: ProgressEvent) -> dict[str, object]:
        """Build the ``extra={...}`` payload carried alongside the log record."""
        extras: dict[str, object] = {"stage": event.stage}
        if event.substage is not None:
            extras["substage"] = event.substage
        if event.current:
            extras["current"] = event.current
        if event.total:
            extras["total"] = event.total
        if event.message is not None:
            extras["message_event"] = event.message
        return extras

    def update(self, event: ProgressEvent) -> None:
        message = event.message or ""
        extras = self._extras(event)
        if event.stage == "warn":
            _logger.warning(message, extra=extras)
        else:
            _logger.info(message, extra=extras)

    def close(self) -> None:  # pragma: no cover - trivial
        return


class RichProgressDisplay:
    """Two-row progress renderer driven by ``ProgressEvent`` instances.

    A single :class:`rich.progress.Progress` instance is started lazily
    on the first ``process / scan`` event. Two tasks share that
    instance: ``chapter_task`` advances per chapter and ``block_task``
    advances per block. The bar columns are
    ``SpinnerColumn · TextColumn · BarColumn · MofNCompleteColumn ·
    TimeRemainingColumn``; ``transient=True`` keeps both rows visible
    after ``close()`` so the final 100% state is inspectable.

    Lifecycle
    ---------
    The :class:`rich.console.Console` is constructed and
    ``Progress.start()`` is called lazily on the first ``process /
    scan`` event so that no rich terminal ownership happens before
    the pipeline's long-running stage. In particular, this lets
    ``comment_epub``'s optional ``chapter_filter`` (which may invoke
    rich-selector) run with full terminal control before any rich
    rendering thread is alive. ``close()`` is idempotent. All event
    delivery happens on the main thread (see
    ``pipeline/process.py``'s ``as_completed`` loop) — ``Progress.update``
    is internally thread-safe regardless.
    """

    def __init__(self) -> None:
        self._console: Console | None = None
        self._progress: Progress | None = None
        self._chapter_task: TaskID | None = None
        self._block_task: TaskID | None = None
        self._closed: bool = False

    def _ensure_started(self) -> None:
        """Lazily construct the ``Console`` + ``Progress`` and add two tasks."""
        if self._progress is not None:
            return
        self._console = Console(file=sys.stderr)
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self._console,
            transient=True,
            expand=True,
        )
        self._progress.start()
        self._chapter_task = self._progress.add_task("准备中...", total=None)
        self._block_task = self._progress.add_task("—", total=None, visible=True)

    def update(self, event: ProgressEvent) -> None:
        if event.stage == "warn":
            self._ensure_started()
            console = self._console
            if console is not None and event.message:
                # Console.log renders above the Live region; safe to call
                # while the progress bar is alive. Style with a yellow
                # ⚠ prefix so soft-skip messages stand out from the bar.
                console.log(f"[yellow]⚠[/yellow] [bold yellow]{event.message}[/bold yellow]")
            return

        if event.stage != "process":
            return

        self._ensure_started()
        progress = self._progress
        chapter_task = self._chapter_task
        block_task = self._block_task
        if progress is None or chapter_task is None or block_task is None:
            return  # pragma: no cover - guarded by _ensure_started

        if event.substage == "scan":
            description = (
                f"Ch. {event.current}/{event.total}: {(event.message or '').strip()[:40]}"
                if event.current > 0
                else f"准备中... ({event.total} chapters)"
            )
            progress.update(chapter_task, total=event.total, completed=event.current, description=description)
            # Reset the block row for the new chapter; the upcoming annotate events
            # will set total + completed.
            progress.update(block_task, total=None, completed=0, description="—")
        elif event.substage == "annotate":
            progress.update(
                block_task,
                total=event.total,
                completed=event.current,
                description=f"(block {event.current}/{event.total})",
            )

    def close(self) -> None:
        """Stop the ``Progress``. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        if self._progress is not None:
            try:
                self._progress.stop()
            except Exception:  # pragma: no cover - defensive
                pass
        self._progress = None
        self._chapter_task = None
        self._block_task = None
        self._console = None


def make_default_progress_callback(
    quiet: bool = False,
    stream_logs: bool = False,
) -> ProgressCallback:
    """Construct the renderer the CLI installs by default.

    Selection rules (in order):

    1. ``quiet=True`` → :class:`_NoOpDisplay` (truly silent — drops
       even warn events).
    2. ``stream_logs=True`` or stderr is not a TTY →
       :class:`_StreamLogDisplay` (each event becomes a log record
       routed through the project logger).
    3. Otherwise → :class:`RichProgressDisplay` (live two-row bar).

    The returned callback is always a bound method, so callers can
    invoke ``callback.__self__.close()`` for a clean shutdown — the
    CLI's ``finally`` block relies on this for both the rich and
    stream-log displays (``_StreamLogDisplay.close`` is a no-op).
    """
    if quiet:
        return _NoOpDisplay().update
    if stream_logs or not sys.stderr.isatty():
        return _StreamLogDisplay().update
    return RichProgressDisplay().update


__all__ = ["ProgressCallback", "ProgressEvent", "make_default_progress_callback"]
