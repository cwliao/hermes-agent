"""Regression tests for doctor detection of systemd development-checkout references."""

from __future__ import annotations

import subprocess
from pathlib import Path

import hermes_cli.doctor as doctor_mod


def _runner_for_units(unit_output: str, properties: dict[str, str]):
    def runner(args, **kwargs):
        if args[0:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no crontab for user")
        if args[2] == "list-unit-files":
            return subprocess.CompletedProcess(args, 0, stdout=unit_output, stderr="")
        if args[0:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no crontab for user")
        if args[2] == "show":
            unit = args[3]
            return subprocess.CompletedProcess(
                args, 0, stdout=properties.get(unit, ""), stderr=""
            )
        raise AssertionError(f"unexpected systemctl command: {args}")

    return runner


def test_reports_effective_service_reference_to_development_checkout(tmp_path, monkeypatch, capsys):
    hermes_home = tmp_path / ".hermes"
    project_root = hermes_home / "hermes-agent"
    project_root.mkdir(parents=True)
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project_root)
    units = """
hermes-mcp-health-guard.service enabled
hermes-clean.timer enabled
    """
    properties = {
        "hermes-mcp-health-guard.service": (
            f"WorkingDirectory={project_root}\n"
            f"ExecStart={{ path={project_root}/scripts/mcp_health_check.sh ; }}\n"
            f"ExecStopPost={{ path={project_root}/scripts/cleanup.sh ; }}\n"
            f"EnvironmentFiles=-{project_root}/.env\n"
        ),
        "hermes-clean.timer": "Unit=hermes-clean.service\n",
    }
    issues: list[str] = []

    doctor_mod._check_development_checkout_runtime_references(
        issues,
        runner=_runner_for_units(units, properties),
    )

    assert len(issues) == 1
    assert "hermes-mcp-health-guard.service" in issues[0]
    assert str(project_root) in issues[0]
    assert "hermes-clean.timer" not in issues[0]
    assert "Runtime definitions reference the development checkout" in capsys.readouterr().out


def test_expands_systemd_home_token_and_checks_static_units(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    project_root = hermes_home / "hermes-agent"
    project_root.mkdir(parents=True)
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    units = "hermes-summary.service static\n"
    properties = {
        "hermes-summary.service": "ExecStart=%h/.hermes/hermes-agent/scripts/summary.sh\n"
    }
    issues: list[str] = []

    doctor_mod._check_development_checkout_runtime_references(
        issues,
        runner=_runner_for_units(units, properties),
    )

    assert len(issues) == 1
    assert "hermes-summary.service" in issues[0]


def test_systemd_unavailable_is_non_blocking(monkeypatch):
    def unavailable(args, **kwargs):
        raise FileNotFoundError("systemctl")

    issues: list[str] = []
    doctor_mod._check_development_checkout_runtime_references(
        issues,
        runner=unavailable,
    )

    assert issues == []


def test_systemd_unavailable_still_scans_crontab(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    project_root = hermes_home / "hermes-agent"
    project_root.mkdir(parents=True)
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project_root)

    def runner(args, **kwargs):
        if args[0] == "systemctl":
            raise FileNotFoundError("systemctl")
        if args[0:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"* * * * * {project_root}/scripts/job.sh\n",
                stderr="",
            )
        raise AssertionError(args)

    issues: list[str] = []
    doctor_mod._check_development_checkout_runtime_references(issues, runner=runner)

    assert len(issues) == 1
    assert "user crontab" in issues[0]


def test_crontab_reference_is_reported(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    project_root = hermes_home / "hermes-agent"
    project_root.mkdir(parents=True)
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project_root)
    base_runner = _runner_for_units("hermes-clean.service static\n", {"hermes-clean.service": "MainPID=0\n"})

    def runner(args, **kwargs):
        if args[0:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"17 * * * * {project_root}/scripts/summary.sh\n",
                stderr="",
            )
        return base_runner(args, **kwargs)

    issues: list[str] = []
    doctor_mod._check_development_checkout_runtime_references(issues, runner=runner)

    assert len(issues) == 1
    assert "user crontab" in issues[0]


def test_partial_systemd_failure_does_not_report_green(monkeypatch, capsys):
    def runner(args, **kwargs):
        if args[0:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no crontab for user")
        if args[2] == "list-unit-files":
            return subprocess.CompletedProcess(args, 0, stdout="broken.service static\n", stderr="")
        raise subprocess.CalledProcessError(1, args)

    issues: list[str] = []
    doctor_mod._check_development_checkout_runtime_references(issues, runner=runner)

    assert len(issues) == 1
    assert "scan incomplete" in issues[0]
    assert "No systemd/cron runtime references" not in capsys.readouterr().out


def test_path_component_boundary_avoids_lookalike_false_positive(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    project_root = hermes_home / "hermes-agent"
    project_root.mkdir(parents=True)
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project_root)
    units = "hermes-backup.service static\n"
    properties = {
        "hermes-backup.service": f"ExecStart={project_root}-backup/script.sh\nMainPID=0\n"
    }
    issues: list[str] = []

    doctor_mod._check_development_checkout_runtime_references(
        issues,
        runner=_runner_for_units(units, properties),
    )

    assert issues == []


def test_bare_systemd_executable_is_not_resolved_against_checkout(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    project_root = hermes_home / "hermes-agent"
    project_root.mkdir(parents=True)
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project_root)
    properties = {"hermes-shell.service": "ExecStart={ path=sh ; argv[]=sh -c true ; }\nMainPID=0\n"}
    issues: list[str] = []

    doctor_mod._check_development_checkout_runtime_references(
        issues,
        runner=_runner_for_units("hermes-shell.service static\n", properties),
    )

    assert issues == []


def test_symlinked_wrapper_into_checkout_is_reported(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    project_root = hermes_home / "hermes-agent"
    project_root.mkdir(parents=True)
    wrapper = tmp_path / "stable-wrapper"
    wrapper.symlink_to(project_root / "scripts")
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project_root)
    units = "hermes-wrapper.service static\n"
    properties = {
        "hermes-wrapper.service": f"ExecStart={wrapper}\nMainPID=0\n"
    }
    issues: list[str] = []

    doctor_mod._check_development_checkout_runtime_references(
        issues,
        runner=_runner_for_units(units, properties),
    )

    assert len(issues) == 1


def test_env_s_shebang_into_checkout_is_reported(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    project_root = hermes_home / "hermes-agent"
    project_root.mkdir(parents=True)
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        f"#!/usr/bin/env -S {project_root}/venv/bin/python\nprint('ok')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project_root)
    units = "hermes-wrapper.service static\n"
    properties = {
        "hermes-wrapper.service": f"ExecStart={wrapper}\nMainPID=0\n"
    }
    issues: list[str] = []

    doctor_mod._check_development_checkout_runtime_references(
        issues,
        runner=_runner_for_units(units, properties),
    )

    assert len(issues) == 1
