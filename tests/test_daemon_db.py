"""Tests for :mod:`epub_commentor.daemon.db`.

Coverage strategy: every DAO function gets at least one happy-path test
and one boundary test (None, empty result, terminal state). The schema
is created in a fresh per-test tmp DB so parallel test runs are safe.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from epub_commentor.daemon import db


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh in-memory-style connection on disk for each test."""
    c = db.connect(tmp_path / "test.sqlite")
    db.init_schema(c)
    return c


def _make_job(conn, **kwargs) -> int:
    """Insert a PENDING job and return its id."""
    defaults = {
        "file_name": "book.epub",
        "source_path": "/tmp/job_x/input.epub",
        "priority": 0,
        "book_synopsis": None,
        "flags": None,
        "max_retries": 3,
    }
    defaults.update(kwargs)
    return db.insert_job(conn, **defaults)


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------


class TestConnection:
    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "x.sqlite"
        c = db.connect(target)
        try:
            assert target.parent.exists()
        finally:
            c.close()

    def test_wal_enabled(self, conn: sqlite3.Connection) -> None:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        # ``connect`` issues ``PRAGMA journal_mode=WAL`` which switches
        # the journal mode in place; the value read back should be ``wal``.
        assert mode.lower() == "wal"

    def test_foreign_keys_enabled(self, conn: sqlite3.Connection) -> None:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1

    def test_init_schema_is_idempotent(self, conn: sqlite3.Connection) -> None:
        db.init_schema(conn)
        db.init_schema(conn)
        # Both calls must succeed without error.
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r[0] for r in rows}
        assert {"jobs", "events", "control_signals", "server_stats"}.issubset(names)


# ---------------------------------------------------------------------------
# insert + fetch
# ---------------------------------------------------------------------------


class TestInsertAndFetch:
    def test_insert_returns_id(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn, file_name="a.epub")
        assert job_id == 1

    def test_insert_records_enqueued_event(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        events = db.list_events(conn, job_id)
        assert len(events) == 1
        assert events[0]["kind"] == db.EVENT_ENQUEUED

    def test_fetch_by_id(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn, file_name="x.epub", priority=5)
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.id == job_id
        assert job.file_name == "x.epub"
        assert job.priority == 5
        assert job.status == db.STATUS_PENDING
        assert job.flags == {}
        assert job.retry_count == 0

    def test_fetch_missing_returns_none(self, conn: sqlite3.Connection) -> None:
        assert db.fetch_job(conn, 9999) is None

    def test_flags_round_trip(self, conn: sqlite3.Connection) -> None:
        flags = {"ai_select": True, "no_review": True, "block_size": 8}
        job_id = _make_job(conn, flags=flags)
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.flags == flags

    def test_list_jobs_orders_newest_first(self, conn: sqlite3.Connection) -> None:
        _make_job(conn, file_name="first.epub")
        _make_job(conn, file_name="second.epub")
        _make_job(conn, file_name="third.epub")
        jobs = db.list_jobs(conn)
        assert [j.file_name for j in jobs] == ["third.epub", "second.epub", "first.epub"]

    def test_list_jobs_filter_by_status(self, conn: sqlite3.Connection) -> None:
        a = _make_job(conn, file_name="a.epub")
        _make_job(conn, file_name="b.epub")
        db.mark_processing(conn, a)
        db.mark_success(
            conn,
            a,
            output_path="/tmp/x",
            input_tokens=10,
            output_tokens=5,
            cache_tokens=0,
            chapters_processed=3,
            chapters_skipped=0,
            total_comments=10,
        )
        done = db.list_jobs(conn, status=db.STATUS_SUCCESS)
        assert len(done) == 1
        assert done[0].file_name == "a.epub"
        pending = db.list_jobs(conn, status=db.STATUS_PENDING)
        assert len(pending) == 1
        assert pending[0].file_name == "b.epub"

    def test_queue_depths(self, conn: sqlite3.Connection) -> None:
        for _ in range(3):
            _make_job(conn)
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        depths = db.queue_depths(conn)
        assert depths[db.STATUS_PENDING] == 3
        assert depths[db.STATUS_PROCESSING] == 1


class TestFetchNextPending:
    def test_picks_highest_priority(self, conn: sqlite3.Connection) -> None:
        _make_job(conn, priority=1)
        high = _make_job(conn, priority=10)
        _make_job(conn, priority=5)
        job = db.fetch_next_pending(conn)
        assert job is not None
        assert job.id == high

    def test_ties_broken_by_created_at(self, conn: sqlite3.Connection) -> None:
        # Created sequentially; same priority 0, so older first.
        first = _make_job(conn)
        second = _make_job(conn)
        job = db.fetch_next_pending(conn)
        assert job is not None
        assert job.id == first
        assert job.id != second

    def test_empty_returns_none(self, conn: sqlite3.Connection) -> None:
        assert db.fetch_next_pending(conn) is None

    def test_skips_processing(self, conn: sqlite3.Connection) -> None:
        a = _make_job(conn)
        b = _make_job(conn)
        db.mark_processing(conn, a)
        job = db.fetch_next_pending(conn)
        assert job is not None
        assert job.id == b


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


class TestMarkProcessing:
    def test_pending_to_processing(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_PROCESSING
        assert job.started_at is not None

    def test_paused_to_processing_preserves_started_at(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        first_started = db.fetch_job(conn, job_id).started_at
        db.pause(conn, job_id)
        db.mark_processing(conn, job_id)
        job = db.fetch_job(conn, job_id)
        assert job is not None
        # COALESCE(started_at, ?) should preserve the first value.
        assert job.started_at == first_started

    def test_records_started_event(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        kinds = [e["kind"] for e in db.list_events(conn, job_id)]
        assert db.EVENT_STARTED in kinds


class TestMarkSuccess:
    def test_sets_all_fields(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        db.mark_success(
            conn,
            job_id,
            output_path="/jobs/job_1/output.commented.epub",
            input_tokens=100,
            output_tokens=50,
            cache_tokens=10,
            chapters_processed=5,
            chapters_skipped=1,
            total_comments=20,
        )
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_SUCCESS
        assert job.output_path == "/jobs/job_1/output.commented.epub"
        assert job.input_tokens == 100
        assert job.output_tokens == 50
        assert job.cache_tokens == 10
        assert job.chapters_processed == 5
        assert job.chapters_skipped == 1
        assert job.total_comments == 20
        assert job.finished_at is not None


class TestMarkFailed:
    def test_records_stage_and_message(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        db.mark_failed(conn, job_id, stage="process", message="LLM returned bad JSON")
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_FAILED
        assert job.error_stage == "process"
        assert job.error_message == "LLM returned bad JSON"
        assert job.finished_at is not None


class TestMarkCancelled:
    def test_records_cancelled_state(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        db.mark_cancelled(conn, job_id)
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_CANCELLED
        assert job.error_stage == "cancelled"


class TestIncrementRetry:
    def test_resets_to_pending_when_under_budget(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        db.mark_failed(conn, job_id, stage="x", message="y")
        scheduled = db.increment_retry(conn, job_id)
        assert scheduled is True
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_PENDING
        assert job.retry_count == 1
        assert job.error_stage is None
        assert job.error_message is None

    def test_refuses_when_budget_exhausted(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn, max_retries=1)
        db.mark_processing(conn, job_id)
        db.mark_failed(conn, job_id, stage="x", message="y")
        db.increment_retry(conn, job_id)  # retry_count = 1 (= max)
        # Now we're at the cap; a second increment_retry should refuse.
        scheduled = db.increment_retry(conn, job_id)
        assert scheduled is False
        # Status unchanged from the previous FAILED→PENDING reset — but
        # the cap logic means we don't transition again.
        job = db.fetch_job(conn, job_id)
        assert job is not None
        # The job is left in its current state; the caller is responsible
        # for marking FAILED when increment_retry returns False.


# ---------------------------------------------------------------------------
# Priority / pause / resume
# ---------------------------------------------------------------------------


class TestPriority:
    def test_set_priority(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        ok = db.set_priority(conn, job_id, 99)
        assert ok is True
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.priority == 99

    def test_set_priority_missing(self, conn: sqlite3.Connection) -> None:
        assert db.set_priority(conn, 9999, 5) is False


class TestPause:
    def test_pauses_pending(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        assert db.pause(conn, job_id) is True
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_PAUSED

    def test_cannot_pause_processing(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        assert db.pause(conn, job_id) is False
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_PROCESSING

    def test_pause_records_event(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.pause(conn, job_id)
        events = db.list_events(conn, job_id)
        assert any(e["kind"] == db.EVENT_PAUSED for e in events)


class TestResume:
    def test_paused_to_pending(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.pause(conn, job_id)
        assert db.resume(conn, job_id) is True
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_PENDING

    def test_failed_to_pending_increments_retry(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        db.mark_failed(conn, job_id, stage="x", message="y")
        db.resume(conn, job_id)
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_PENDING
        assert job.retry_count == 1  # bumped on resume-from-FAILED

    def test_resume_success_is_noop(self, conn: sqlite3.Connection) -> None:
        # Once SUCCESS, resume() must not touch it.
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        db.mark_success(
            conn,
            job_id,
            output_path="x",
            input_tokens=0,
            output_tokens=0,
            cache_tokens=0,
            chapters_processed=0,
            chapters_skipped=0,
            total_comments=0,
        )
        assert db.resume(conn, job_id) is False


class TestBulkPauseResume:
    def test_pause_all(self, conn: sqlite3.Connection) -> None:
        a = _make_job(conn)
        b = _make_job(conn)
        c = _make_job(conn)
        db.mark_processing(conn, a)
        n = db.pause_all_non_terminal(conn, reason="disk_low")
        assert n == 3
        for jid in (a, b, c):
            job = db.fetch_job(conn, jid)
            assert job is not None
            assert job.status == db.STATUS_PAUSED

    def test_pause_skips_terminal(self, conn: sqlite3.Connection) -> None:
        done = _make_job(conn)
        db.mark_processing(conn, done)
        db.mark_success(
            conn,
            done,
            output_path="x",
            input_tokens=0,
            output_tokens=0,
            cache_tokens=0,
            chapters_processed=0,
            chapters_skipped=0,
            total_comments=0,
        )
        _make_job(conn)
        n = db.pause_all_non_terminal(conn, reason="test")
        assert n == 1
        # SUCCESS row should still be SUCCESS, not PAUSED.
        job = db.fetch_job(conn, done)
        assert job is not None
        assert job.status == db.STATUS_SUCCESS

    def test_resume_all(self, conn: sqlite3.Connection) -> None:
        a = _make_job(conn)
        b = _make_job(conn)
        db.pause(conn, a)
        db.pause(conn, b)
        n = db.resume_all_paused(conn)
        assert n == 2
        depths = db.queue_depths(conn)
        # No PAUSED jobs remain — use ``.get`` since ``queue_depths``
        # only reports statuses with at least one row.
        assert depths.get(db.STATUS_PAUSED, 0) == 0
        assert depths[db.STATUS_PENDING] == 2


# ---------------------------------------------------------------------------
# Control signals
# ---------------------------------------------------------------------------


class TestControlSignals:
    def test_send_then_fetch_clears(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.send_control_signal(conn, job_id, db.SIGNAL_CANCEL)
        sigs = db.fetch_control_signals(conn)
        assert len(sigs) == 1
        assert sigs[0]["job_id"] == job_id
        assert sigs[0]["kind"] == db.SIGNAL_CANCEL
        # Second fetch is empty (DELETE happened inside the transaction).
        assert db.fetch_control_signals(conn) == []

    def test_idempotent_same_kind(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.send_control_signal(conn, job_id, db.SIGNAL_CANCEL)
        db.send_control_signal(conn, job_id, db.SIGNAL_CANCEL)
        sigs = db.fetch_control_signals(conn)
        assert len(sigs) == 1

    def test_signals_for_multiple_jobs(self, conn: sqlite3.Connection) -> None:
        a = _make_job(conn)
        b = _make_job(conn)
        db.send_control_signal(conn, a, db.SIGNAL_CANCEL)
        db.send_control_signal(conn, b, db.SIGNAL_CANCEL)
        sigs = db.fetch_control_signals(conn)
        assert {s["job_id"] for s in sigs} == {a, b}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEvents:
    def test_list_orders_by_time_ascending(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        db.mark_processing(conn, job_id)
        db.mark_success(
            conn,
            job_id,
            output_path="x",
            input_tokens=0,
            output_tokens=0,
            cache_tokens=0,
            chapters_processed=0,
            chapters_skipped=0,
            total_comments=0,
        )
        events = db.list_events(conn, job_id)
        kinds = [e["kind"] for e in events]
        assert kinds[0] == db.EVENT_ENQUEUED
        assert db.EVENT_STARTED in kinds
        assert db.EVENT_FINISHED in kinds

    def test_limit(self, conn: sqlite3.Connection) -> None:
        job_id = _make_job(conn)
        for i in range(5):
            db.record_event(conn, job_id, f"custom_{i}")
        events = db.list_events(conn, job_id, limit=3)
        assert len(events) == 3


# ---------------------------------------------------------------------------
# Server stats
# ---------------------------------------------------------------------------


class TestServerStats:
    def test_record_then_fetch_latest(self, conn: sqlite3.Connection) -> None:
        db.record_stat(
            conn,
            cpu_percent=12.0,
            mem_percent=45.0,
            disk_used_percent=82.0,
            disk_available_gb=3.5,
            pending_jobs=2,
            processing_jobs=1,
        )
        latest = db.fetch_latest_stat(conn)
        assert latest is not None
        assert latest["cpu_percent"] == pytest.approx(12.0)
        assert latest["pending_jobs"] == 2

    def test_no_stats_returns_none(self, conn: sqlite3.Connection) -> None:
        assert db.fetch_latest_stat(conn) is None


# ---------------------------------------------------------------------------
# Crash recovery + prune
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    def test_processing_becomes_pending(self, conn: sqlite3.Connection) -> None:
        a = _make_job(conn)
        b = _make_job(conn)
        db.mark_processing(conn, a)
        db.mark_processing(conn, b)
        n = db.recover_crashed_jobs(conn)
        assert n == 2
        for jid in (a, b):
            job = db.fetch_job(conn, jid)
            assert job is not None
            assert job.status == db.STATUS_PENDING
            assert job.error_stage == "daemon_restart"

    def test_restart_event_recorded(self, conn: sqlite3.Connection) -> None:
        a = _make_job(conn)
        db.mark_processing(conn, a)
        db.recover_crashed_jobs(conn)
        events = db.list_events(conn, a)
        assert any(e["kind"] == db.EVENT_RESTARTED for e in events)

    def test_pending_jobs_unchanged(self, conn: sqlite3.Connection) -> None:
        a = _make_job(conn)
        db.recover_crashed_jobs(conn)
        job = db.fetch_job(conn, a)
        assert job is not None
        assert job.status == db.STATUS_PENDING
        # No restart event for never-started jobs.
        events = db.list_events(conn, a)
        assert not any(e["kind"] == db.EVENT_RESTARTED for e in events)


class TestDeleteJobs:
    def test_delete_removes_job_and_cascades_events(self, conn: sqlite3.Connection) -> None:
        a = _make_job(conn)
        _make_job(conn)  # keep
        db.mark_processing(conn, a)
        n = db.delete_jobs(conn, [a])
        assert n == 1
        assert db.fetch_job(conn, a) is None
        # Events are removed via FK cascade
        events = db.list_events(conn, a)
        assert events == []

    def test_delete_empty_returns_zero(self, conn: sqlite3.Connection) -> None:
        assert db.delete_jobs(conn, []) == 0
