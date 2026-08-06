"""Tests for SessionDB WAL checkpoint strategy (issue #45383).

Verifies that periodic checkpoints use PASSIVE mode (safe for large DBs)
while close() and pre-VACUUM paths still use TRUNCATE.
"""

import sqlite3
import logging
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB, _backup_db_file


@pytest.fixture()
def db(tmp_path):
    """Create a SessionDB with a temp database file."""
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    try:
        session_db.close()
    except Exception:
        pass


class TestTryWalCheckpointPassive:
    """_try_wal_checkpoint() should use PASSIVE mode for periodic use."""

    def test_checkpoint_uses_passive_mode(self, db):
        """PASSIVE checkpoint does not require exclusive lock — safe for large DBs."""
        # Capture the real connection's execute before mocking
        real_conn = db._conn
        execute_calls = []

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        # sqlite3.Connection.execute is read-only (C extension) — replace _conn
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        mock_conn.fetchone.return_value = None
        db._conn = mock_conn

        db._try_wal_checkpoint()

        passive_calls = [c for c in execute_calls if "wal_checkpoint(PASSIVE)" in c]
        truncate_calls = [c for c in execute_calls if "wal_checkpoint(TRUNCATE)" in c]
        assert len(passive_calls) == 1, (
            f"Expected 1 PASSIVE checkpoint call, got {len(passive_calls)}"
        )
        assert len(truncate_calls) == 0, (
            "Periodic checkpoint should NOT use TRUNCATE"
        )

    def test_checkpoint_logs_warning_on_failure(self, db, caplog):
        """Failed PASSIVE checkpoint logs a warning instead of silent pass."""
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        db._conn = mock_conn

        with caplog.at_level(logging.WARNING):
            db._try_wal_checkpoint()

        assert any("WAL checkpoint (PASSIVE) failed" in r.message for r in caplog.records), (
            f"Expected warning log about PASSIVE checkpoint failure, got: {caplog.text}"
        )

    def test_checkpoint_returns_result_on_success(self, db):
        """Successful PASSIVE checkpoint does not raise."""
        db._try_wal_checkpoint()


class TestCloseCheckpoint:
    """close() folds safely and only resets a small WAL."""

    def _mock_connection(self, db):
        real_conn = db._conn
        execute_calls = []

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        db._conn = mock_conn
        return execute_calls

    def test_close_uses_passive_and_small_truncate(self, db):
        execute_calls = self._mock_connection(db)

        with patch.object(db, "_wal_reset_is_cheap", return_value=True):
            db.close()

        passive_calls = [c for c in execute_calls if "wal_checkpoint(PASSIVE)" in c]
        truncate_calls = [c for c in execute_calls if "wal_checkpoint(TRUNCATE)" in c]
        assert len(passive_calls) == 1
        assert len(truncate_calls) == 1

    def test_close_skips_truncate_for_large_wal(self, db):
        execute_calls = self._mock_connection(db)

        with patch.object(db, "_wal_reset_is_cheap", return_value=False):
            db.close()

        assert any("wal_checkpoint(PASSIVE)" in c for c in execute_calls)
        assert not any("wal_checkpoint(TRUNCATE)" in c for c in execute_calls)

    def test_close_skips_truncate_when_passive_fails(self, db, caplog):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        db._conn = mock_conn

        with caplog.at_level(logging.DEBUG):
            db.close()

        assert any("WAL checkpoint (PASSIVE) at close failed" in r.message for r in caplog.records)
        assert not any(
            call.args and "wal_checkpoint(TRUNCATE)" in str(call.args[0])
            for call in mock_conn.execute.call_args_list
        )

    def test_wal_reset_cheap_uses_size_threshold(self, db):
        wal_path = db.db_path.with_name(db.db_path.name + "-wal")
        assert db._wal_reset_is_cheap() is True
        wal_path.write_bytes(b"x" * (db._WAL_RESET_MAX_BYTES + 1))
        assert db._wal_reset_is_cheap() is False


def test_backup_sidecars_use_non_sqlite_names(tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"db")
    (tmp_path / "state.db-wal").write_bytes(b"wal")
    (tmp_path / "state.db-shm").write_bytes(b"shm")

    backup_path = _backup_db_file(db_path)

    assert backup_path is not None
    assert backup_path.read_bytes() == b"db"
    assert backup_path.with_name(backup_path.name + ".wal-copy").read_bytes() == b"wal"
    assert backup_path.with_name(backup_path.name + ".shm-copy").read_bytes() == b"shm"
    assert not backup_path.with_name(backup_path.name + "-wal").exists()
    assert not backup_path.with_name(backup_path.name + "-shm").exists()


class TestCheckpointFrequency:
    """Checkpoint triggers every N writes."""

    def test_checkpoint_triggers_at_interval(self, db):
        """_try_wal_checkpoint is called every _CHECKPOINT_EVERY_N_WRITES writes."""
        call_count = [0]
        original = db._try_wal_checkpoint

        def counting_checkpoint():
            call_count[0] += 1
            original()

        db._try_wal_checkpoint = counting_checkpoint

        # Write exactly _CHECKPOINT_EVERY_N_WRITES sessions to trigger one checkpoint
        n = db._CHECKPOINT_EVERY_N_WRITES
        import time as _time
        for i in range(n):
            db._execute_write(lambda conn, _i=i: conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                (f"sess_{_i}", "test", _time.time()),
            ))

        assert call_count[0] == 1, (
            f"Expected 1 checkpoint after {n} writes, got {call_count[0]}"
        )
