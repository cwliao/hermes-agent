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
import shutil
import subprocess
import sys
import tempfile
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
    backup = state / "rollback" / args.run_id / "previous-dropin.conf"
    dropin = Path(args.systemd_dropin).expanduser().resolve()
    previous_release = args.previous_release
    built = False
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
        built = True
        if dropin.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dropin, backup)
        dropin.parent.mkdir(parents=True, exist_ok=True)
        content = "[Service]\nWorkingDirectory=%s\nEnvironment=PYTHONPATH=%s\nEnvironment=HERMES_RELEASE_SHA=%s\n" % (destination, destination, candidate["candidate_sha"])
        temporary = dropin.with_name(f".{dropin.name}.{os.getpid()}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, dropin)
        for command in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "restart", args.systemd_unit]):
            completed = _run(command)
            if completed.returncode != 0:
                raise RuntimeError(f"{command[-1]} failed")
        active = _run(["systemctl", "--user", "is-active", "--quiet", args.systemd_unit])
        identity = _run(["systemctl", "--user", "show", args.systemd_unit, "-p", "WorkingDirectory", "-p", "Environment"])
        if active.returncode != 0 or destination.as_posix() not in identity.stdout or str(candidate["candidate_sha"]) not in identity.stdout:
            raise RuntimeError("post-restart health or release identity check failed")
        result.update(status="DONE", candidate_sha=candidate["candidate_sha"], release_path=str(destination), verification=identity.stdout, next_step="保留 rollback artifacts，檢查 systemd effective identity 與 health logs")
        candidate["status"] = "DONE"
        candidate["applied_main_sha"] = source_sha
        candidate["applied_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write(candidate_path, candidate)
        return 0, result
    except Exception as exc:
        result.update(status="FAILED", error_code="APPLY_FAILED", message=str(exc), rollback="attempted")
        if backup.exists():
            shutil.copy2(backup, dropin)
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
    parser.add_argument("--systemd-dropin", default="~/.config/systemd/user/hermes-gateway.service.d/zzzz-upstream-apply.conf")
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
