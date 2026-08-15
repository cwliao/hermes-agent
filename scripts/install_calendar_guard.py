"""Install the repo-owned calendar guard wrapper and recovery timer.

This script is intentionally separate from the gateway process. It is an
operator/deployer action and is not run by the hourly guard itself.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _render(template_name: str, *, hermes_home: Path, release_path: Path, python_path: Path) -> str:
    text = (ROOT / "systemd" / template_name).read_text(encoding="utf-8")
    return (
        text.replace("@HERMES_HOME@", str(hermes_home))
        .replace("@RELEASE_PATH@", str(release_path))
        .replace("@PYTHON@", str(python_path))
    )


def install_user_units(
    hermes_home: Path,
    release_path: Path,
    python_path: Path,
    *,
    unit_dir: Path | None = None,
    runner=subprocess.run,
) -> list[Path]:
    hermes_home = hermes_home.resolve()
    release_path = release_path.resolve()
    python_path = python_path.resolve()
    unit_dir = unit_dir or (Path.home() / ".config" / "systemd" / "user")
    unit_dir.mkdir(parents=True, exist_ok=True)
    script_dir = hermes_home / "scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    wrapper = script_dir / "hermes_calendar_guard.sh"
    shutil.copyfile(ROOT / "hermes_calendar_guard.sh", wrapper)
    try:
        wrapper.chmod(wrapper.stat().st_mode | 0o111)
    except OSError:
        pass

    service = unit_dir / "hermes-gateway-recovery.service"
    timer = unit_dir / "hermes-gateway-recovery.timer"
    service.write_text(
        _render(
            "hermes-gateway-recovery.service.in",
            hermes_home=hermes_home,
            release_path=release_path,
            python_path=python_path,
        ),
        encoding="utf-8",
    )
    timer.write_text(
        _render(
            "hermes-gateway-recovery.timer.in",
            hermes_home=hermes_home,
            release_path=release_path,
            python_path=python_path,
        ),
        encoding="utf-8",
    )
    clean_env = os.environ.copy()
    clean_env.pop("_HERMES_GATEWAY", None)
    runner(["systemctl", "--user", "daemon-reload"], check=True, env=clean_env)
    runner(
        ["systemctl", "--user", "enable", "--now", timer.name],
        check=True,
        env=clean_env,
    )
    return [wrapper, service, timer]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install Hermes calendar guard recovery")
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--release-path", type=Path, required=True)
    parser.add_argument("--python", dest="python_path", type=Path, required=True)
    args = parser.parse_args(argv)
    for path in install_user_units(args.hermes_home, args.release_path, args.python_path):
        print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
