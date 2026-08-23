"""Runtime-boundary checks for mutable Hermes development checkouts.

Ported from the upstream doctor check and kept separate because this checkout
uses the modular doctor_* layout.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from hermes_cli.doctor_report import Finding, _section, check_info, check_ok, check_warn, doctor_check

PROJECT_ROOT = Path(".")
HERMES_HOME = Path.home() / ".hermes"

_SYSTEMD_RUNTIME_PROPERTIES = (
    "ExecStart,ExecStartPre,ExecStartPost,ExecCondition,ExecReload,ExecStop,"
    "ExecStopPost,WorkingDirectory,Environment,EnvironmentFiles,FragmentPath,"
    "DropInPaths,MainPID"
)

_SYSTEMD_ASSIGNMENT_RE = re.compile(
    r"(?:^|[\s{=])([A-Z_][A-Z0-9_]*)=([^\s;}]+)"
)
_SYSTEMD_PATH_RE = re.compile(
    r"(?:^|[=\s{])((?:/|%h/|\$HOME/|\$\{HOME\}/|~/)[^\s{};,\"']+)"
)
_PATH_BOUNDARY_CHARS = frozenset("._-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def _systemd_unit_names(text: str) -> list[str]:
    """Extract service/timer unit names from ``list-unit-files`` output."""
    names = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[0]
        if name.endswith((".service", ".timer")):
            names.append(name)
    return names


def _runtime_reference_roots() -> tuple[Path, ...]:
    """Return mutable Hermes checkout roots which must not back production units."""
    roots: set[Path] = set()
    for candidate in (PROJECT_ROOT, HERMES_HOME / "hermes-agent"):
        roots.add(candidate.absolute())
        try:
            roots.add(candidate.resolve())
        except (OSError, RuntimeError):
            pass
    return tuple(sorted(roots, key=lambda path: (len(str(path)), str(path)), reverse=True))


def _expanded_systemd_text(text: str) -> str:
    """Expand common systemd/home tokens before checking runtime references."""
    substitutions = {
        "HOME": str(Path.home()),
        "HERMES_HOME": str(HERMES_HOME),
    }
    for name, value in _SYSTEMD_ASSIGNMENT_RE.findall(text):
        if name in {"HOME", "HERMES_HOME", "VIRTUAL_ENV", "PYTHONPATH"}:
            substitutions[name] = value
    expanded = text
    for _ in range(3):
        expanded = expanded.replace("%h", substitutions["HOME"])
        for name, value in substitutions.items():
            expanded = expanded.replace(f"${{{name}}}", value)
            expanded = expanded.replace(f"${name}", value)
        expanded = expanded.replace("~/", f"{substitutions['HOME']}/")
        expanded = re.sub(
            r"\\x([0-9a-fA-F]{2})",
            lambda match: chr(int(match.group(1), 16)),
            expanded,
        )
    return expanded


def _systemd_property(text: str, name: str) -> str:
    prefix = f"{name}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        return path == root or root in path.parents
    except (OSError, RuntimeError):
        return False


def _path_boundary_contains(text: str, root: Path) -> bool:
    root_text = str(root)
    start = 0
    while True:
        index = text.find(root_text, start)
        if index < 0:
            return False
        before = text[index - 1] if index else ""
        after_index = index + len(root_text)
        after = text[after_index] if after_index < len(text) else ""
        if before not in _PATH_BOUNDARY_CHARS and after not in _PATH_BOUNDARY_CHARS:
            return True
        start = after_index


def _candidate_paths(text: str) -> list[str]:
    return [token.rstrip(")]") for token in _SYSTEMD_PATH_RE.findall(text)]


def _exec_executable_paths(text: str) -> list[str]:
    """Return only the first executable path from each effective Exec* value."""
    paths: list[str] = []
    for line in text.splitlines():
        property_name, _, value = line.partition("=")
        if not property_name.startswith("Exec"):
            continue
        path_match = re.search(r"(?:^|[\s{])path=([^\s{};,\"']+)", value)
        candidates = [path_match.group(1)] if path_match else _candidate_paths(value)
        if candidates:
            paths.append(candidates[0])
    return paths


def _path_candidate_reference(
    candidate: str,
    roots: tuple[Path, ...],
    *,
    context: str = "",
) -> Path | None:
    expanded = _expanded_systemd_text(candidate)
    try:
        path = Path(expanded).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None
    if not path.is_absolute():
        # systemd's structured ExecStart output commonly reports bare
        # executables such as ``sh`` or ``systemd-tmpfiles``. Resolving those
        # against the doctor's current checkout would create a false positive.
        return None
    for root in roots:
        if _path_is_under(path.absolute(), root) or _path_is_under(
            path.resolve(strict=False), root.resolve(strict=False)
        ):
            return root

    # Follow wrapper symlinks and inspect bounded script content. Never read
    # dotenv files: direct path references are enough and their contents may
    # contain credentials.
    try:
        if (
            path.name == ".env"
            or path.name.startswith(".env.")
            or not path.is_file()
            or path.stat().st_size > 256 * 1024
        ):
            return None
        body = path.read_text(encoding="utf-8", errors="ignore")[:65536]
    except (OSError, UnicodeError):
        return None
    expanded_body = _expanded_systemd_text(body + "\n" + context)
    for root in roots:
        if _path_boundary_contains(expanded_body, root):
            return root
    first_line = body.splitlines()[0] if body.splitlines() else ""
    if first_line.startswith("#!"):
        try:
            interpreter = shlex.split(first_line[2:].strip())
        except ValueError:
            interpreter = first_line[2:].strip().split()
        if interpreter and Path(interpreter[0]).name == "env":
            if "-S" in interpreter:
                interpreter = interpreter[interpreter.index("-S") + 1 :]
            else:
                interpreter = [token for token in interpreter[1:] if not token.startswith("-")]
        for interpreter_token in interpreter[:3]:
            if not interpreter_token.startswith(("/", "%h/", "$HOME/", "${HOME}/", "~/")):
                continue
            try:
                interpreter_path = Path(_expanded_systemd_text(interpreter_token)).expanduser().resolve(strict=False)
                for root in roots:
                    if _path_is_under(interpreter_path, root.resolve(strict=False)):
                        return root
            except (OSError, RuntimeError, ValueError):
                pass
    return None


def _runtime_reference(
    text: str,
    roots: tuple[Path, ...],
    *,
    context: str = "",
    inspect_candidates: bool = False,
    candidate_paths: list[str] | None = None,
) -> Path | None:
    expanded = _expanded_systemd_text(text)
    for root in roots:
        if _path_boundary_contains(expanded, root):
            return root
    if inspect_candidates:
        candidates = candidate_paths if candidate_paths is not None else _candidate_paths(expanded)
        for candidate in candidates:
            match = _path_candidate_reference(candidate, roots, context=context or expanded)
            if match is not None:
                return match
    return None


def _process_runtime_reference(pid: int, roots: tuple[Path, ...]) -> Path | None:
    """Read non-secret identity paths for a live Linux process."""
    proc = Path("/proc") / str(pid)
    values: list[str] = []
    for name in ("cwd", "exe"):
        try:
            value = os.readlink(proc / name)
            match = _path_candidate_reference(value, roots)
            if match is not None:
                return match
            values.append(value)
        except OSError:
            pass
    try:
        values.append((proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode())
    except (OSError, UnicodeError):
        pass
    return _runtime_reference("\n".join(values), roots)


def _check_development_checkout_runtime_references(
    issues: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Report effective systemd service/timer properties pointing at source.

    This is deliberately read-only. It inspects every installed user service and
    timer, including static units that an enabled timer may activate, then asks
    systemd for merged effective properties rather than reading one base unit or
    drop-in file. Unit environment values are never printed; only unit names and
    the matched checkout root are reported.
    """
    # Keep the split module's references aligned with doctor.py so tests and
    # callers can safely override the canonical roots.
    from hermes_cli import doctor as doctor_mod
    global PROJECT_ROOT, HERMES_HOME
    PROJECT_ROOT = doctor_mod.PROJECT_ROOT
    HERMES_HOME = doctor_mod.HERMES_HOME
    if sys.platform == "win32":
        return
    if runner is None:
        def runner(args: list[str], **kwargs):
            return subprocess.run(args, **kwargs)

    scan_deadline = time.monotonic() + 30

    def remaining_timeout() -> float:
        return max(0.1, min(8, scan_deadline - time.monotonic()))

    list_args = [
        "systemctl",
        "--user",
        "list-unit-files",
        "--type=service,timer",
        "--no-legend",
        "--no-pager",
    ]
    systemd_unavailable = False
    systemd_error = ""
    units: list[str] = []
    try:
        listed = runner(
            list_args,
            check=True,
            capture_output=True,
            text=True,
            timeout=remaining_timeout(),
        )
        units = _systemd_unit_names(listed.stdout)
    except FileNotFoundError:
        systemd_unavailable = True
    except (OSError, subprocess.SubprocessError):
        systemd_error = "systemd unit listing failed or timed out"

    roots = _runtime_reference_roots()
    findings: list[tuple[str, Path]] = []
    failed_units: list[str] = []
    for index, unit in enumerate(units):
        remaining = scan_deadline - time.monotonic()
        if remaining <= 0:
            failed_units.extend(units[index:])
            break
        show_args = [
            "systemctl",
            "--user",
            "show",
            unit,
            f"--property={_SYSTEMD_RUNTIME_PROPERTIES}",
            "--no-pager",
        ]
        try:
            shown = runner(
                show_args,
                check=True,
                capture_output=True,
                text=True,
                timeout=min(8, max(0.1, remaining)),
            )
        except (OSError, subprocess.SubprocessError):
            failed_units.append(unit)
            continue
        effective = shown.stdout
        root = _runtime_reference(effective, roots)
        if root is None:
            exec_properties = "\n".join(
                line
                for line in effective.splitlines()
                if line.split("=", 1)[0].startswith("Exec")
            )
            root = _runtime_reference(
                exec_properties,
                roots,
                context=effective,
                inspect_candidates=True,
                candidate_paths=_exec_executable_paths(exec_properties),
            )
        if root is not None:
            findings.append((unit, root))
        main_pid = _systemd_property(effective, "MainPID")
        if main_pid.isdigit() and int(main_pid) > 0:
            root = _process_runtime_reference(int(main_pid), roots)
            if root is not None:
                findings.append((f"{unit} process PID {main_pid}", root))

    cron_error = ""
    if time.monotonic() < scan_deadline:
        try:
            cron = runner(
                ["crontab", "-l"],
                check=False,
                capture_output=True,
                text=True,
                timeout=remaining_timeout(),
            )
            if cron.returncode == 0:
                cron_text = "\n".join(
                    line for line in cron.stdout.splitlines() if not line.lstrip().startswith("#")
                )
                root = _runtime_reference(
                    cron_text,
                    roots,
                )
                if root is not None:
                    findings.append(("user crontab", root))
            # Portable crontab implementations commonly return 1 for both
            # "no crontab" and a localized no-entry diagnostic. Treat an
            # empty stdout with return code 1 as the normal no-crontab case;
            # higher exit codes or unexpected stdout remain incomplete.
            elif cron.returncode != 1 or cron.stdout.strip():
                cron_error = "crontab inspection failed or timed out"
        except FileNotFoundError:
            pass
        except (OSError, subprocess.SubprocessError):
            cron_error = "crontab inspection failed or timed out"
    else:
        cron_error = "runtime boundary scan deadline exceeded before crontab inspection"

    _section("Runtime Boundary")
    incomplete: list[str] = []
    if systemd_error:
        incomplete.append(systemd_error)
    if failed_units:
        incomplete.append(f"systemd show failed for {len(failed_units)} unit(s)")
    if cron_error:
        incomplete.append(f"crontab inspection failed: {cron_error}")
    if systemd_unavailable:
        check_info("Systemd user scope unavailable; scanned crontab only")
    if incomplete:
        check_warn("Runtime boundary scan incomplete", "(" + "; ".join(incomplete) + ")")
        issues.append("Runtime boundary scan incomplete: " + "; ".join(incomplete))
    if not findings:
        if not incomplete:
            check_ok("No systemd/cron runtime references the development checkout")
        return

    check_warn(
        "Runtime definitions reference the development checkout",
        f"({len(findings)} effective reference(s))",
    )
    rendered = ", ".join(f"{unit} → {root}" for unit, root in findings)
    print(f"    → {rendered}")
    issues.append(
        "User systemd/cron runtime definitions still reference the mutable "
        "Hermes development checkout: "
        + rendered
        + ". Migrate release-coupled code or host-owned wrappers before cleanup."
    )




@doctor_check()
def _check_runtime_boundary(should_fix: bool, finding: Finding) -> None:
    _check_development_checkout_runtime_references(finding.issues)
