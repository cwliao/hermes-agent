"""Regression tests for the read-only upstream preflight contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "hermes_upstream_preflight.py"


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def git_fixture(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    upstream = tmp_path / "upstream.git"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Hermes test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "baseline")
    subprocess.run(["git", "init", "--bare", str(upstream)], check=True, stdout=subprocess.DEVNULL)
    git(repo, "remote", "add", "upstream", str(upstream))
    git(repo, "push", "upstream", "main")
    return repo, upstream


def run_preflight(repo: Path, state: Path, *extra: str) -> tuple[int, dict]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--repo",
        str(repo),
        "--state-dir",
        str(state),
        "--now",
        "2026-09-05T10:00:00Z",
        "--json",
        *extra,
    ]
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, check=False)
    return completed.returncode, json.loads(completed.stdout)


def test_clean_preflight_is_ready_and_does_not_update_refs(git_fixture, tmp_path: Path):
    repo, _ = git_fixture
    before = git(repo, "show-ref")
    code, result = run_preflight(repo, tmp_path / "state")
    after = git(repo, "show-ref")

    assert code == 0
    assert result["status"] == "READY"
    assert result["read_only_contract"]["git_ref_updates"] is False
    assert before == after
    assert Path(result["artifact_path"]).is_file()


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("dirty", "DIRTY_WORKTREE"),
        ("branch", "NON_MAIN_BRANCH"),
        ("fetch", "UPSTREAM_FETCH_FAIL"),
    ],
)
def test_preflight_reports_actionable_repository_failures(git_fixture, tmp_path: Path, setup, expected):
    repo, _ = git_fixture
    if setup == "dirty":
        (repo / "uncommitted.txt").write_text("do not lose\n", encoding="utf-8")
    elif setup == "branch":
        git(repo, "switch", "-c", "not-main")
    else:
        git(repo, "remote", "set-url", "upstream", str(tmp_path / "missing-upstream.git"))

    code, result = run_preflight(repo, tmp_path / "state")

    assert code == 1
    assert result["status"] in {"BLOCKED", "FAILED"}
    issue = next(item for item in result["issues"] if item["code"] == expected)
    assert issue["next_steps"]


def test_stale_lock_and_marker_mismatch_are_fail_closed(git_fixture, tmp_path: Path):
    repo, _ = git_fixture
    state = tmp_path / "state"
    candidates = state / "candidates"
    candidates.mkdir(parents=True)
    head = git(repo, "rev-parse", "HEAD")
    candidate = {
        "run_id": "run-1",
        "status": "APPROVED",
        "created_at_utc": "2026-09-05T00:00:00Z",
        "candidate_sha": head,
        "source_sha": head,
        "parent_sha": head,
        "release_id": "release-1",
        "review_branch": "refs/heads/main",
    }
    (candidates / "run-1.json").write_text(json.dumps(candidate), encoding="utf-8")
    (state / "update.lock").write_text(
        json.dumps({"owner": "dead", "run_id": "run-1", "pid": 1, "expires_at": "2026-09-04T00:00:00Z"}),
        encoding="utf-8",
    )
    (state / "apply.marker.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "candidate_sha": "0" * 40,
                "release_id": "wrong-release",
                "metadata_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    code, result = run_preflight(repo, state, "--mode", "apply", "--run-id", "run-1")

    assert code == 1
    codes = {item["code"] for item in result["issues"]}
    assert {"STALE_LOCK", "MARKER_SIGNATURE_MISMATCH"} <= codes


def test_push_rejected_candidate_is_not_treated_as_success(git_fixture, tmp_path: Path):
    repo, _ = git_fixture
    state = tmp_path / "state"
    candidates = state / "candidates"
    candidates.mkdir(parents=True)
    head = git(repo, "rev-parse", "HEAD")
    candidate = {
        "run_id": "run-2",
        "status": "PENDING",
        "created_at_utc": "2026-09-05T00:00:00Z",
        "candidate_sha": head,
        "source_sha": head,
        "parent_sha": head,
        "release_id": "release-2",
        "review_branch": "refs/heads/main",
        "push_status": "rejected",
    }
    (candidates / "run-2.json").write_text(json.dumps(candidate), encoding="utf-8")

    code, result = run_preflight(repo, state)

    assert code == 1
    assert any(item["code"] == "PUSH_REJECTED" for item in result["issues"])
