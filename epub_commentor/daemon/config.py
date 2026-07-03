"""Daemon-level configuration (workspace dir, log level, disk thresholds).

The daemon reads a separate ``format.daemon.json`` file (see
``format.daemon.template.json``) so the LLM-side ``format.json`` can
stay focused on API credentials. Two well-known lookup paths:

1. ``$EPUBCTL_DAEMON_CONFIG`` env var — overrides everything else.
2. ``<workspace_dir>/format.daemon.json`` — bundled with the workspace
   so a single ``epubctl`` invocation finds the matching daemon.

The shape is intentionally flat so existing ``_split_format_config``
idioms don't apply: every key here is daemon-owned. Unknown keys are
collected into :attr:`DaemonConfig.unknown_keys` and logged at WARNING
so a stray typo is loud but never crashes startup.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_logger = logging.getLogger(__name__)


@dataclass
class DiskCircuitConfig:
    """Thresholds for the worker-loop disk circuit breaker.

    Either condition tripping pauses all non-terminal jobs:

    * ``avail_gb < min_free_gb``
    * ``used_percent > (100 - min_free_percent)``
    """

    min_free_gb: float = 2.0
    min_free_percent: float = 10.0


@dataclass
class DaemonConfig:
    """All knobs the daemon reads from ``format.daemon.json``.

    Defaults are tuned for a 30 GB cloud server running a single
    200 MB EPUB at a time. Adjust ``workspace_dir`` and ``disk`` for
    smaller disks or multi-tenant setups.
    """

    workspace_dir: Path = field(default_factory=lambda: Path("./daemon_workspace"))
    sqlite_path: Path | None = None  # resolved relative to workspace_dir when None
    log_level: str = "INFO"
    log_format: str = "text"  # "text" | "json"
    max_retries: int = 3
    disk: DiskCircuitConfig = field(default_factory=DiskCircuitConfig)
    shutdown_grace_seconds: int = 30
    poll_interval_idle_seconds: float = 5.0
    poll_interval_paused_seconds: float = 60.0
    notification_command: str | None = None  # shell command, optional

    # Populated after ``load_daemon_config``; useful for warning the
    # operator about typos in ``format.daemon.json``.
    unknown_keys: list[str] = field(default_factory=list)

    def resolve_sqlite_path(self) -> Path:
        """Return the absolute path to the SQLite database.

        ``sqlite_path=None`` defaults to ``<workspace_dir>/daemon.sqlite``.
        Relative paths resolve against ``workspace_dir``.
        """
        base = self.workspace_dir.resolve()
        if self.sqlite_path is None:
            return base / "daemon.sqlite"
        p = Path(self.sqlite_path)
        if not p.is_absolute():
            p = base / p
        return p.resolve()


# Field names this dataclass owns (minus ``unknown_keys`` which is
# populated after load). Used by the loader to detect typos.
_DAEMON_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "workspace_dir",
        "sqlite_path",
        "log_level",
        "log_format",
        "max_retries",
        "disk",
        "shutdown_grace_seconds",
        "poll_interval_idle_seconds",
        "poll_interval_paused_seconds",
        "notification_command",
    }
)


def load_daemon_config(explicit_path: Path | None = None) -> DaemonConfig:
    """Locate and parse ``format.daemon.json``.

    Lookup order:

    1. ``explicit_path`` argument (highest priority — used by tests).
    2. ``$EPUBCTL_DAEMON_CONFIG`` environment variable.
    3. ``<cwd>/format.daemon.json``.

    A missing file is non-fatal: the function returns a default
    :class:`DaemonConfig` and logs at INFO. An unreadable / invalid file
    is fatal (raises :class:`RuntimeError`) so a typo never silently
    degrades the daemon.

    Returns
    -------
    DaemonConfig
        ``workspace_dir`` is resolved to an absolute path even when the
        JSON supplied a relative one, so downstream code can assume the
        filesystem layout is stable across worker / CLI processes.
    """
    config_path = _locate_config(explicit_path)
    if config_path is None:
        cfg = DaemonConfig()
        cfg.workspace_dir = cfg.workspace_dir.resolve()
        _logger.info("no format.daemon.json found; using defaults (workspace=%s)", cfg.workspace_dir)
        return cfg

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"format.daemon.json is not valid JSON ({config_path}): {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"format.daemon.json must be a JSON object; got {type(raw).__name__}")

    # Route each key into the dataclass; unknown keys are surfaced but
    # do not abort startup (matches CLI behaviour for format.json).
    disk_raw = raw.pop("disk", None)
    kwargs: dict = {}
    unknown: list[str] = []
    for key, value in raw.items():
        if key in _DAEMON_FIELD_NAMES:
            kwargs[key] = value
        else:
            unknown.append(key)

    if isinstance(disk_raw, dict):
        kwargs["disk"] = DiskCircuitConfig(**disk_raw)
    elif disk_raw is not None:
        raise RuntimeError(f'"disk" must be a JSON object; got {type(disk_raw).__name__}')

    # JSON scalars are bare strings; lift to ``Path`` so downstream
    # code can call ``.resolve()`` / ``.mkdir()`` without ad-hoc casting.
    if "workspace_dir" in kwargs and isinstance(kwargs["workspace_dir"], str):
        kwargs["workspace_dir"] = Path(kwargs["workspace_dir"])
    if "sqlite_path" in kwargs and isinstance(kwargs["sqlite_path"], str):
        kwargs["sqlite_path"] = Path(kwargs["sqlite_path"])

    cfg = DaemonConfig(**kwargs)
    cfg.workspace_dir = cfg.workspace_dir.resolve()
    cfg.unknown_keys = unknown

    if unknown:
        _logger.warning(
            "format.daemon.json: ignoring unknown keys %s (typo? See format.daemon.template.json)",
            unknown,
        )
    return cfg


def _locate_config(explicit: Path | None) -> Path | None:
    """Resolve which config file (if any) should be loaded."""
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(f"daemon config not found at: {explicit}")
        return explicit.resolve()
    env = os.environ.get("EPUBCTL_DAEMON_CONFIG")
    if env:
        p = Path(env)
        if not p.exists():
            raise FileNotFoundError(f"$EPUBCTL_DAEMON_CONFIG={env} does not exist")
        return p.resolve()
    candidate = Path("format.daemon.json").resolve()
    return candidate if candidate.exists() else None


__all__ = ["DaemonConfig", "DiskCircuitConfig", "load_daemon_config"]


# ``_locate_config`` is private but referenced from tests; keep its
# definition above the public symbols so ``__all__`` stays minimal.
_ = sys  # silence "imported but unused" warnings while keeping stdlib parity
