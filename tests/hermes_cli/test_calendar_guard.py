import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import calendar_guard
from hermes_cli.gateway_identity import (
    GatewayIdentityError,
    active_gateway_identity,
    identity_from_project,
    parse_systemd_properties,
)
from hermes_cli.release_markers import stamp_release_marker
from scripts.release_snapshot import build_snapshot
from scripts.install_calendar_guard import install_user_units


def _runner(stdout: str):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return run


def _props(path: Path, pid: str = "123") -> str:
    return (
        "ActiveState=active\n"
        "SubState=running\n"
        f"MainPID={pid}\n"
        f"WorkingDirectory={path}\n"
    )


def test_release_marker_precedence_and_legacy_names(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "RELEASE_SHA").write_text("abc1234\n")
    assert identity_from_project(release).fingerprint == "release:abc1234"
    (release / ".hermes-release-sha").write_text("abc1234\n")
    assert identity_from_project(release).marker_name == ".hermes-release-sha"


def test_conflicting_markers_fail_closed(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / ".hermes-release-sha").write_text("abc1234\n")
    (release / "RELEASE_SHA").write_text("def5678\n")
    with pytest.raises(GatewayIdentityError, match="conflicting"):
        identity_from_project(release)


def test_unknown_release_marker_fails_closed(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "RELEASE_COMMIT").write_text("abc1234\n")
    (release / "RELEASE_SHA256").write_text("hash\n")
    with pytest.raises(GatewayIdentityError, match="unrecognized"):
        identity_from_project(release)


def test_unrelated_release_notes_do_not_trigger_marker_failure(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / ".hermes-release-sha").write_text("abc1234\n")
    (release / "RELEASE_NOTES.md").write_text("notes\n")

    assert identity_from_project(release).fingerprint == "release:abc1234"


def test_pruned_release_directory_fails_closed_with_guard_warning(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "pruned"
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = calendar_guard.check_once(
        home=home,
        project_root=tmp_path / "checkout",
        now=100,
        runner=_runner(_props(release)),
    )

    assert "release directory unavailable" in result
    assert "recovery queued" in result


def test_systemd_properties_validate_required_fields_and_pid():
    parsed = parse_systemd_properties(
        "ActiveState=active\nSubState=running\nMainPID=9\nWorkingDirectory=/x\n"
    )
    assert parsed["MainPID"] == "9"
    with pytest.raises(GatewayIdentityError, match="MainPID"):
        parse_systemd_properties(
            "ActiveState=active\nSubState=running\nMainPID=nope\nWorkingDirectory=/x\n"
        )


def test_active_identity_uses_user_scope_and_release_marker(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("abc1234\n")
    identity = active_gateway_identity(
        home,
        project_root=tmp_path / "checkout",
        runner=_runner(_props(release)),
    )
    assert identity.fingerprint == "release:abc1234"
    assert identity.service_properties["MainPID"] == "123"


def test_atomic_release_marker_writer(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    marker = stamp_release_marker(release, "ABC1234")
    assert marker.name == ".hermes-release-sha"
    assert marker.read_text() == "ABC1234\n"
    with pytest.raises(ValueError):
        stamp_release_marker(release, "not a sha")


def test_release_snapshot_builder_stamps_canonical_marker(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sentinel.txt").write_text("ok")
    destination = tmp_path / "release"
    build_snapshot(source, destination, "abc1234")
    assert (destination / "sentinel.txt").read_text() == "ok"
    assert (destination / ".hermes-release-sha").read_text() == "abc1234\n"
    with pytest.raises(FileExistsError):
        build_snapshot(source, destination, "abc1234")


def test_installer_renders_user_units_without_gateway_env(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    paths = install_user_units(
        home,
        release,
        Path("/opt/hermes/bin/python"),
        unit_dir=tmp_path / "systemd-user",
        runner=runner,
    )
    assert {path.name for path in paths} == {
        "hermes_calendar_guard.sh",
        "hermes-gateway-recovery.service",
        "hermes-gateway-recovery.timer",
    }
    service = (tmp_path / "systemd-user" / "hermes-gateway-recovery.service").read_text()
    assert "Environment=_HERMES_GATEWAY=" in service
    assert "hermes_cli.calendar_guard --recover" in service
    assert calls[0][0] == ["systemctl", "--user", "daemon-reload"]


def test_check_queues_once_and_suppresses_duplicate(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("abc1234\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        calendar_guard,
        "read_boot_record",
        lambda home=None: {"schema": 1, "fingerprint": "release:old123", "release_path": str(release)},
    )
    runner = _runner(_props(release))
    first = calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=100, runner=runner
    )
    second = calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=101, runner=runner
    )
    assert "recovery queued" in first
    assert second == ""
    request = json.loads((home / "gateway" / "calendar_guard_request.json").read_text())
    assert request["service"] == "hermes-gateway.service"


def test_exhausted_incident_stays_silent_across_hourly_checks(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("abc1234\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        calendar_guard,
        "read_boot_record",
        lambda home=None: {"schema": 1, "fingerprint": "release:old123", "release_path": str(release)},
    )
    runner = _runner(_props(release))
    first = calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=100, runner=runner
    )
    assert "recovery queued" in first
    state_path = home / "gateway" / "calendar_guard_state.json"
    state = json.loads(state_path.read_text())
    state.update({"attempts": calendar_guard.MAX_ATTEMPTS, "recovery_exhausted": True})
    state_path.write_text(json.dumps(state))

    assert calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=3700, runner=runner
    ) == ""
    assert calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=7300, runner=runner
    ) == ""


def test_check_is_silent_when_boot_identity_and_path_match(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("abc1234\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        calendar_guard,
        "read_boot_record",
        lambda home=None: {"schema": 1, "fingerprint": "release:abc1234", "release_path": str(release)},
    )
    assert calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=100, runner=_runner(_props(release))
    ) == ""


def test_recovery_removes_gateway_marker_from_restart_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(calendar_guard.subprocess, "run", fake_run)
    calendar_guard._restart_user_service("hermes-gateway.service")
    assert captured["args"] == ["systemctl", "--user", "restart", "hermes-gateway.service"]
    assert "_HERMES_GATEWAY" not in captured["env"]


def test_recovery_requires_new_pid_and_matching_boot_record(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("abc1234\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "gateway").mkdir()
    (home / "gateway" / "calendar_guard_request.json").write_text(
        '{"schema": 1, "incident_key": "abc", "service": "hermes-gateway.service"}'
    )
    identities = iter(
        [
            active_gateway_identity(home, project_root=tmp_path / "checkout", runner=_runner(_props(release, "1"))),
            active_gateway_identity(home, project_root=tmp_path / "checkout", runner=_runner(_props(release, "2"))),
        ]
    )
    monkeypatch.setattr(calendar_guard, "active_gateway_identity", lambda *args, **kwargs: next(identities))
    monkeypatch.setattr(
        calendar_guard,
        "read_boot_record",
        lambda home=None: {"schema": 1, "fingerprint": "release:abc1234", "release_path": str(release)},
    )
    result = calendar_guard.recover_once(
        home=home,
        project_root=tmp_path / "checkout",
        now=100,
        restart=lambda service: None,
        sleep=lambda seconds: None,
    )
    assert "recovery verified" in result
    assert not (home / "gateway" / "calendar_guard_request.json").exists()
