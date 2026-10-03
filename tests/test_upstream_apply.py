"""Apply gate tests; all tests use dry-run and temporary Git state."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import hermes_upstream_apply as apply_module  # noqa: E402
from hermes_upstream_apply import (  # noqa: E402
    _compute_dropin_path,
    _prune_after_write,
    _provision_release_venv,
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


def invoke(repo: Path, state: Path, *extra: str) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--state-dir", str(state), "--run-id", "run-apply", "--now", "2026-09-05T10:00:00Z", *extra],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
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
        "approval": {"approved_by": "operator"},
    }
    state.joinpath("candidates", "run-apply.json").write_text(json.dumps(value), encoding="utf-8")


def test_unapproved_candidate_is_blocked(tmp_path: Path):
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    candidate(repo, state, approved=False)
    code, result = invoke(repo, state)
    assert code == 1
    assert result["error_code"] == "NOT_APPROVED"


def test_approved_apply_defaults_to_dry_run(tmp_path: Path):
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    candidate(repo, state, approved=True)
    code, result = invoke(repo, state)
    assert code == 0
    assert result["status"] == "APPROVED"
    assert result["dry_run"] is True
    assert not (tmp_path / "releases").exists()


def test_approved_candidate_without_approver_is_blocked(tmp_path: Path):
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    candidate(repo, state, approved=True)
    path = state / "candidates" / "run-apply.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["approval"] = {}
    path.write_text(json.dumps(value), encoding="utf-8")

    code, result = invoke(repo, state)

    assert code == 1
    assert result["error_code"] == "NOT_APPROVED"


def test_compute_dropin_path_sorts_after_every_existing_z_prefixed_file(tmp_path: Path):
    dropin_dir = tmp_path / "drop-ins"
    dropin_dir.mkdir()
    (dropin_dir / ("z" * 180 + "-some-ad-hoc-branch.conf")).write_text("", encoding="utf-8")
    (dropin_dir / ("z" * 60 + "-older-release.conf")).write_text("", encoding="utf-8")
    (dropin_dir / "10-corporate-tls-ca.conf").write_text("", encoding="utf-8")

    before = {path.name: path.read_bytes() for path in dropin_dir.iterdir()}
    computed = _compute_dropin_path(dropin_dir, "530cd6ff91f5edc163e37e07a767c0caf585cb66")

    existing = sorted(p.name for p in dropin_dir.iterdir())
    assert sorted(existing + [computed.name])[-1] == computed.name
    assert {path.name: path.read_bytes() for path in dropin_dir.iterdir()} == before


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


def test_prune_after_write_keeps_new_file_and_removes_superseded_dropins(tmp_path: Path, monkeypatch):
    dropin_dir = tmp_path / "drop-ins"
    dropin_dir.mkdir()
    old = dropin_dir / ("z" * 20 + "-upstream-1111111111.conf")
    old.write_text(_FULL_DROPIN_TEMPLATE.format(sha="1111111111"), encoding="utf-8")
    keep = dropin_dir / ("z" * 40 + "-upstream-2222222222.conf")
    keep.write_text(_FULL_DROPIN_TEMPLATE.format(sha="2222222222"), encoding="utf-8")

    monkeypatch.setattr(apply_module, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", ""))
    final = _prune_after_write(dropin_dir, keep)

    assert final.is_file()
    assert final.name == "z" * 20 + "-upstream-2222222222.conf"
    assert list(dropin_dir.iterdir()) == [final]


def test_prune_after_write_does_not_rename_same_name_through_symlink(tmp_path: Path, monkeypatch):
    real_dir = tmp_path / "real-drop-ins"
    real_dir.mkdir()
    symlink_dir = tmp_path / "drop-ins"
    symlink_dir.symlink_to(real_dir, target_is_directory=True)
    keep = symlink_dir / ("z" * 20 + "-upstream-2222222222.conf")
    keep.write_text(_FULL_DROPIN_TEMPLATE.format(sha="2222222222"), encoding="utf-8")

    monkeypatch.setattr(apply_module, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", ""))
    final = _prune_after_write(symlink_dir, keep)

    assert final.name == keep.name
    assert final.is_file()
    assert keep.is_file()


def test_prune_after_write_raises_when_daemon_reload_fails(tmp_path: Path, monkeypatch):
    dropin_dir = tmp_path / "drop-ins"
    dropin_dir.mkdir()
    keep = dropin_dir / ("z" * 20 + "-upstream-2222222222.conf")
    keep.write_text(_FULL_DROPIN_TEMPLATE.format(sha="2222222222"), encoding="utf-8")

    monkeypatch.setattr(
        apply_module,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 1, "", "reload failed"),
    )
    with pytest.raises(RuntimeError, match="daemon-reload failed: reload failed"):
        _prune_after_write(dropin_dir, keep)


def test_compute_dropin_path_stays_short_after_many_releases(tmp_path: Path, monkeypatch):
    """Repeated compute/write/verify/prune cycles keep the name bounded."""
    dropin_dir = tmp_path / "drop-ins"
    dropin_dir.mkdir()
    hand_added = dropin_dir / ("z" * 7 + "-operator.conf")
    hand_added.write_text("[Service]\nEnvironment=OPERATOR_ONLY=1\n", encoding="utf-8")
    monkeypatch.setattr(apply_module, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", ""))

    for i in range(100):
        sha = f"{i:010x}"
        computed = _compute_dropin_path(dropin_dir, sha)
        assert sorted([path.name for path in dropin_dir.iterdir()] + [computed.name])[-1] == computed.name
        computed.write_text(_FULL_DROPIN_TEMPLATE.format(sha=sha), encoding="utf-8")
        final = _prune_after_write(dropin_dir, computed)
        assert final.is_file()
        assert sorted(path.name for path in dropin_dir.iterdir())[-1] == final.name
        assert len(final.name) <= 60

    assert hand_added.is_file()
    assert len(list(dropin_dir.iterdir())) == 2


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


def test_target_python_version_normalizes_patch_component(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        "environments = [\"python_version >= '3.14.0'\"]\n",
        encoding="utf-8",
    )
    assert _target_python_version(pyproject) == "3.14"


def test_target_python_version_reads_compound_tool_uv_environment(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        "environments = [\"python_version >= '3.14' and sys_platform != 'android'\"]\n",
        encoding="utf-8",
    )
    assert _target_python_version(pyproject) == "3.14"


def test_target_python_version_falls_back_for_empty_environments(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.uv]\nenvironments = []\n", encoding="utf-8")
    assert _target_python_version(pyproject) == f"{sys.version_info.major}.{sys.version_info.minor}"


@pytest.mark.parametrize(
    "value",
    [
        '["python_version >= \'3.14\'", "python_version >= \'3.15\'"]',
        '["python_version >= \'3.14\' or python_version < \'3.12\'"]',
    ],
)
def test_target_python_version_rejects_ambiguous_environments(tmp_path: Path, value: str):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f"[tool.uv]\nenvironments = {value}\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        _target_python_version(pyproject)


def test_target_python_version_falls_back_when_undeclared(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = \"x\"\n", encoding="utf-8")
    version = _target_python_version(pyproject)
    assert version == f"{sys.version_info.major}.{sys.version_info.minor}"


def test_target_python_version_rejects_unparseable_environments(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        "environments = [\"sys_platform != 'android'\"]\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="could not parse .*environments value"):
        _target_python_version(pyproject)


def test_provision_release_venv_rejects_python_version_mismatch(tmp_path: Path, monkeypatch):
    destination = tmp_path / "release"
    destination.mkdir()
    (destination / "pyproject.toml").write_text(
        "[tool.uv]\nenvironments = [\"python_version >= '3.14'\"]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    venv_python = tmp_path / ".hermes" / "venvs" / "gateway-candidate1" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("fake interpreter\n", encoding="utf-8")
    monkeypatch.setattr("hermes_upstream_apply._install_release", lambda *args: [])

    def fake_run(command, **kwargs):
        assert command[0] == str(venv_python)
        return subprocess.CompletedProcess(command, 0, stdout="3.12\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match=r"Python 3\.12.*target is Python 3\.14") as exc_info:
        _provision_release_venv(destination, "candidate1234567890", [])
    assert "if gateway-candidate1 is not the live venv, remove it and retry" in str(exc_info.value)
    assert venv_python.is_file()
