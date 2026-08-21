from __future__ import annotations

from pathlib import Path

import pytest

from scripts.install_ops_timers import (
    UNIT_GROUPS,
    enable_group,
    render_unit,
    write_group_units,
)


@pytest.mark.parametrize("group", sorted(UNIT_GROUPS))
def test_each_group_renders_with_no_unresolved_placeholders(group, tmp_path: Path):
    outputs = write_group_units(
        group,
        Path("/home/cwliao/.hermes"),
        Path("/home/cwliao/.hermes/hermes-agent"),
        Path("/home/cwliao/.hermes/hermes-agent/venv/bin/python"),
        unit_dir=tmp_path,
    )
    service_template, timer_template = UNIT_GROUPS[group]
    assert {path.name for path in outputs} == {
        service_template.removesuffix(".in"),
        timer_template.removesuffix(".in"),
    }
    for path in outputs:
        rendered = path.read_text(encoding="utf-8")
        assert "@" not in rendered


def test_unknown_group_fails_closed(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown unit group"):
        write_group_units(
            "not-a-real-group",
            Path("/home/cwliao/.hermes"),
            Path("/home/cwliao/.hermes/hermes-agent"),
            Path("/home/cwliao/.hermes/hermes-agent/venv/bin/python"),
            unit_dir=tmp_path,
        )


def test_render_unit_raises_on_leftover_unrecognized_placeholder(tmp_path, monkeypatch):
    stray = tmp_path / "systemd"
    stray.mkdir()
    (stray / "stray.service.in").write_text("@HERMES_HOME@ @NOT_A_REAL_TOKEN@\n", encoding="utf-8")
    monkeypatch.setattr("scripts.install_ops_timers.ROOT", tmp_path)
    with pytest.raises(ValueError, match="unresolved unit placeholder"):
        render_unit(
            "stray.service.in",
            hermes_home=Path("/home/cwliao/.hermes"),
            release_path=Path("/home/cwliao/.hermes/hermes-agent"),
            python_path=Path("/home/cwliao/.hermes/hermes-agent/venv/bin/python"),
        )


def test_enable_group_reloads_then_enables_the_right_timer():
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))

        class _Result:
            returncode = 0

        return _Result()

    enable_group("app-deep-health-check", runner=runner)
    assert calls[0][0] == ["systemctl", "--user", "daemon-reload"]
    assert calls[1][0] == ["systemctl", "--user", "enable", "--now", "app-deep-health-check.timer"]
    assert calls[0][1]["check"] is True
    assert "_HERMES_GATEWAY" not in calls[0][1]["env"]
