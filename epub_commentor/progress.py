"""CLI-facing progress display built on :mod:`rich.progress`.

The pipeline emits :class:`ProgressEvent` instances through a callback
that the host application (CLI, notebook, library user) supplies. The
default renderer installed by :func:`make_default_progress_callback`
mounts those events onto a single :class:`rich.progress.Progress` with
two task rows stacked vertically — the top row tracks chapter
progress, the bottom row tracks block progress within the current
chapter:

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
    process layer emits a warn event so the rich display can render
    a single yellow line via ``Console.log`` (above the live progress
    bar) without breaking the bar itself. The dataclass still accepts
    any ``stage`` string for forward compatibility. Note: when the
    no-op renderer is in use (quiet / non-TTY), warn events still
    surface through the project logger so non-interactive runs see
    soft-skip messages on stderr.
    """

    stage: str
    current: int
    total: int
    substage: str | None = None
    message: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


class _NoOpDisplay:
    """Drop-in renderer used when ``quiet=True`` or stderr is not a TTY.

    Progress events are dropped, but ``stage="warn"`` events are
    surfaced through the project logger so non-TTY / quiet users still
    see soft-skip messages on stderr (otherwise they'd only appear in
    the debug log file, which most users never open).
    """

    def update(self, event: ProgressEvent) -> None:
        if event.stage == "warn" and event.message:
            _logger.warning(event.message)
        # Other events: dropped (matches "no progress output" promise).

    def close(self) -> None:  # pragma: no cover - trivial
        return


class RichProgressDisplay:
    """Two-row progress renderer driven by ``ProgressEvent`` instances.

    A single :class:`rich.progress.Progress` instance is started lazily
    on the first ``process / scan`` event. Two tasks share that
    instance: ``chapter_task`` advances per chapter and ``block_task``
    advances per block. The bar columns are
    ``SpinnerColumn · TextColumn · BarColumn · MofNCompleteColumn ·
    TimeRemainingColumn``; ``transient=False`` keeps both rows visible
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


def make_default_progress_callback(quiet: bool = False) -> ProgressCallback:
    """Construct the renderer the CLI installs by default.

    When ``quiet`` is True the returned callback is a no-op so the CLI
    produces zero progress output. When stderr is not a TTY (e.g.
    piped or redirected) the callback also degrades to a no-op so the
    bar does not draw escape codes into logs. Otherwise the callback
    drives a :class:`RichProgressDisplay`; the underlying renderer is
    reachable via ``callback.__self__`` so callers can request a clean
    shutdown.
    """
    if quiet or not sys.stderr.isatty():
        return _NoOpDisplay().update
    return RichProgressDisplay().update


__all__ = ["ProgressCallback", "ProgressEvent", "make_default_progress_callback"]
