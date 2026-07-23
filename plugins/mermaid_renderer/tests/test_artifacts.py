import argparse
import importlib
import os
from pathlib import Path

import pytest


EXPIRED = "11111111-1111-4111-8111-111111111111.png"
FRESH = "22222222-2222-4222-8222-222222222222.png"


def _artifacts():
    return importlib.import_module("plugins.mermaid_renderer.artifacts")


def _secure_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def _write(root: Path, name: str, mtime: float) -> Path:
    path = root / name
    path.write_bytes(b"test-png")
    os.utime(path, (mtime, mtime))
    return path


def test_status_classifies_expired_fresh_and_ignored_entries(monkeypatch, tmp_path):
    artifacts = _artifacts()
    root = _secure_root(tmp_path)
    now = 1_000_000.0
    _write(root, EXPIRED, now - 86_400)
    _write(root, FRESH, now - 86_399)
    _write(root, "not-renderer.png", now - 100_000)
    (root / "link.png").symlink_to(root / FRESH)
    monkeypatch.setattr(artifacts, "MEDIA_ROOT", root)

    status = artifacts.inspect_artifacts(now=now)

    assert status.eligible_count == 1
    assert status.retained_count == 1
    assert status.ignored_count == 2
    assert status.eligible_bytes == len(b"test-png")


def test_cleanup_is_dry_run_until_apply(monkeypatch, tmp_path):
    artifacts = _artifacts()
    root = _secure_root(tmp_path)
    now = 1_000_000.0
    expired = _write(root, EXPIRED, now - 86_401)
    fresh = _write(root, FRESH, now - 60)
    monkeypatch.setattr(artifacts, "MEDIA_ROOT", root)

    preview = artifacts.cleanup_artifacts(apply=False, now=now)
    assert preview.mode == "dry-run"
    assert preview.eligible_count == 1
    assert preview.deleted_count == 0
    assert expired.exists()

    applied = artifacts.cleanup_artifacts(apply=True, now=now)
    assert applied.mode == "apply"
    assert applied.deleted_count == 1
    assert applied.deleted_bytes == len(b"test-png")
    assert not expired.exists()
    assert fresh.exists()


@pytest.mark.parametrize("kind", ["missing", "insecure", "symlink"])
def test_root_validation_fails_closed(monkeypatch, tmp_path, kind):
    artifacts = _artifacts()
    root = tmp_path / "media"
    if kind == "insecure":
        root.mkdir(mode=0o755)
        os.chmod(root, 0o755)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        os.chmod(target, 0o700)
        root.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(artifacts, "MEDIA_ROOT", root)

    with pytest.raises(artifacts.ArtifactRootError):
        artifacts.inspect_artifacts(now=1_000_000.0)


def test_cleanup_skips_candidate_that_fails_revalidation(monkeypatch, tmp_path):
    artifacts = _artifacts()
    root = _secure_root(tmp_path)
    expired = _write(root, EXPIRED, 1.0)
    monkeypatch.setattr(artifacts, "MEDIA_ROOT", root)
    monkeypatch.setattr(artifacts, "_candidate_is_current", lambda candidate, cutoff: False)

    result = artifacts.cleanup_artifacts(apply=True, now=1_000_000.0)

    assert result.deleted_count == 0
    assert result.skipped_count == 1
    assert expired.exists()


def test_cli_status_and_cleanup_parse_without_root_or_age_overrides(monkeypatch, tmp_path, capsys):
    artifacts = _artifacts()
    cli = importlib.import_module("plugins.mermaid_renderer.cli")
    root = _secure_root(tmp_path)
    monkeypatch.setattr(artifacts, "MEDIA_ROOT", root)
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)

    status_args = parser.parse_args(["status"])
    cleanup_args = parser.parse_args(["cleanup"])
    apply_args = parser.parse_args(["cleanup", "--apply"])

    assert cli.mermaid_renderer_command(status_args) == 0
    assert "retention_hours=24" in capsys.readouterr().out
    assert cli.mermaid_renderer_command(cleanup_args) == 0
    assert "mode=dry-run" in capsys.readouterr().out
    assert cli.mermaid_renderer_command(apply_args) == 0
    assert "mode=apply" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        parser.parse_args(["cleanup", "--older-than-hours", "1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["cleanup", "--root", "/tmp/other"])


def test_cli_reports_bounded_failure_for_unsafe_root(monkeypatch, tmp_path, capsys):
    artifacts = _artifacts()
    cli = importlib.import_module("plugins.mermaid_renderer.cli")
    monkeypatch.setattr(artifacts, "MEDIA_ROOT", tmp_path / "missing")
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)

    assert cli.mermaid_renderer_command(parser.parse_args(["status"])) == 1
    assert capsys.readouterr().out == "status=failed\nerror=media_root_unavailable\n"
