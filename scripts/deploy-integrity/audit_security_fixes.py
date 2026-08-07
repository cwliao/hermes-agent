#!/usr/bin/env python3
"""Audit registered security-fix fingerprints against the running release.

The normal mode reads the service drop-in, resolves MainPID, and audits the
process cwd.  Explicit --target-dir and --systemd-working-directory overrides
exist for offline/synthetic tests only; they do not change production behavior.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tokenize
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = REPO_ROOT / "docs" / "security-fixes-registry.md"
DEFAULT_DROPIN_DIR = Path.home() / ".config" / "systemd" / "user" / "hermes-gateway.service.d"
DEFAULT_TICKETS_DIR = Path.home() / "project" / "klib" / ".ai" / "tickets"
TICKET_RE = re.compile(r"^T\d+\.md$")
SECURITY_WORDS = re.compile(r"security|secure|安全|資安", re.IGNORECASE)
PRIORITY_RE = re.compile(r"^\s*(?:[-*]\s*)?\*{0,2}Priority\*{0,2}\s*:\s*(P[01])\b", re.IGNORECASE)
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_registry(path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if header is None:
            normalized = [cell.lower() for cell in cells]
            if "ticket" in normalized and "fingerprint" in normalized:
                header = normalized
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if len(cells) != len(header):
            continue
        row = dict(zip(header, cells))
        if not row.get("ticket") or not row.get("fingerprint"):
            continue
        entries.append(
            {
                "ticket": row["ticket"].strip("`").strip(),
                "file": row.get("file", "").strip("`").strip(),
                "fingerprint": row["fingerprint"].strip("`").strip(),
            }
        )
    if not entries:
        raise ValueError(f"registry contains no parseable entries: {path}")
    return entries


def _code_lines(source: str) -> set[int]:
    """Lines containing Python code tokens, excluding comments and strings."""

    lines: set[int] = set()
    for token in tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__):
        if token.type not in {
            tokenize.ENCODING,
            tokenize.ENDMARKER,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.COMMENT,
            tokenize.STRING,
        }:
            lines.add(token.start[0])
    return lines


def _find_fingerprint(target: Path, entry: dict[str, str]) -> tuple[str, str | None]:
    relative_file = entry["file"]
    if not relative_file:
        return "undetermined", None
    if not target.is_dir():
        return "undetermined", None
    candidate = (target / relative_file).resolve()
    try:
        candidate.relative_to(target.resolve())
    except ValueError:
        return "undetermined", None
    if not candidate.is_file():
        return "missing", None
    try:
        source = candidate.read_text(encoding="utf-8")
        code_lines = _code_lines(source)
    except (OSError, UnicodeError, IndentationError, SyntaxError, tokenize.TokenError):
        return "undetermined", None
    fingerprint = entry["fingerprint"]
    for line_number, line in enumerate(source.splitlines(), start=1):
        if line_number in code_lines and line.lstrip().startswith(fingerprint):
            return "present", relative_file
    return "missing", None


def _dropin_working_directory(dropin_dir: Path) -> tuple[Path | None, str | None]:
    if not dropin_dir.is_dir():
        return None, f"systemd drop-in directory does not exist: {dropin_dir}"
    files = sorted(dropin_dir.glob("*.conf"))
    if not files:
        return None, f"no .conf files found in systemd drop-in directory: {dropin_dir}"
    working_directory: str | None = None
    for path in files:
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "WorkingDirectory":
                    working_directory = value.strip()
        except OSError as exc:
            return None, f"unable to read systemd drop-in {path}: {exc}"
    if not working_directory:
        return None, "systemd drop-ins do not define WorkingDirectory"
    if working_directory.startswith("-"):
        working_directory = working_directory[1:]
    return Path(working_directory).expanduser(), None


def _main_pid(unit: str) -> tuple[int | None, str | None]:
    result = subprocess.run(
        ["systemctl", "--user", "show", "-p", "MainPID", unit],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, f"systemctl --user show failed with exit code {result.returncode}"
    match = re.search(r"^MainPID=(\d+)\s*$", result.stdout, re.MULTILINE)
    if not match or match.group(1) == "0":
        return None, "systemd MainPID is unavailable or the service is not running"
    return int(match.group(1)), None


def _process_cwd(pid: int) -> tuple[Path | None, str | None]:
    link = Path("/proc") / str(pid) / "cwd"
    try:
        return Path(link.readlink()), None
    except OSError as exc:
        return None, f"unable to read {link}: {exc}"


def _ticket_sections(text: str) -> str:
    matches = list(HEADING_RE.finditer(text))
    selected: list[str] = []
    for index, match in enumerate(matches):
        heading = match.group(1).strip().lower()
        if heading not in {"objective", "why it matters"}:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        selected.append(text[match.end() : end])
    return "\n".join(selected)


def _unregistered_tickets(tickets_dir: Path, registered: set[str]) -> tuple[list[str], str | None]:
    if not tickets_dir.is_dir():
        return [], f"ticket directory is unavailable; registry-gap scan undetermined: {tickets_dir}"
    unregistered: list[str] = []
    try:
        for path in sorted(tickets_dir.glob("T*.md")):
            if not TICKET_RE.fullmatch(path.name):
                continue
            text = path.read_text(encoding="utf-8")
            priority = any(PRIORITY_RE.search(line) for line in text.splitlines())
            if not priority or not SECURITY_WORDS.search(_ticket_sections(text)):
                continue
            ticket = path.stem.upper()
            if ticket not in registered:
                unregistered.append(ticket)
    except (OSError, UnicodeError) as exc:
        return [], f"ticket registry-gap scan failed: {exc}"
    return unregistered, None


def audit(args: argparse.Namespace) -> int:
    warnings: list[str] = []
    undetermined = False
    registry_path = args.registry.expanduser().resolve()
    try:
        entries = _parse_registry(registry_path)
    except (OSError, UnicodeError, ValueError) as exc:
        entries = []
        undetermined = True
        warnings.append(f"unable to load security registry: {exc}")

    if args.target_dir:
        target = args.target_dir.expanduser().resolve()
        if args.systemd_working_directory:
            systemd_dir = args.systemd_working_directory.expanduser().resolve()
        else:
            systemd_dir = target
            warnings.append("using synthetic --target-dir; systemd discovery was skipped")
    else:
        systemd_dir, error = _dropin_working_directory(args.systemd_dropin_dir.expanduser().resolve())
        if error:
            warnings.append(error)
            undetermined = True
        target = None
        pid: int | None
        if args.pid is not None:
            pid = args.pid
        else:
            pid, error = _main_pid(args.unit)
            if error:
                warnings.append(error)
                undetermined = True
        if pid is not None:
            target, error = _process_cwd(pid)
            if error:
                warnings.append(error)
                undetermined = True

    mismatch = False
    if systemd_dir is not None and target is not None:
        mismatch = systemd_dir.resolve(strict=False) != target.resolve(strict=False)
        if mismatch:
            warnings.append(
                "systemd configuration and actual running process are inconsistent: "
                f"WorkingDirectory={systemd_dir} but /proc/<pid>/cwd={target}"
            )
    else:
        undetermined = True

    target_string = str(target) if target is not None else ""
    results: list[dict[str, object]] = []
    for entry in entries:
        if target is None:
            status, hit_file = "undetermined", None
        else:
            status, hit_file = _find_fingerprint(target, entry)
        if status == "undetermined":
            undetermined = True
        results.append(
            {
                "ticket": entry["ticket"],
                "fingerprint": entry["fingerprint"],
                "status": status,
                "file": hit_file,
            }
        )

    tickets_dir = args.tickets_dir.expanduser().resolve()
    unregistered, error = _unregistered_tickets(
        tickets_dir, {entry["ticket"] for entry in entries}
    )
    if error:
        warnings.append(error)
        undetermined = True
    if unregistered:
        warnings.append(
            "P0/P1 security tickets not registered; manually confirm whether to "
            "add them to docs/security-fixes-registry.md: "
            + ", ".join(unregistered)
        )

    report = {
        "checked_at": _iso_now(),
        "target_dir": target_string,
        "systemd_process_mismatch": mismatch,
        "results": results,
        "unregistered_p0_p1_security_tickets": unregistered,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    for warning in warnings:
        print(f"security-audit: WARNING: {warning}", file=sys.stderr)

    missing = any(result["status"] == "missing" for result in results)
    if missing or mismatch:
        return 2
    if undetermined or any(result["status"] == "undetermined" for result in results):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "AC3: compare registered P0/P1 security-fix definition fingerprints "
            "with the actual /proc/<pid>/cwd selected by systemd. Exit 0 means "
            "all present and no cwd mismatch, 1 means undetermined, and 2 means "
            "missing or mismatch. AC1/AC2 are general deployment checks; AC3 is "
            "security-only and report-only (it never repairs or restarts anything)."
        )
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--systemd-dropin-dir", type=Path, default=DEFAULT_DROPIN_DIR)
    parser.add_argument("--unit", default="hermes-gateway.service")
    parser.add_argument("--pid", type=int, help="override MainPID lookup")
    parser.add_argument(
        "--target-dir",
        type=Path,
        help="offline/synthetic override for the actual process cwd (/proc/<pid>/cwd)",
    )
    parser.add_argument(
        "--systemd-working-directory",
        type=Path,
        help="offline/synthetic override for parsed WorkingDirectory",
    )
    parser.add_argument(
        "--tickets-dir",
        type=Path,
        default=DEFAULT_TICKETS_DIR,
        help=(
            "directory containing T*.md P0/P1 security tickets; default is "
            "$HOME/project/klib/.ai/tickets when present"
        ),
    )
    args = parser.parse_args()
    return audit(args)


if __name__ == "__main__":
    raise SystemExit(main())
