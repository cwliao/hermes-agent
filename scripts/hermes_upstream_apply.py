#!/usr/bin/env python3
"""Apply an approved upstream candidate through an immutable release snapshot.

Without ``--execute`` this command is a validation/dry-run only. Live service
mutation requires both an approved candidate and an approval token supplied by
the operator through ``HERMES_UPSTREAM_APPROVAL_TOKEN``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from hermes_upstream_preflight import _git, run_preflight  # noqa: E402
from release_snapshot import build_snapshot  # noqa: E402

UV = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")

# Every dependency line in pyproject.toml that uv rejects for the target
# interpreter (e.g. no wheel published yet for a new CPython ABI) is retried
# once from a scratch copy of the release with that exact line removed, so a
# single stale/unpublished pin can't block an otherwise-clean deploy. Matched
# from uv's own "has no wheels with a matching Python ABI tag" error text.
_NO_WHEEL_RE = re.compile(r"because ([a-zA-Z0-9_.-]+)==\S+ has no wheels with a matching Python ABI tag", re.IGNORECASE)


def _target_python_version(pyproject_path: Path) -> str:
    """Read ``[tool.uv] environments`` for the interpreter this release targets.

    Falls back to the interpreter currently running this script if the
    project declares no environments restriction.
    """
    text = pyproject_path.read_text(encoding="utf-8")
    match = re.search(r"environments\s*=\s*\[\s*\"python_version\s*>=\s*'([0-9.]+)'\"", text)
    if match:
        return match.group(1)
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def _install_release(venv_python: Path, release_dir: Path, extras: list[str]) -> list[str]:
    """Editable-install ``release_dir`` into ``venv_python``'s venv.

    Retries, once per failing package, from a scratch copy of the release
    with that package's dependency line stripped out of ``pyproject.toml`` --
    then repoints the editable install at the real (untouched) release
    directory with ``--no-deps`` so the on-disk release stays byte-identical
    to its candidate_sha. Returns the list of packages excluded this way, if
    any, so the caller can surface it instead of silently swallowing it.
    """
    extras_suffix = f"[{','.join(extras)}]" if extras else ""
    excluded: list[str] = []
    install_source = release_dir
    scratch: Path | None = None
    for _attempt in range(8):  # bounded: one real dependency conflict per retry, not an infinite loop
        completed = subprocess.run(
            [UV, "pip", "install", "--python", str(venv_python), "-e", f"{install_source}{extras_suffix}"],
            cwd=install_source, capture_output=True, text=True, timeout=1800,
        )
        if completed.returncode == 0:
            break
        match = _NO_WHEEL_RE.search(completed.stdout + completed.stderr)
        if not match:
            raise RuntimeError(f"uv pip install failed:\n{completed.stdout}\n{completed.stderr}")
        package = match.group(1)
        if package in excluded:
            raise RuntimeError(f"uv pip install still failing on already-excluded package {package!r}:\n{completed.stderr}")
        excluded.append(package)
        if scratch is None:
            scratch = Path(tempfile.mkdtemp(prefix="hermes-apply-noexcl-"))
            shutil.copytree(release_dir, scratch, dirs_exist_ok=True, symlinks=True)
            install_source = scratch
        pyproject = scratch / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        pattern = re.compile(rf'^\s*"{re.escape(package)}==[^"]*",?\s*\n', re.MULTILINE)
        new_text, count = pattern.subn("", text)
        if not count:
            raise RuntimeError(f"could not locate {package!r}'s dependency line in pyproject.toml to exclude it")
        pyproject.write_text(new_text, encoding="utf-8")
    else:
        raise RuntimeError("too many excluded packages; giving up rather than looping indefinitely")
    if excluded:
        # Repoint the editable install at the real, untouched release directory
        # without re-resolving dependencies -- the excluded packages' absence
        # is intentional, not something a plain reinstall should try to fix.
        completed = subprocess.run(
            [UV, "pip", "install", "--python", str(venv_python), "-e", str(release_dir), "--no-deps", "--force-reinstall"],
            cwd=release_dir, capture_output=True, text=True, timeout=300,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"failed to repoint editable install at real release dir:\n{completed.stderr}")
    if scratch is not None:
        shutil.rmtree(scratch, ignore_errors=True)
    return excluded


def _provision_release_venv(destination: Path, candidate_sha: str, extras: list[str]) -> tuple[Path, list[str]]:
    """Build (or reuse) a dedicated venv for this release and install it.

    Reusing a venv already built for this exact candidate_sha is safe (same
    source content); anything else starts fresh so a stale/incompatible venv
    is never silently kept.
    """
    venv_dir = Path.home() / ".hermes" / "venvs" / f"gateway-{candidate_sha[:10]}"
    python_version = _target_python_version(destination / "pyproject.toml")
    if not _venv_python(venv_dir).is_file():
        shutil.rmtree(venv_dir, ignore_errors=True)
        completed = subprocess.run([UV, "venv", str(venv_dir), "--python", python_version], capture_output=True, text=True, timeout=300)
        if completed.returncode != 0:
            raise RuntimeError(f"uv venv failed:\n{completed.stderr}")
    excluded = _install_release(_venv_python(venv_dir), destination, extras)
    return venv_dir, excluded


def _previous_extras(destination: Path, previous_dropin_content: str) -> list[str]:
    """Infer the extras the currently-live release was built with.

    Reads the previous drop-in's ``VIRTUAL_ENV=`` to find the live venv, then
    reverse-engineers which optional-dependency groups it satisfies -- so a
    new release keeps whatever platforms/features the operator already has
    enabled, without the operator having to re-specify them on every deploy.
    """
    match = re.search(r"^Environment=VIRTUAL_ENV=(\S+)\s*$", previous_dropin_content, re.MULTILINE)
    if not match:
        return []
    previous_venv = Path(match.group(1))
    python_exe = _venv_python(previous_venv)
    if not python_exe.is_file():
        return []
    sys.path.insert(0, str(REPO_ROOT))
    from pm.features import installed_extras  # noqa: PLC0415

    return installed_extras(destination, previous_venv, python_exe=python_exe)


def _compute_dropin_path(dropin_dir: Path, candidate_sha: str) -> Path:
    """Pick a drop-in filename guaranteed to win systemd's lexical merge order.

    This repo's convention (see docs/plans/2026-09-25-upstream-rebase-005.md)
    is that each new deploy's filename carries strictly more leading ``z``
    characters than every prior one, so the newest deploy always sorts last
    and wins. Computed fresh from the directory's actual current contents --
    never hardcoded -- so a stale guess can't silently lose to a drop-in
    that was added after the guess was made.
    """
    max_z = 0
    if dropin_dir.is_dir():
        for entry in dropin_dir.iterdir():
            match = re.match(r"^(z+)", entry.name)
            if match:
                max_z = max(max_z, len(match.group(1)))
    return dropin_dir / f"{'z' * (max_z + 20)}-upstream-{candidate_sha[:10]}.conf"


def _render_dropin(previous_content: str, venv_dir: Path, destination: Path, candidate_sha: str) -> str:
    """Build the new drop-in by carrying forward the previous one's lines.

    Only the release-specific keys (ExecStart, ExecStopPost, WorkingDirectory,
    VIRTUAL_ENV, PYTHONPATH, HERMES_RELEASE_SHA, and the venv's bin/ entry at
    the front of PATH) are replaced; everything else (SSL_CERT_FILE, HERMES_HOME,
    proxy/CA env, etc.) is preserved byte-for-byte from whatever is already
    live, since those rarely change and hardcoding them here would silently
    drift from the operator's actual environment.
    """
    venv_python = _venv_python(venv_dir)
    lines = []
    seen_execstart = seen_execstoppost = False
    for line in previous_content.splitlines():
        if line.startswith("ExecStart="):
            if not seen_execstart:
                lines.append("ExecStart=")
                lines.append(f"ExecStart={venv_python} -m hermes_cli.main gateway run")
                seen_execstart = True
            continue
        if line.startswith("ExecStopPost="):
            if not seen_execstoppost:
                lines.append("ExecStopPost=")
                lines.append(f"ExecStopPost=-{venv_python} -m gateway.cgroup_cleanup")
                seen_execstoppost = True
            continue
        if line.startswith("WorkingDirectory="):
            lines.append(f"WorkingDirectory={destination}")
            continue
        if line.startswith("Environment=VIRTUAL_ENV="):
            lines.append(f"Environment=VIRTUAL_ENV={venv_dir}")
            continue
        if line.startswith("Environment=PYTHONPATH="):
            lines.append(f"Environment=PYTHONPATH={destination}")
            continue
        if line.startswith("Environment=HERMES_RELEASE_SHA="):
            lines.append(f"Environment=HERMES_RELEASE_SHA={candidate_sha}")
            continue
        if line.startswith("Environment=PATH="):
            rest = line[len("Environment=PATH="):].split(":", 1)
            tail = rest[1] if len(rest) > 1 else ""
            lines.append(f"Environment=PATH={venv_dir}/bin" + (f":{tail}" if tail else ""))
            continue
        lines.append(line)
    if not seen_execstart:
        lines[0:0] = ["ExecStart=", f"ExecStart={venv_python} -m hermes_cli.main gateway run"]
    if not any(l.startswith("Environment=VIRTUAL_ENV=") for l in lines):
        lines.append(f"Environment=VIRTUAL_ENV={venv_dir}")
    if not any(l.startswith("Environment=PYTHONPATH=") for l in lines):
        lines.append(f"Environment=PYTHONPATH={destination}")
    if not any(l.startswith("Environment=HERMES_RELEASE_SHA=") for l in lines):
        lines.append(f"Environment=HERMES_RELEASE_SHA={candidate_sha}")
    return "\n".join(lines) + "\n"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("candidate metadata must be an object")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check, timeout=240)


def _approval_ok(candidate: dict[str, Any]) -> bool:
    approval = candidate.get("approval") or {}
    approved_by = approval.get("approved_by")
    token_hash = approval.get("approval_token_sha256")
    token = os.environ.get("HERMES_UPSTREAM_APPROVAL_TOKEN", "")
    return bool(approved_by and token_hash and token and hashlib.sha256(token.encode()).hexdigest() == token_hash)


def apply(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    repo = Path(args.repo).expanduser().resolve()
    state = Path(args.state_dir).expanduser().resolve()
    candidate_path = state / "candidates" / f"{args.run_id}.json"
    candidate = _load(candidate_path)
    result: dict[str, Any] = {"phase": "apply", "run_id": args.run_id, "candidate_path": str(candidate_path), "release_id": candidate.get("release_id"), "status": "FAILED", "dry_run": not args.execute}

    if str(candidate.get("status")).upper() != "APPROVED":
        result.update(status="BLOCKED", error_code="NOT_APPROVED", next_step="取得人工 approval 後重試 apply")
        return 1, result
    if not _approval_ok(candidate):
        result.update(status="BLOCKED", error_code="APPROVAL_TOKEN_INVALID", next_step="確認 approved_by 與 HERMES_UPSTREAM_APPROVAL_TOKEN，再重試")
        return 1, result

    preflight_args = argparse.Namespace(
        repo=str(repo), state_dir=str(state), mode="apply", run_id=args.run_id,
        upstream_remote=args.upstream_remote, upstream_ref=args.upstream_ref,
        review_ttl_seconds=args.review_ttl_seconds, now=args.now,
    )
    preflight_code, preflight = run_preflight(preflight_args)
    if preflight_code:
        return 1, {**result, "status": preflight.get("status", "BLOCKED"), "error_code": "APPLY_PREFLIGHT", "preflight_artifact": preflight.get("artifact_path"), "preflight": preflight}

    source_sha = _git(repo, ["rev-parse", "refs/heads/main"])
    if source_sha != candidate.get("source_sha"):
        return 1, {**result, "status": "BLOCKED", "error_code": "STALE_MAIN", "next_step": "重新產生 review candidate"}
    if not args.execute:
        return 0, {**result, "status": "APPROVED", "candidate_sha": candidate.get("candidate_sha"), "next_step": "人工確認後以 --execute 執行 snapshot promotion"}

    release_root = Path(args.release_root).expanduser().resolve()
    destination = release_root / str(candidate["release_id"])
    worktree = state / "apply-worktrees" / args.run_id
    previous_release = args.previous_release
    dropin: Path | None = None  # only set once our new drop-in file actually exists, so rollback knows what to remove
    venv_dir: Path | None = None
    try:
        if not previous_release or not args.previous_dropin:
            raise RuntimeError("execute requires an explicit previous release and rollback drop-in")
        previous_dropin = Path(args.previous_dropin).expanduser().resolve()
        if not previous_dropin.is_file():
            raise RuntimeError(f"rollback drop-in does not exist: {previous_dropin}")
        if destination.exists():
            raise RuntimeError("release destination already exists")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        add = _run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), str(candidate["candidate_sha"])])
        if add.returncode != 0:
            raise RuntimeError("unable to create apply source worktree")
        build_snapshot(worktree, destination, str(candidate["candidate_sha"]))

        previous_content = previous_dropin.read_text(encoding="utf-8")
        extras = _previous_extras(destination, previous_content)
        venv_dir, excluded_packages = _provision_release_venv(destination, str(candidate["candidate_sha"]), extras)
        if excluded_packages:
            result["excluded_packages"] = excluded_packages

        dropin_dir = Path(args.systemd_dropin).expanduser().resolve() if args.systemd_dropin else previous_dropin.parent
        dropin = _compute_dropin_path(dropin_dir, str(candidate["candidate_sha"]))
        content = _render_dropin(previous_content, venv_dir, destination, str(candidate["candidate_sha"]))
        dropin.parent.mkdir(parents=True, exist_ok=True)
        temporary = dropin.with_name(f".{dropin.name}.{os.getpid()}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, dropin)
        for command in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "restart", args.systemd_unit]):
            completed = _run(command)
            if completed.returncode != 0:
                raise RuntimeError(f"{command[-1]} failed")
        active = _run(["systemctl", "--user", "is-active", "--quiet", args.systemd_unit])
        identity = _run(["systemctl", "--user", "show", args.systemd_unit, "-p", "WorkingDirectory", "-p", "Environment", "-p", "ExecStart"])
        identity_ok = (
            active.returncode == 0
            and destination.as_posix() in identity.stdout
            and str(candidate["candidate_sha"]) in identity.stdout
            and str(venv_dir) in identity.stdout  # ExecStart/VIRTUAL_ENV must actually point at the new venv, not just env vars
        )
        if not identity_ok:
            raise RuntimeError("post-restart health or release identity check failed")
        # Give a genuinely bad release a real chance to crash-loop before declaring success --
        # `systemctl is-active` right after `restart` can be true mid-crash-loop for a live unit.
        time.sleep(8)
        settle = _run(["systemctl", "--user", "show", args.systemd_unit, "-p", "ActiveState", "-p", "NRestarts"])
        if "ActiveState=active" not in settle.stdout:
            raise RuntimeError(f"service did not stay active after restart:\n{settle.stdout}")
        result.update(status="DONE", candidate_sha=candidate["candidate_sha"], release_path=str(destination), venv_path=str(venv_dir), dropin_path=str(dropin), verification=identity.stdout, next_step="保留 rollback artifacts，檢查 systemd effective identity 與 health logs")
        candidate["status"] = "DONE"
        candidate["applied_main_sha"] = source_sha
        candidate["applied_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write(candidate_path, candidate)
        return 0, result
    except Exception as exc:
        result.update(status="FAILED", error_code="APPLY_FAILED", message=str(exc), rollback="attempted")
        # Rollback is a pure removal, not a restore: our drop-in is always a brand-new,
        # additively-named file (see _compute_dropin_path) that never overwrote anything,
        # so deleting it lets whatever was already live win systemd's lexical merge again --
        # no backup file to go stale or fail to have been written in the first place.
        if dropin is not None and dropin.exists():
            dropin.unlink()
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "restart", args.systemd_unit])
        candidate["status"] = "FAILED"
        candidate["error_code"] = "APPLY_FAILED"
        _write(candidate_path, candidate)
        return 1, result
    finally:
        _run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)])
        shutil.rmtree(worktree, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--release-root", default="~/.hermes/releases")
    parser.add_argument(
        "--systemd-dropin",
        help=(
            "Directory to write the new drop-in .conf into (filename is "
            "computed automatically to sort last -- see _compute_dropin_path). "
            "Defaults to --previous-dropin's directory."
        ),
    )
    parser.add_argument("--systemd-unit", default="hermes-gateway.service")
    parser.add_argument("--previous-dropin")
    parser.add_argument("--previous-release")
    parser.add_argument("--upstream-remote", default="upstream")
    parser.add_argument("--upstream-ref", default="main")
    parser.add_argument("--review-ttl-seconds", type=int, default=7 * 24 * 60 * 60)
    parser.add_argument("--now")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        code, result = apply(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        code, result = 1, {"phase": "apply", "status": "FAILED", "error_code": "APPLY_INPUT_FAIL", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
