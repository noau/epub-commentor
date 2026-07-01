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

Stages
------
``extract`` and ``inject`` are short enough that single ``print()``
status lines to stderr are sufficient. ``process`` is the long phase
and drives the bar: ``substage="scan"`` advances the chapter-level
counter (and resets the block counter) while ``substage="annotate"``
advances the block-level counter. Both rows live the entire
``process`` window so the visual state is always self-explanatory.
"""

from __future__ import annotations

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
    TimeRemainingColumn,
)


@dataclass(frozen=True)
class ProgressEvent:
    """One progress update from the pipeline.

    ``stage`` is one of ``"extract"``, ``"process"``, ``"inject"``.
    ``substage`` is set only when ``stage == "process"``: ``"scan"``
    advances the chapter-level task and resets the block task;
    ``"annotate"`` advances the block-level task.
    """

    stage: str
    current: int
    total: int
    substage: str | None = None
    message: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


class _NoOpDisplay:
    """Drop-in renderer used when ``quiet=True`` or stderr is not a TTY."""

    def update(self, event: ProgressEvent) -> None:  # pragma: no cover - trivial
        return

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
    ``Progress.start()`` is called lazily on the first ``process / scan``
    event so the extract stage's ``print()`` status lines render cleanly
    to stderr before any bar appears. ``close()`` is idempotent. All
    event delivery happens on the main thread (see
    ``pipeline/process.py``'s ``as_completed`` loop) — ``Progress.update``
    is internally thread-safe regardless.
    """

    def __init__(self) -> None:
        self._console: Console = Console(file=sys.stderr)
        self._progress: Progress | None = None
        self._chapter_task: TaskID | None = None
        self._block_task: TaskID | None = None
        self._closed: bool = False

    def _ensure_started(self) -> None:
        """Lazily construct the ``Progress`` and add the two tasks."""
        if self._progress is not None:
            return
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(bar_width=24),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=self._console,
            transient=False,
        )
        self._progress.start()
        self._chapter_task = self._progress.add_task("准备中...", total=None)
        self._block_task = self._progress.add_task("—", total=None, visible=True)

    def update(self, event: ProgressEvent) -> None:
        if event.stage == "extract":
            if event.current == 0:
                print("Extracting chapters...", file=sys.stderr)
            elif event.current == event.total:
                print(f"Extracted {event.total} chapter(s).", file=sys.stderr)
            return
        if event.stage == "inject":
            if event.current == 0:
                print("Injecting annotations...", file=sys.stderr)
            elif event.current == event.total:
                print("Injection complete.", file=sys.stderr)
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
