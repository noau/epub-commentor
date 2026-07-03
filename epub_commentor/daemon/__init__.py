"""Cloud daemon for epub-commentor — long-running queue and worker.

Public surface re-exported at package root:

* :class:`DaemonConfig` — daemon-level settings (workspace dir, log level,
  disk circuit-breaker thresholds, polling cadence).
* :func:`load_daemon_config` — read ``format.daemon.json`` from disk.
* :func:`serve` — entry point that opens the SQLite database, runs
  crash recovery, and starts the worker loop (blocking).
* :func:`build_arg_parser` — ``--workspace / --once / --max-seconds`` flags.
* :func:`worker_loop` — extracted core loop, useful for tests.
* :data:`SQLite connection helpers <db>` and :class:`Workspace` — used by
  :mod:`epub_commentor.ctl` (the ``epubctl`` CLI client).

The package is deliberately small (~9 files, ~1900 LoC including tests)
and reuses the existing :func:`~epub_commentor.commentor.comment_epub`
API rather than shelling out to the ``epub-commentor`` console script —
see ``daemon-prd.md`` §11 for the rationale.
"""

from __future__ import annotations

from .config import DaemonConfig, load_daemon_config
from .server import build_arg_parser, serve, worker_loop

__all__ = [
    "DaemonConfig",
    "load_daemon_config",
    "serve",
    "build_arg_parser",
    "worker_loop",
]
