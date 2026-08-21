"""Render generic ops-timer systemd user units from ``scripts/systemd/*.in``.

Several small, alert-or-repair-only unit pairs (T0213's
``failed-unit-allowlist-repair`` and ``app-deep-health-check``, T0214's
``release-drift-watch``) need no unit-specific parameters beyond the three
placeholders every ``.in`` template in this repo already shares:
``@HERMES_HOME@``, ``@RELEASE_PATH@``, ``@PYTHON@``. Before this script,
each of those three pairs existed on the DGX host only as manually,
out-of-band rendered files with no repo-tracked way to reproduce them on a
redeploy or a rebuilt host -- see
``docs/plans/2026-08-20-session-handover-notify-subs-and-lane-failures.md``.

This script is the generic mechanism that gap called for. It does not
replace ``install_calendar_guard.py`` (owns a release marker check and a
wrapper script) or ``install_kanban_summary.py`` (owns a ``--board``
parameter) -- both have bespoke needs this script doesn't model. A future
unit pair only needs an entry in ``UNIT_GROUPS`` here if it truly needs
nothing beyond the three shared placeholders.

Rendering is separate from enabling: by default this command only writes
unit files for human review. ``--enable`` additionally runs
``systemctl --user daemon-reload`` and ``enable --now`` for the rendered
timer.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# group name -> (service template, timer template)
UNIT_GROUPS: dict[str, tuple[str, str]] = {
    "failed-unit-allowlist-repair": (
        "failed-unit-allowlist-repair.service.in",
        "failed-unit-allowlist-repair.timer.in",
    ),
    "app-deep-health-check": (
        "app-deep-health-check.service.in",
        "app-deep-health-check.timer.in",
    ),
    "release-drift-watch": (
        "release-drift-watch.service.in",
        "release-drift-watch.timer.in",
    ),
}


def render_unit(
    template_name: str,
    *,
    hermes_home: Path,
    release_path: Path,
    python_path: Path,
) -> str:
    text = (ROOT / "systemd" / template_name).read_text(encoding="utf-8")
    values = {
        "@HERMES_HOME@": str(hermes_home.resolve()),
        "@RELEASE_PATH@": str(release_path.resolve()),
        "@PYTHON@": str(python_path.absolute()),
    }
    for token, value in values.items():
        text = text.replace(token, value)
    if "@" in text:
        raise ValueError(f"unresolved unit placeholder in {template_name}")
    return text


def write_group_units(
    group: str,
    hermes_home: Path,
    release_path: Path,
    python_path: Path,
    *,
    unit_dir: Path,
) -> list[Path]:
    if group not in UNIT_GROUPS:
        raise ValueError(
            f"unknown unit group {group!r}; known groups: {sorted(UNIT_GROUPS)}"
        )
    unit_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for template_name in UNIT_GROUPS[group]:
        output = unit_dir / template_name.removesuffix(".in")
        output.write_text(
            render_unit(
                template_name,
                hermes_home=hermes_home,
                release_path=release_path,
                python_path=python_path,
            ),
            encoding="utf-8",
        )
        outputs.append(output)
    return outputs


def _timer_name(group: str) -> str:
    service_template, timer_template = UNIT_GROUPS[group]
    del service_template
    return timer_template.removesuffix(".in")


def enable_group(group: str, *, runner=subprocess.run) -> None:
    clean_env = os.environ.copy()
    clean_env.pop("_HERMES_GATEWAY", None)
    clean_env.pop("HERMES_GATEWAY_SESSION", None)
    runner(["systemctl", "--user", "daemon-reload"], check=True, env=clean_env)
    runner(
        ["systemctl", "--user", "enable", "--now", _timer_name(group)],
        check=True,
        env=clean_env,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render (and optionally enable) a generic ops-timer unit pair"
    )
    parser.add_argument(
        "--group", choices=sorted(UNIT_GROUPS), required=True, help="unit group to render"
    )
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--release-path", type=Path, required=True)
    parser.add_argument("--python", dest="python_path", type=Path, required=True)
    parser.add_argument("--unit-dir", type=Path, default=None)
    parser.add_argument(
        "--enable",
        action="store_true",
        help="also run 'systemctl --user daemon-reload' and 'enable --now' the timer",
    )
    args = parser.parse_args(argv)
    unit_dir = args.unit_dir or (Path.home() / ".config" / "systemd" / "user")
    outputs = write_group_units(
        args.group,
        args.hermes_home,
        args.release_path,
        args.python_path,
        unit_dir=unit_dir,
    )
    for path in outputs:
        print(path)
    if args.enable:
        enable_group(args.group)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
