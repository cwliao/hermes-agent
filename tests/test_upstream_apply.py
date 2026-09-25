"""Apply gate tests; all tests use dry-run and temporary Git state."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from hermes_upstream_apply import (  # noqa: E402
    _compute_dropin_path,
    _render_dropin,
    _target_python_version,
)


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


def test_compute_dropin_path_sorts_after_every_existing_z_prefixed_file(tmp_path: Path):
    dropin_dir = tmp_path / "drop-ins"
    dropin_dir.mkdir()
    (dropin_dir / ("z" * 180 + "-some-ad-hoc-branch.conf")).write_text("", encoding="utf-8")
    (dropin_dir / ("z" * 60 + "-older-release.conf")).write_text("", encoding="utf-8")
    (dropin_dir / "10-corporate-tls-ca.conf").write_text("", encoding="utf-8")

    computed = _compute_dropin_path(dropin_dir, "530cd6ff91f5edc163e37e07a767c0caf585cb66")

    existing = sorted(p.name for p in dropin_dir.iterdir())
    assert sorted(existing + [computed.name])[-1] == computed.name


def test_compute_dropin_path_on_empty_directory(tmp_path: Path):
    dropin_dir = tmp_path / "empty-drop-ins"
    dropin_dir.mkdir()
    computed = _compute_dropin_path(dropin_dir, "abc1234567" + "0" * 30)
    assert computed.name.startswith("z" * 20)


def test_render_dropin_replaces_release_specific_keys_and_preserves_the_rest(tmp_path: Path):
    previous = "\n".join([
        "[Service]",
        "ExecStart=",
        "ExecStart=/home/cwliao/.hermes/venvs/gateway-old/bin/python -m hermes_cli.main gateway run",
        "ExecStopPost=",
        "ExecStopPost=-/home/cwliao/.hermes/venvs/gateway-old/bin/python -m gateway.cgroup_cleanup",
        "WorkingDirectory=/home/cwliao/.hermes/releases/old-release",
        "Environment=PYTHONPATH=/home/cwliao/.hermes/releases/old-release",
        "Environment=VIRTUAL_ENV=/home/cwliao/.hermes/venvs/gateway-old",
        "Environment=HERMES_RELEASE_SHA=oldsha",
        "Environment=PATH=/home/cwliao/.hermes/venvs/gateway-old/bin:/usr/bin:/bin",
        "Environment=HERMES_HOME=/home/cwliao/.hermes",
        "Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        "",
    ])
    venv_dir = Path("/home/cwliao/.hermes/venvs/gateway-new")
    destination = Path("/home/cwliao/.hermes/releases/new-release")

    rendered = _render_dropin(previous, venv_dir, destination, "newsha1234")

    assert f"ExecStart={venv_dir}/bin/python -m hermes_cli.main gateway run" in rendered
    assert f"ExecStopPost=-{venv_dir}/bin/python -m gateway.cgroup_cleanup" in rendered
    assert f"WorkingDirectory={destination}" in rendered
    assert f"Environment=PYTHONPATH={destination}" in rendered
    assert f"Environment=VIRTUAL_ENV={venv_dir}" in rendered
    assert "Environment=HERMES_RELEASE_SHA=newsha1234" in rendered
    assert f"Environment=PATH={venv_dir}/bin:/usr/bin:/bin" in rendered
    # Keys the release doesn't touch must survive untouched, byte-for-byte.
    assert "Environment=HERMES_HOME=/home/cwliao/.hermes" in rendered
    assert "Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in rendered
    # Old release's values must not leak through.
    assert "old-release" not in rendered
    assert "gateway-old" not in rendered
    assert "oldsha" not in rendered


def test_target_python_version_reads_tool_uv_environments(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        "default-groups = []\n"
        "environments = [\"python_version >= '3.14'\"]\n",
        encoding="utf-8",
    )
    assert _target_python_version(pyproject) == "3.14"


def test_target_python_version_falls_back_when_undeclared(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = \"x\"\n", encoding="utf-8")
    version = _target_python_version(pyproject)
    assert version == f"{sys.version_info.major}.{sys.version_info.minor}"
