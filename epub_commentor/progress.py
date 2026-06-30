"""CLI-facing progress display built on :mod:`tqdm`.

The pipeline emits :class:`ProgressEvent` instances through a callback
that the host application (CLI, notebook, library user) supplies. The
default renderer installed by :func:`make_default_progress_callback`
turns those events into two stacked tqdm bars: an outer chapter bar
plus an inner block bar that resets on each new chapter.

Stages
------
``extract`` and ``inject`` are short enough that single ``tqdm.write()``
status lines are sufficient. ``process`` is the long phase and gets the
two-bar treatment, where ``substage="scan"`` advances the chapter bar
and ``substage="annotate"`` advances the block bar.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tqdm import tqdm


@dataclass(frozen=True)
class ProgressEvent:
    """One progress update from the pipeline.

    ``stage`` is one of ``"extract"``, ``"process"``, ``"inject"``.
    ``substage`` is set only when ``stage == "process"``: ``"scan"``
    advances the chapter-level bar, ``"annotate"`` advances the
    block-level bar.
    """

    stage: str
    current: int
    total: int
    substage: str | None = None
    message: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


class _NoOpDisplay:
    """Drop-in renderer used when ``quiet=True``."""

    def update(self, event: ProgressEvent) -> None:  # pragma: no cover - trivial
        return

    def close(self) -> None:  # pragma: no cover - trivial
        return


class TqdmProgressDisplay:
    """Two-bar renderer: outer chapter bar + inner block bar.

    The chapter bar lives on ``position=0`` and is opened lazily on the
    first ``process / scan`` event. The block bar lives on ``position=1``
    and is recreated whenever a new chapter's total differs from the
    previous one (which it does every chapter for non-uniform block
    counts). Both bars auto-close when their total is reached.
    """

    def __init__(self) -> None:
        self._chapter_bar: tqdm | None = None
        self._block_bar: tqdm | None = None
        self._block_total: int = -1

    def update(self, event: ProgressEvent) -> None:
        if event.stage == "extract":
            if event.current == 0:
                tqdm.write("Extracting chapters...")
            elif event.current == event.total:
                tqdm.write(f"Extracted {event.total} chapter(s).")
            return
        if event.stage == "inject":
            if event.current == 0:
                tqdm.write("Injecting annotations...")
            elif event.current == event.total:
                tqdm.write("Injection complete.")
            return
        if event.stage != "process":
            return

        if event.substage == "scan":
            if self._chapter_bar is None:
                self._chapter_bar = tqdm(
                    total=event.total,
                    desc="Chapters",
                    position=0,
                    unit="ch",
                )
            self._chapter_bar.n = event.current
            label = (event.message or "").strip()[:40]
            self._chapter_bar.set_description_str(f"Ch. {event.current}/{event.total}: {label}")
            self._chapter_bar.refresh()
            if event.current == event.total:
                self._chapter_bar.close()
                self._chapter_bar = None
        elif event.substage == "annotate":
            if self._block_bar is None or self._block_total != event.total:
                if self._block_bar is not None:
                    self._block_bar.close()
                self._block_bar = tqdm(
                    total=event.total,
                    desc="Blocks",
                    position=1,
                    unit="blk",
                )
                self._block_total = event.total
            self._block_bar.n = event.current
            self._block_bar.refresh()
            if event.current == event.total:
                self._block_bar.close()
                self._block_bar = None
                self._block_total = -1

    def close(self) -> None:
        """Force-close both bars. Safe to call multiple times."""
        if self._chapter_bar is not None:
            self._chapter_bar.close()
            self._chapter_bar = None
        if self._block_bar is not None:
            self._block_bar.close()
            self._block_bar = None


def make_default_progress_callback(quiet: bool = False) -> ProgressCallback:
    """Construct the renderer the CLI installs by default.

    When ``quiet`` is True the returned callback is a no-op so the CLI
    produces zero progress output. Otherwise the callback drives a
    :class:`TqdmProgressDisplay`; the underlying renderer is reachable
    via ``callback.__self__`` so callers can request a clean shutdown.
    """
    display: TqdmProgressDisplay | _NoOpDisplay
    if quiet:
        display = _NoOpDisplay()
    else:
        display = TqdmProgressDisplay()
    return display.update


__all__ = ["ProgressCallback", "ProgressEvent", "TqdmProgressDisplay", "make_default_progress_callback"]
