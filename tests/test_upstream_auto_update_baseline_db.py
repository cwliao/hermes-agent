"""Focused contracts for the upstream updater's persisted baseline-failure cache."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts" / "hermes_upstream_auto_update.py"
SPEC = importlib.util.spec_from_file_location("hermes_upstream_auto_update", SCRIPT)
assert SPEC and SPEC.loader
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


NODEID = "tests/example/test_known.py::test_old_failure"
TEST_FILE = "tests/example/test_known.py"


def _python_path(tmp_path: Path) -> Path:
    path = tmp_path / ".venv" / "bin" / "python3"
    path.parent.mkdir(parents=True)
    path.touch()
    return path


def _result(returncode: int, stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def _write_db(state_dir: Path, nodeid: str = NODEID) -> Path:
    state_dir.mkdir()
    path = state_dir / "known_baseline_test_failures.json"
    path.write_text(
        json.dumps(
            {
                nodeid: {
                    "first_confirmed_utc": "2026-09-01T00:00:00Z",
                    "last_confirmed_utc": "2026-09-01T00:00:00Z",
                    "last_error_summary": "old assertion",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_known_failure_uses_fast_path_without_live_baseline_check(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    db_path = _write_db(state_dir)
    _python_path(tmp_path)
    monkeypatch.setattr(updater, "_available_memory_mb", lambda: None)
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *_args, **_kwargs: _result(1, f"FAILED {NODEID} - AssertionError: old\n"),
    )
    monkeypatch.setattr(
        updater,
        "_chunk_failures_on_baseline",
        lambda *_args: (_ for _ in ()).throw(AssertionError("live baseline check should be skipped")),
    )

    ok, _ = updater._run_scoped_tests(
        tmp_path,
        tmp_path,
        [TEST_FILE],
        state_dir,
    )

    # The fake interpreter path is enough because _run_chunk is patched through subprocess.
    assert ok is True
    data = json.loads(db_path.read_text(encoding="utf-8"))
    assert data[NODEID]["last_confirmed_utc"] != "2026-09-01T00:00:00Z"


def test_unknown_failure_falls_back_to_live_baseline_check(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    _python_path(tmp_path)
    monkeypatch.setattr(updater, "_available_memory_mb", lambda: None)
    calls = []

    def baseline(_venv_repo, chunk):
        calls.append(chunk)
        return {NODEID}

    monkeypatch.setattr(updater, "_chunk_failures_on_baseline", baseline)
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *_args, **_kwargs: _result(1, f"FAILED {NODEID} - AssertionError: old\n"),
    )

    ok, _ = updater._run_scoped_tests(tmp_path, tmp_path, [TEST_FILE], state_dir)

    assert ok is True
    assert calls == [[TEST_FILE]]


def test_live_confirmed_failure_is_persisted(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    _python_path(tmp_path)
    monkeypatch.setattr(updater, "_available_memory_mb", lambda: None)
    monkeypatch.setattr(updater, "_chunk_failures_on_baseline", lambda *_args: {NODEID})
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *_args, **_kwargs: _result(1, f"FAILED {NODEID} - AssertionError: old\n"),
    )

    ok, _ = updater._run_scoped_tests(tmp_path, tmp_path, [TEST_FILE], state_dir)

    assert ok is True
    data = json.loads((state_dir / "known_baseline_test_failures.json").read_text(encoding="utf-8"))
    assert data[NODEID]["first_confirmed_utc"] == data[NODEID]["last_confirmed_utc"]
    assert "AssertionError" in data[NODEID]["last_error_summary"]


def test_fixed_known_failure_is_removed_after_clean_run(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    db_path = _write_db(state_dir)
    _python_path(tmp_path)
    monkeypatch.setattr(updater, "_available_memory_mb", lambda: None)
    monkeypatch.setattr(updater.subprocess, "run", lambda *_args, **_kwargs: _result(0, "1 passed\n"))

    ok, _ = updater._run_scoped_tests(tmp_path, tmp_path, [TEST_FILE], state_dir)

    assert ok is True
    assert json.loads(db_path.read_text(encoding="utf-8")) == {}
