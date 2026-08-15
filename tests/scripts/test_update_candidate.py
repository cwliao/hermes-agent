from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


def _load_module():
    path = Path(__file__).parents[2] / "scripts" / "update_candidate.py"
    spec = importlib.util.spec_from_file_location("update_candidate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_inspect_refs_reports_identity_and_categories(tmp_path):
    module = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "gateway").mkdir()
    (repo / "gateway" / "run.py").write_text("private\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "private")
    private_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(repo, "branch", "private")
    (repo / "gateway" / "run.py").write_text("upstream\n", encoding="utf-8")
    (repo / "cron.py").write_text("cron\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "upstream")
    _git(repo, "branch", "upstream")

    report = module.inspect_refs(repo, private_sha, "upstream")

    assert report["private_ref"]["sha"] == private_sha
    assert len(report["upstream_ref"]["sha"]) == 40
    assert report["private_only_commits"] == 0
    assert report["upstream_only_commits"] == 1
    assert report["changed_file_count"] == 2
    assert report["changed_path_categories"]["gateway-and-platforms"] == 1
    assert report["changed_path_categories"]["other"] == 1
    assert report["candidate_policy"].startswith("inspect-only")


def test_main_refuses_to_overwrite_report(tmp_path):
    module = _load_module()
    output = tmp_path / "report.json"
    output.write_text("existing\n", encoding="utf-8")

    try:
        module.main(["--repo", str(tmp_path), "--output", str(output)])
    except SystemExit as exc:
        assert "refusing to overwrite" in str(exc)
    else:
        raise AssertionError("existing report was overwritten")
