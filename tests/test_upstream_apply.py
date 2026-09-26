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
    _prune_superseded_dropins,
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


_FULL_DROPIN_TEMPLATE = """[Service]
ExecStart=
ExecStart=/venv-{sha}/bin/python -m hermes_cli.main gateway run
ExecStopPost=
ExecStopPost=-/venv-{sha}/bin/python -m gateway.cgroup_cleanup
WorkingDirectory=/releases/{sha}
Environment=PYTHONPATH=/releases/{sha}
Environment=VIRTUAL_ENV=/venv-{sha}
Environment=HERMES_RELEASE_SHA={sha}
Environment=PATH=/venv-{sha}/bin:/usr/bin
Environment=HERMES_HOME=/home/x/.hermes
Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
"""


def test_prune_superseded_dropins_removes_fully_overridden_files_only(tmp_path: Path):
    dropin_dir = tmp_path / "drop-ins"
    dropin_dir.mkdir()
    # Three self-managed releases in sort order: the last one's key set is a superset of
    # the earlier two, so both earlier ones are provably dead once it exists.
    (dropin_dir / ("z" * 20 + "-upstream-1111111111.conf")).write_text(
        _FULL_DROPIN_TEMPLATE.format(sha="1111111111"), encoding="utf-8")
    (dropin_dir / ("z" * 60 + "-upstream-2222222222.conf")).write_text(
        _FULL_DROPIN_TEMPLATE.format(sha="2222222222"), encoding="utf-8")
    (dropin_dir / ("z" * 100 + "-upstream-3333333333.conf")).write_text(
        _FULL_DROPIN_TEMPLATE.format(sha="3333333333"), encoding="utf-8")
    # A foundational, unrelated drop-in that only sets a key nothing later sets --
    # must survive since it is not provably dead.
    (dropin_dir / "10-corporate-tls-ca.conf").write_text(
        "[Service]\nEnvironment=CORPORATE_CA_ONLY=1\n", encoding="utf-8")

    removed = _prune_superseded_dropins(dropin_dir)

    assert set(removed) == {
        "z" * 20 + "-upstream-1111111111.conf",
        "z" * 60 + "-upstream-2222222222.conf",
    }
    remaining = {p.name for p in dropin_dir.iterdir()}
    assert remaining == {"z" * 100 + "-upstream-3333333333.conf", "10-corporate-tls-ca.conf"}


def test_compute_dropin_path_stays_short_after_many_releases(tmp_path: Path):
    """Regression test for the 2026-09-26 incident: after ~30 releases the z-prefix grew
    past NAME_MAX (255) and every further deploy's drop-in write failed outright. Pruning
    must keep the computed filename small regardless of how much history precedes it."""
    dropin_dir = tmp_path / "drop-ins"
    dropin_dir.mkdir()
    z_count = 20
    for i in range(30):
        sha = f"{i:010x}"
        (dropin_dir / (("z" * z_count) + f"-upstream-{sha}.conf")).write_text(
            _FULL_DROPIN_TEMPLATE.format(sha=sha), encoding="utf-8")
        z_count += 6  # mimics the ever-growing convention across many real deploys
    # Growing at this rate for a few more releases (unpruned) would exceed NAME_MAX
    # (255) and fail to write the file at all -- which is exactly what happened in
    # production after ~30 releases.
    assert z_count > 190

    computed = _compute_dropin_path(dropin_dir, "abcdef0123456789")

    assert len(computed.name) < 60
    remaining = list(dropin_dir.iterdir())
    assert remaining == []  # every prior self-managed release is now provably dead


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
