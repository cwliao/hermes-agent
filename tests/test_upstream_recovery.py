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


def write_done_candidate_with_ref(repo: Path, state: Path, *, run_id: str, status: str) -> str:
    """Write a terminal-status (DONE/SUPERSEDED) candidate whose metadata
    still names a real refs/upstream/review/<run_id> ref, and create that
    ref in the repo -- mirroring what a completed real update leaves behind
    (see docs/plans/2026-09-06-upstream-preflight-orphan-ref-001.md)."""
    review_ref = f"refs/upstream/review/{run_id}"
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", review_ref, head)
    path = state / "candidates" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "run_id": run_id, "status": status, "created_at_utc": "2026-09-06T05:03:37Z",
        "candidate_sha": head, "source_sha": head, "parent_sha": git(repo, "rev-parse", "HEAD^"),
        "release_id": f"release-{run_id}", "review_branch": review_ref,
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return review_ref


def test_done_candidate_ref_is_not_flagged_as_orphan(tmp_path: Path):
    """Regression test for UPSTREAM-PREFLIGHT-ORPHAN-REF-001: a review ref
    left behind by a candidate that finished DONE (successfully applied) or
    SUPERSEDED must not be reported as an orphan -- its metadata still
    exists and explains it; it just isn't PENDING/APPROVED any more."""
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    (state / "candidates").mkdir(parents=True)
    write_done_candidate_with_ref(repo, state, run_id="20260906-050337", status="DONE")

    result = subprocess.run(
        ["python3", str(Path(__file__).parents[1] / "scripts/hermes_upstream_preflight.py"),
         "--repo", str(repo), "--state-dir", str(state), "--now", "2026-09-08T10:00:00Z", "--json"],
        text=True, stdout=subprocess.PIPE, check=False,
    )
    payload = json.loads(result.stdout)
    orphan_issues = [
        item for item in payload["issues"]
        if item["code"] == "STALE_REVIEW_CANDIDATE" and "沒有 metadata 對應" in item["message"]
    ]
    assert orphan_issues == [], orphan_issues


def test_ref_with_no_metadata_at_all_is_still_flagged_as_orphan(tmp_path: Path):
    """A review ref with genuinely no backing candidate file anywhere must
    still fail closed -- the fix must not weaken true-orphan detection."""
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    (state / "candidates").mkdir(parents=True)
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/upstream/review/no-metadata-at-all", head)

    result = subprocess.run(
        ["python3", str(Path(__file__).parents[1] / "scripts/hermes_upstream_preflight.py"),
         "--repo", str(repo), "--state-dir", str(state), "--now", "2026-09-08T10:00:00Z", "--json"],
        text=True, stdout=subprocess.PIPE, check=False,
    )
    payload = json.loads(result.stdout)
    orphan_issues = [
        item for item in payload["issues"]
        if item["code"] == "STALE_REVIEW_CANDIDATE" and "沒有 metadata 對應" in item["message"]
    ]
    assert len(orphan_issues) == 1
    assert "no-metadata-at-all" in orphan_issues[0]["message"]


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
