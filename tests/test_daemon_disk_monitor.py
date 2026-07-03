"""Tests for :mod:`epub_commentor.daemon.disk_monitor`."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest import mock

import pytest

from epub_commentor.daemon.config import DiskCircuitConfig
from epub_commentor.daemon.disk_monitor import DiskMonitor


def _usage(free_gb: float, total_gb: float = 100.0) -> mock.Mock:
    """Build a shutil.disk_usage-shaped Mock with a given free/total."""
    usage = mock.Mock()
    usage.free = int(free_gb * 1024 ** 3)
    usage.total = int(total_gb * 1024 ** 3)
    usage.used = int((total_gb - free_gb) * 1024 ** 3)
    return usage


class TestBreakerTriggers:
    def test_above_thresholds_is_healthy(self, tmp_path: Path) -> None:
        m = DiskMonitor(DiskCircuitConfig(min_free_gb=2.0, min_free_percent=10.0), tmp_path)
        with mock.patch("epub_commentor.daemon.disk_monitor.shutil.disk_usage", return_value=_usage(50.0)):
            assert m.is_low() is False
        assert m.was_low() is False

    def test_low_avail_gb_trips(self, tmp_path: Path) -> None:
        m = DiskMonitor(DiskCircuitConfig(min_free_gb=2.0, min_free_percent=10.0), tmp_path)
        with mock.patch("epub_commentor.daemon.disk_monitor.shutil.disk_usage", return_value=_usage(1.0)):
            assert m.is_low() is True
        assert m.was_low() is True

    def test_high_used_percent_trips(self, tmp_path: Path) -> None:
        m = DiskMonitor(DiskCircuitConfig(min_free_gb=2.0, min_free_percent=10.0), tmp_path)
        # 100 GB total, 5 GB free → 95% used > 90% threshold
        with mock.patch("epub_commentor.daemon.disk_monitor.shutil.disk_usage", return_value=_usage(5.0, 100.0)):
            assert m.is_low() is True

    def test_below_gb_threshold_with_huge_percent(self, tmp_path: Path) -> None:
        m = DiskMonitor(DiskCircuitConfig(min_free_gb=2.0, min_free_percent=10.0), tmp_path)
        with mock.patch("epub_commentor.daemon.disk_monitor.shutil.disk_usage", return_value=_usage(1.9, 100.0)):
            assert m.is_low() is True


class TestEdgeDetection:
    def test_recovered_returns_true_once_on_transition(self, tmp_path: Path, caplog) -> None:
        m = DiskMonitor(DiskCircuitConfig(min_free_gb=2.0, min_free_percent=10.0), tmp_path)
        # Trip
        with mock.patch(
            "epub_commentor.daemon.disk_monitor.shutil.disk_usage",
            return_value=_usage(1.0),
        ):
            assert m.is_low() is True
        # Still tripped
        with mock.patch(
            "epub_commentor.daemon.disk_monitor.shutil.disk_usage",
            return_value=_usage(1.0),
        ):
            assert m.recovered() is False
        # Recovered: free disk returns
        with mock.patch(
            "epub_commentor.daemon.disk_monitor.shutil.disk_usage",
            return_value=_usage(50.0),
        ):
            with caplog.at_level(logging.INFO, logger="epub_commentor.daemon.disk_monitor"):
                assert m.recovered() is True
                assert any("recovered" in rec.message for rec in caplog.records)
        assert m.was_low() is False

    def test_recovered_idempotent_when_never_low(self, tmp_path: Path) -> None:
        m = DiskMonitor(DiskCircuitConfig(), tmp_path)
        with mock.patch(
            "epub_commentor.daemon.disk_monitor.shutil.disk_usage",
            return_value=_usage(50.0),
        ):
            assert m.recovered() is False

    def test_logged_warning_on_trip(self, tmp_path: Path, caplog) -> None:
        m = DiskMonitor(DiskCircuitConfig(), tmp_path)
        with mock.patch(
            "epub_commentor.daemon.disk_monitor.shutil.disk_usage",
            return_value=_usage(1.0),
        ):
            with caplog.at_level(logging.WARNING, logger="epub_commentor.daemon.disk_monitor"):
                m.is_low()
        assert any("circuit breaker tripped" in rec.message for rec in caplog.records)


class TestSnapshot:
    def test_returns_avail_gb_and_used_percent(self, tmp_path: Path) -> None:
        m = DiskMonitor(DiskCircuitConfig(), tmp_path)
        with mock.patch(
            "epub_commentor.daemon.disk_monitor.shutil.disk_usage",
            return_value=_usage(7.5, 100.0),
        ):
            avail_gb, used_pct = m.current_snapshot()
        assert avail_gb == pytest.approx(7.5)
        assert used_pct == pytest.approx(92.5)

    def test_zero_total_returns_zero_percent(self, tmp_path: Path) -> None:
        # Pathological case — should not crash on divide-by-zero.
        usage = mock.Mock()
        usage.free = 0
        usage.total = 0
        usage.used = 0
        m = DiskMonitor(DiskCircuitConfig(), tmp_path)
        with mock.patch("epub_commentor.daemon.disk_monitor.shutil.disk_usage", return_value=usage):
            _, pct = m.current_snapshot()
        assert pct == 0.0
