"""Apply gate tests; all tests use dry-run and temporary Git state."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "hermes_upstream_apply.py"


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    upstream = tmp_path / "upstream.git"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Hermes test")
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "one")
    (repo / "history").write_text("two\n", encoding="utf-8")
    git(repo, "add", "history")
    git(repo, "commit", "-m", "two")
    subprocess.run(["git", "init", "--bare", str(upstream)], check=True, stdout=subprocess.DEVNULL)
    git(repo, "remote", "add", "upstream", str(upstream))
    git(repo, "push", "upstream", "main")
    return repo


def invoke(repo: Path, state: Path, token: str, *extra: str) -> tuple[int, dict]:
    env = os.environ.copy()
    env["HERMES_UPSTREAM_APPROVAL_TOKEN"] = token
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--state-dir", str(state), "--run-id", "run-apply", "--now", "2026-09-05T10:00:00Z", *extra],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
    )
    return result.returncode, json.loads(result.stdout)


def candidate(repo: Path, state: Path, *, approved: bool) -> None:
    state.joinpath("candidates").mkdir(parents=True)
    head = git(repo, "rev-parse", "HEAD")
    parent = git(repo, "rev-parse", "HEAD^")
    value = {
        "run_id": "run-apply", "status": "APPROVED" if approved else "PENDING",
        "created_at_utc": "2026-09-05T00:00:00Z", "candidate_sha": head,
        "source_sha": head, "parent_sha": parent, "release_id": "release-apply",
        "review_branch": "refs/heads/main",
        "approval": {"approved_by": "operator", "approval_token_sha256": hashlib.sha256(b"ok").hexdigest()},
    }
    state.joinpath("candidates", "run-apply.json").write_text(json.dumps(value), encoding="utf-8")


def test_unapproved_candidate_is_blocked(tmp_path: Path):
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    candidate(repo, state, approved=False)
    code, result = invoke(repo, state, "ok")
    assert code == 1
    assert result["error_code"] == "NOT_APPROVED"


def test_approved_apply_defaults_to_dry_run(tmp_path: Path):
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    candidate(repo, state, approved=True)
    code, result = invoke(repo, state, "ok")
    assert code == 0
    assert result["status"] == "APPROVED"
    assert result["dry_run"] is True
    assert not (tmp_path / "releases").exists()
