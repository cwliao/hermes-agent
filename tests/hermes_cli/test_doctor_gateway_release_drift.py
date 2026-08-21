"""Tests for the hermes-CLI-vs-gateway-daemon release drift check in doctor.py.

See docs/plans/2026-08-20-session-handover-notify-subs-and-lane-failures.md
for the incident this check exists to catch earlier: the ``hermes`` CLI
wrapper always resolves against the live editable checkout, while
``hermes-gateway.service`` is typically pinned to a separately built release
snapshot -- the two can silently run different code.
"""

from __future__ import annotations

from pathlib import Path

import hermes_cli.doctor as doctor_mod
import hermes_cli.gateway as gateway_cli


def _stub_running(monkeypatch, *, running: bool, gateway_sha: str = ""):
    monkeypatch.setattr(
        gateway_cli, "_probe_systemd_service_running", lambda: (False, running)
    )
    monkeypatch.setattr(
        gateway_cli,
        "_read_systemd_unit_environment",
        lambda: ({"HERMES_RELEASE_SHA": gateway_sha} if gateway_sha else {}),
    )


def test_skips_when_service_not_running(monkeypatch):
    _stub_running(monkeypatch, running=False)
    issues = []
    doctor_mod._check_gateway_release_drift(issues)
    assert issues == []


def test_skips_when_no_release_sha_pinned(monkeypatch):
    _stub_running(monkeypatch, running=True, gateway_sha="")
    issues = []
    doctor_mod._check_gateway_release_drift(issues)
    assert issues == []


def test_matching_sha_is_ok_and_raises_no_issue(monkeypatch, capsys):
    sha = "09bbe31121b2a70289a5f5383ecc28e624b6ac49"
    _stub_running(monkeypatch, running=True, gateway_sha=sha)
    monkeypatch.setattr(doctor_mod, "_resolve_cli_release_sha", lambda: (sha, False))
    issues = []
    doctor_mod._check_gateway_release_drift(issues)
    assert issues == []
    assert "same release" in capsys.readouterr().out


def test_mismatched_sha_warns_and_records_an_issue(monkeypatch, capsys):
    gateway_sha = "09bbe31121b2a70289a5f5383ecc28e624b6ac49"
    cli_sha = "399b72846a85721a932432de48c3459553fbe350"
    _stub_running(monkeypatch, running=True, gateway_sha=gateway_sha)
    monkeypatch.setattr(doctor_mod, "_resolve_cli_release_sha", lambda: (cli_sha, False))
    issues = []
    doctor_mod._check_gateway_release_drift(issues)
    assert len(issues) == 1
    assert cli_sha[:10] in issues[0]
    assert gateway_sha[:10] in issues[0]
    out = capsys.readouterr().out
    assert "differs" in out


def test_dirty_checkout_skips_comparison_without_an_issue(monkeypatch, capsys):
    gateway_sha = "09bbe31121b2a70289a5f5383ecc28e624b6ac49"
    _stub_running(monkeypatch, running=True, gateway_sha=gateway_sha)
    monkeypatch.setattr(doctor_mod, "_resolve_cli_release_sha", lambda: (gateway_sha, True))
    issues = []
    doctor_mod._check_gateway_release_drift(issues)
    assert issues == []
    assert "uncommitted changes" in capsys.readouterr().out


def test_unresolvable_cli_identity_is_informational_only(monkeypatch, capsys):
    gateway_sha = "09bbe31121b2a70289a5f5383ecc28e624b6ac49"
    _stub_running(monkeypatch, running=True, gateway_sha=gateway_sha)
    monkeypatch.setattr(doctor_mod, "_resolve_cli_release_sha", lambda: (None, False))
    issues = []
    doctor_mod._check_gateway_release_drift(issues)
    assert issues == []
    assert gateway_sha[:10] in capsys.readouterr().out


def test_resolve_cli_release_sha_prefers_release_marker(tmp_path, monkeypatch):
    sha = "abc123def456abc123def456abc123def456abc"
    (tmp_path / ".hermes-release-sha").write_text(sha + "\n", encoding="utf-8")
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path)
    resolved_sha, dirty = doctor_mod._resolve_cli_release_sha()
    assert resolved_sha == sha
    assert dirty is False


def test_resolve_cli_release_sha_falls_back_to_git_head(tmp_path, monkeypatch):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path)

    resolved_sha, dirty = doctor_mod._resolve_cli_release_sha()
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert resolved_sha == head
    assert dirty is False

    (tmp_path / "f.txt").write_text("y", encoding="utf-8")
    resolved_sha, dirty = doctor_mod._resolve_cli_release_sha()
    assert resolved_sha == head
    assert dirty is True


def test_resolve_cli_release_sha_no_marker_no_git_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path)
    resolved_sha, dirty = doctor_mod._resolve_cli_release_sha()
    assert resolved_sha is None
    assert dirty is False
