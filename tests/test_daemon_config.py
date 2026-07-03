"""Tests for :mod:`epub_commentor.daemon.config`."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from epub_commentor.daemon.config import (
    DaemonConfig,
    DiskCircuitConfig,
    _locate_config,
    load_daemon_config,
)


class TestLoadDefaults:
    def test_missing_file_returns_defaults(self, tmp_path: Path, monkeypatch, caplog) -> None:
        # Run from a directory that has no format.daemon.json
        monkeypatch.chdir(tmp_path)
        with caplog.at_level(logging.INFO, logger="epub_commentor.daemon.config"):
            cfg = load_daemon_config()
        assert isinstance(cfg, DaemonConfig)
        # workspace_dir gets resolved to absolute
        assert cfg.workspace_dir.is_absolute()
        assert cfg.disk.min_free_gb == pytest.approx(2.0)
        assert cfg.disk.min_free_percent == pytest.approx(10.0)
        assert cfg.max_retries == 3
        assert cfg.poll_interval_idle_seconds == pytest.approx(5.0)

    def test_explicit_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_daemon_config(explicit_path=tmp_path / "absent.json")

    def test_env_var_path_used(self, tmp_path: Path, monkeypatch) -> None:
        cfg_file = tmp_path / "fmt.json"
        cfg_file.write_text(
            json.dumps({"workspace_dir": str(tmp_path / "ws"), "max_retries": 7}),
            encoding="utf-8",
        )
        monkeypatch.setenv("EPUBCTL_DAEMON_CONFIG", str(cfg_file))
        cfg = load_daemon_config()
        assert cfg.max_retries == 7
        assert cfg.workspace_dir == (tmp_path / "ws").resolve()


class TestParseConfig:
    def test_full_file(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "fmt.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "workspace_dir": str(tmp_path / "ws"),
                    "sqlite_path": "custom.sqlite",
                    "log_level": "DEBUG",
                    "log_format": "json",
                    "max_retries": 5,
                    "disk": {"min_free_gb": 5.0, "min_free_percent": 15.0},
                    "shutdown_grace_seconds": 60,
                    "poll_interval_idle_seconds": 2.0,
                    "poll_interval_paused_seconds": 120.0,
                    "notification_command": "echo $JOB_ID",
                }
            ),
            encoding="utf-8",
        )
        cfg = load_daemon_config(explicit_path=cfg_file)
        assert cfg.workspace_dir == (tmp_path / "ws").resolve()
        assert cfg.sqlite_path == Path("custom.sqlite")
        assert cfg.log_level == "DEBUG"
        assert cfg.log_format == "json"
        assert cfg.max_retries == 5
        assert cfg.disk == DiskCircuitConfig(min_free_gb=5.0, min_free_percent=15.0)
        assert cfg.shutdown_grace_seconds == 60
        assert cfg.poll_interval_idle_seconds == pytest.approx(2.0)
        assert cfg.poll_interval_paused_seconds == pytest.approx(120.0)
        assert cfg.notification_command == "echo $JOB_ID"

    def test_relative_workspace_is_resolved(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        cfg_file = tmp_path / "fmt.json"
        cfg_file.write_text(json.dumps({"workspace_dir": "rel_ws"}), encoding="utf-8")
        cfg = load_daemon_config(explicit_path=cfg_file)
        assert cfg.workspace_dir.is_absolute()
        assert cfg.workspace_dir == (tmp_path / "rel_ws").resolve()

    def test_unknown_keys_collected_and_warned(self, tmp_path: Path, caplog) -> None:
        cfg_file = tmp_path / "fmt.json"
        cfg_file.write_text(
            json.dumps({"workspace_dir": str(tmp_path), "made_up": 42, "also_bad": "x"}),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="epub_commentor.daemon.config"):
            cfg = load_daemon_config(explicit_path=cfg_file)
        assert set(cfg.unknown_keys) == {"made_up", "also_bad"}
        assert any("ignoring unknown keys" in rec.message for rec in caplog.records)

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "fmt.json"
        cfg_file.write_text("not json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="not valid JSON"):
            load_daemon_config(explicit_path=cfg_file)

    def test_non_object_raises(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "fmt.json"
        cfg_file.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(RuntimeError, match="must be a JSON object"):
            load_daemon_config(explicit_path=cfg_file)

    def test_disk_must_be_object(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "fmt.json"
        cfg_file.write_text(json.dumps({"disk": 42}), encoding="utf-8")
        with pytest.raises(RuntimeError, match='"disk" must be a JSON object'):
            load_daemon_config(explicit_path=cfg_file)


class TestResolveSqlitePath:
    def test_default_is_workspace_daemon_sqlite(self) -> None:
        cfg = DaemonConfig(workspace_dir=Path("/tmp/ws"))
        assert cfg.resolve_sqlite_path() == Path("/tmp/ws/daemon.sqlite").resolve()

    def test_absolute_explicit(self) -> None:
        cfg = DaemonConfig(workspace_dir=Path("/tmp/ws"), sqlite_path=Path("/var/lib/x.db"))
        assert cfg.resolve_sqlite_path() == Path("/var/lib/x.db").resolve()

    def test_relative_resolved_against_workspace(self) -> None:
        cfg = DaemonConfig(workspace_dir=Path("/tmp/ws"), sqlite_path=Path("custom.db"))
        assert cfg.resolve_sqlite_path() == Path("/tmp/ws/custom.db").resolve()


class TestLocateConfig:
    def test_explicit_wins(self, tmp_path: Path) -> None:
        f = tmp_path / "x.json"
        f.write_text("{}", encoding="utf-8")
        assert _locate_config(f) == f.resolve()

    def test_explicit_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            _locate_config(tmp_path / "absent.json")

    def test_env_var(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "env.json"
        f.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("EPUBCTL_DAEMON_CONFIG", str(f))
        assert _locate_config(None) == f.resolve()

    def test_env_var_missing_raises(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("EPUBCTL_DAEMON_CONFIG", str(tmp_path / "absent.json"))
        with pytest.raises(FileNotFoundError):
            _locate_config(None)

    def test_cwd_fallback(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "format.daemon.json"
        f.write_text("{}", encoding="utf-8")
        assert _locate_config(None) == f.resolve()

    def test_returns_none_when_absent(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("EPUBCTL_DAEMON_CONFIG", raising=False)
        assert _locate_config(None) is None
