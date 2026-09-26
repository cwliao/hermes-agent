"""Tests for the daily Telegram upstream report renderer."""

from __future__ import annotations

import sys
from pathlib import Path

import scripts.hermes_upstream_report as report


def test_added_symbols_extracts_new_python_functions_and_classes(monkeypatch):
    diff = (
        "diff --git a/hermes_cli/new_feature.py b/hermes_cli/new_feature.py\n"
        "+++ b/hermes_cli/new_feature.py\n"
        "@@ -1,0 +2,8 @@\n"
        "+def first_feature(value):\n"
        "+    return value\n"
        "+\n"
        "+class FeatureRegistry:\n"
        "+    pass\n"
        "+async def second_feature():\n"
        "+    return None\n"
    )
    monkeypatch.setattr(report, "_git", lambda *_args: diff)

    assert report._added_symbols(Path("/repo"), "a" * 40, "b" * 40) == [
        "hermes_cli/new_feature.py: function first_feature",
        "hermes_cli/new_feature.py: class FeatureRegistry",
        "hermes_cli/new_feature.py: function second_feature",
    ]


def test_diff_stats_handles_binary_files(monkeypatch):
    monkeypatch.setattr(
        report,
        "_git",
        lambda *_args: "10\t2\tsrc/new.py\n-\t-\tassets/logo.png\n",
    )

    assert report._diff_stats(Path("/repo"), "a" * 40, "b" * 40) == (
        2,
        10,
        2,
        ["src/new.py", "assets/logo.png"],
    )


def test_render_report_is_a_concise_manual_update_reminder(monkeypatch):
    monkeypatch.setattr(report, "_diff_stats", lambda *_args: (2, 20, 4, ["src/new.py", "README.md"]))

    rendered, fingerprint = report.render_report(
        Path("/repo"),
        {
            "run_id": "20260905-123422",
            "upstream_sha": "a" * 40,
            "source_sha": "b" * 40,
            "candidate_sha": "c" * 40,
            "checks": {"preflight_ok": True, "rebase_ok": True, "tests_ok": True},
        },
        checked_at="2026-09-05 20:00 CST",
    )

    assert "🔔 Hermes upstream 更新提醒" in rendered
    assert "變更規模：2 files，+20/-4" in rendered
    assert "請使用 code workflow 手動檢查與更新" in rendered
    assert "Telegram 只提醒" in rendered
    assert "核准套用" not in rendered
    assert fingerprint is None


def test_render_noop_does_not_request_apply():
    rendered, fingerprint = report.render_report(
        Path("/repo"),
        {"upstream_sha": "a" * 40, "checks": {"noop": True}},
        checked_at="2026-09-05 20:00 CST",
    )

    assert "沒有新更新" in rendered
    assert "核准套用" not in rendered
    assert fingerprint == "noop:" + "a" * 40


def test_render_report_dedupes_unchanged_blocked_state_via_state_file(tmp_path, monkeypatch, capsys):
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(
        '{"upstream_sha": "' + "a" * 40 + '", "status": "BLOCKED", "error_code": "REBASE_CONFLICT", '
        '"local_commit_ids": ["x", "y"], "replayed_local_commit_count": 0}',
        encoding="utf-8",
    )
    state_file = tmp_path / "alert.sha256"
    monkeypatch.setattr(sys, "argv", [
        "hermes_upstream_report.py", "--repo", "/repo", "--candidate", str(candidate_path),
        "--state-file", str(state_file),
    ])

    assert report.main() == 0
    first_output = capsys.readouterr().out
    assert "rebase 卡住" in first_output
    assert state_file.read_text(encoding="utf-8").strip() == report._blocked_fingerprint(
        report._load(candidate_path)
    )

    assert report.main() == 0
    second_output = capsys.readouterr().out
    assert second_output == ""  # unchanged blocked state -- stays silent the second time
