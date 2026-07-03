"""Disk circuit breaker for the daemon worker loop.

Triggers on either of:

* available GB < ``min_free_gb``
* used percent > (100 - ``min_free_percent``)

When tripped, the worker pauses every non-terminal job and skips the
queue until the threshold recovers. Edge detection (a ``was_low`` /
``recovered`` pair) is the only state this class carries — it's
stateless enough to live in a single ``self._was_low`` boolean, which
also makes it trivially testable.

The module deliberately does **not** spawn its own thread. The worker
loop calls :meth:`is_low` once per iteration, which is at most a few
times per minute on an idle queue. A dedicated thread would only add
shutdown coordination cost for no measurable benefit.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .config import DiskCircuitConfig

_logger = logging.getLogger(__name__)


class DiskMonitor:
    """In-process disk circuit breaker.

    The monitor owns a single boolean (``_was_low``) that tracks whether
    the disk is currently in the low state. The :meth:`recovered` method
    returns ``True`` exactly once at the transition out of low — that
    is the worker's cue to bulk-resume paused jobs and emit the
    ``disk_recovered`` event.
    """

    def __init__(self, cfg: DiskCircuitConfig, workspace_dir: Path) -> None:
        self._cfg = cfg
        self._workspace_dir = workspace_dir
        self._was_low = False

    @property
    def cfg(self) -> DiskCircuitConfig:
        return self._cfg

    def is_low(self) -> bool:
        """Current trip state. Updates ``_was_low`` as a side-effect."""
        usage = shutil.disk_usage(self._workspace_dir)
        avail_gb = usage.free / (1024 ** 3)
        used_percent = (usage.used / usage.total) * 100 if usage.total else 0.0
        low = avail_gb < self._cfg.min_free_gb or used_percent > (100 - self._cfg.min_free_percent)
        if low and not self._was_low:
            _logger.warning(
                "disk circuit breaker tripped: %.2f GB free (%.1f%% used) at %s",
                avail_gb,
                used_percent,
                self._workspace_dir,
            )
            self._was_low = True
        return low

    def was_low(self) -> bool:
        """True while the breaker is tripped (sticky until recovered)."""
        return self._was_low

    def recovered(self) -> bool:
        """True exactly once on the low → healthy transition.

        The worker calls this *after* :meth:`is_low` so the edge is
        detected without an extra ``shutil.disk_usage`` round-trip.
        """
        if self._was_low and not self.is_low():
            _logger.info("disk circuit breaker recovered at %s", self._workspace_dir)
            self._was_low = False
            return True
        return False

    # Edge introspection helpers — useful in tests; cheap enough to
    # expose publicly.
    def current_snapshot(self) -> tuple[float, float]:
        """Return ``(avail_gb, used_percent)`` for the latest call."""
        usage = shutil.disk_usage(self._workspace_dir)
        avail_gb = usage.free / (1024 ** 3)
        used_percent = (usage.used / usage.total) * 100 if usage.total else 0.0
        return avail_gb, used_percent


__all__ = ["DiskMonitor"]
