"""Regression tests for isolated rebase-based review candidates."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "hermes_upstream_review.py"


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
def repo_fixture(tmp_path: Path) -> tuple[Path, Path]:
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


def run_review(repo: Path, state: Path, *extra: str) -> tuple[int, dict]:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--run-id",
            "review-test",
            "--now",
            "2026-09-05T10:00:00Z",
            "--worktree-root",
            str(state / "worktrees"),
            "--json",
            *extra,
        ],
        text=True,
        stdout=subprocess.PIPE,
        check=False,
    )
    return completed.returncode, json.loads(completed.stdout)


def push_upstream_change(upstream: Path, tmp_path: Path, content: str) -> None:
    clone = tmp_path / "upstream-clone"
    subprocess.run(["git", "clone", str(upstream), str(clone)], check=True, stdout=subprocess.DEVNULL)
    git(clone, "config", "user.email", "test@example.invalid")
    git(clone, "config", "user.name", "Hermes test")
    (clone / "README.md").write_text(content, encoding="utf-8")
    git(clone, "add", "README.md")
    git(clone, "commit", "-m", "upstream change")
    git(clone, "push", "origin", "main")


def test_review_creates_pending_candidate_and_review_ref(repo_fixture, tmp_path: Path):
    repo, _ = repo_fixture
    state = tmp_path / "state"
    before = git(repo, "show-ref")

    code, result = run_review(repo, state)

    assert code == 0
    assert result["status"] == "PENDING"
    candidate = json.loads((state / "candidates" / "review-test.json").read_text())
    assert candidate["status"] == "PENDING"
    assert candidate["checks"]["noop"] is True
    assert git(repo, "rev-parse", "refs/upstream/review/review-test") == candidate["candidate_sha"]
    assert git(repo, "show-ref") != before
    assert not (state / "update.lock").exists()
    assert not (state / "worktrees" / "review-test").exists()


def test_rebase_conflict_is_blocked_without_review_ref(repo_fixture, tmp_path: Path):
    repo, upstream = repo_fixture
    push_upstream_change(upstream, tmp_path, "upstream conflicting change\n")
    (repo / "README.md").write_text("local conflicting change\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "local change")
    state = tmp_path / "state"

    code, result = run_review(repo, state)

    assert code == 1
    assert result["status"] == "BLOCKED"
    candidate = json.loads((state / "candidates" / "review-test.json").read_text())
    assert candidate["error_code"] == "REBASE_CONFLICT"
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "refs/upstream/review/review-test"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode != 0
    assert not (state / "update.lock").exists()
    assert not (state / "worktrees" / "review-test").exists()
