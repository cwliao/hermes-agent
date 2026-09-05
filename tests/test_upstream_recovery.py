"""Recovery coverage for stale candidates and failed release restart."""

from __future__ import annotations

import hashlib
import json
import subprocess
from argparse import Namespace
from pathlib import Path

import scripts.hermes_upstream_apply as apply_module


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


def write_candidate(repo: Path, state: Path, *, created: str, status: str = "APPROVED") -> Path:
    path = state / "candidates" / "run.json"
    path.parent.mkdir(parents=True)
    head = git(repo, "rev-parse", "HEAD")
    value = {
        "run_id": "run", "status": status, "created_at_utc": created,
        "candidate_sha": head, "source_sha": head, "parent_sha": git(repo, "rev-parse", "HEAD^"),
        "release_id": "release-run", "review_branch": "refs/heads/main",
        "approval": {"approved_by": "operator", "approval_token_sha256": hashlib.sha256(b"ok").hexdigest()},
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_expired_candidate_is_blocked(tmp_path: Path):
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    write_candidate(repo, state, created="2026-08-01T00:00:00Z", status="PENDING")
    result = subprocess.run(
        ["python3", str(Path(__file__).parents[1] / "scripts/hermes_upstream_preflight.py"), "--repo", str(repo), "--state-dir", str(state), "--now", "2026-09-05T10:00:00Z", "--json"],
        text=True, stdout=subprocess.PIPE, check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert any(item["code"] == "STALE_REVIEW_CANDIDATE" for item in payload["issues"])


def test_restart_failure_restores_previous_dropin(tmp_path: Path, monkeypatch):
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    write_candidate(repo, state, created="2026-09-05T00:00:00Z")
    old_dropin = tmp_path / "old.conf"
    old_dropin.write_text("previous-release\n", encoding="utf-8")
    new_dropin = tmp_path / "new.conf"

    real_run = apply_module._run

    def fake_run(command, *, check=False):
        if command[:3] == ["systemctl", "--user", "restart"]:
            return subprocess.CompletedProcess(command, 1, "", "restart failed")
        return real_run(command, check=check)

    monkeypatch.setenv("HERMES_UPSTREAM_APPROVAL_TOKEN", "ok")
    monkeypatch.setattr(apply_module, "_run", fake_run)
    args = Namespace(
        repo=str(repo), state_dir=str(state), run_id="run", release_root=str(tmp_path / "releases"),
        systemd_dropin=str(new_dropin), systemd_unit="hermes-gateway.service", previous_release="previous-release",
        previous_dropin=str(old_dropin), upstream_remote="upstream", upstream_ref="main",
        review_ttl_seconds=7 * 24 * 60 * 60, now="2026-09-05T10:00:00Z", execute=True,
    )

    code, result = apply_module.apply(args)

    assert code == 1
    assert result["status"] == "FAILED"
    assert old_dropin.read_text(encoding="utf-8") == "previous-release\n"
    assert json.loads((state / "candidates" / "run.json").read_text())["status"] == "FAILED"
    assert not (state / "apply-worktrees" / "run").exists()
