#!/usr/bin/env python3
"""Fully automated upstream sync: preflight -> review -> scoped tests -> apply -> main reset+push.

Runs unattended from Hermes's native cron (no_agent, stdout piped to Telegram).
On any non-clean outcome (rebase conflict, scoped test failure, apply failure,
push failure) it stops short of touching anything live and prints a report for
a human to act on -- it never retries or force-fixes a problem itself.

On the clean path (rebase_ok, no scoped-test failures) it self-generates the
`HERMES_UPSTREAM_APPROVAL_TOKEN`/`approval_token_sha256` pair that
`hermes_upstream_apply.py --execute` requires. This is a deliberate, narrow
exception to the normal human-approval gate, authorized specifically for this
script by the operator (2026-09-26) -- it does not apply anywhere else a human
is asked to approve an upstream candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

PREFLIGHT_SCRIPT = SCRIPT_DIR / "hermes_upstream_preflight.py"
REVIEW_SCRIPT = SCRIPT_DIR / "hermes_upstream_review.py"
APPLY_SCRIPT = SCRIPT_DIR / "hermes_upstream_apply.py"
REPORT_SCRIPT = SCRIPT_DIR / "hermes_upstream_report.py"

TEST_MEMORY_CAP_KB = 4 * 1024 * 1024  # 4GB, matches the /hermes-update skill's post-rebase cap


def _git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=check, timeout=300,
    )


def _run_script(script: Path, args: list[str], *, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False, timeout=1800, env=env,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"status": "FAILED", "error_code": "UNPARSEABLE_OUTPUT", "stdout": completed.stdout, "stderr": completed.stderr}
    return completed.returncode, payload


def _render_report(repo: Path, candidate_path: Path) -> str:
    # No --state-file here deliberately: this cron job's own conflict-report branch
    # should always surface the current state, since it's the one signal an operator
    # acts on to go resolve the stuck rebase by hand (unlike the passive daily guard).
    completed = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT), "--repo", str(repo), "--candidate", str(candidate_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=120,
    )
    return completed.stdout.strip() or "(report script produced no output)"


def _current_release_identity(unit: str) -> tuple[Path, Path]:
    """Read the currently-live release dir and its highest-precedence drop-in from systemd."""
    completed = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "WorkingDirectory", "-p", "DropInPaths"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=30,
    )
    working_directory = None
    dropin_paths: list[str] = []
    for line in completed.stdout.splitlines():
        if line.startswith("WorkingDirectory="):
            working_directory = line[len("WorkingDirectory="):].strip()
        elif line.startswith("DropInPaths="):
            dropin_paths = [p for p in line[len("DropInPaths="):].strip().split() if p]
    if not working_directory:
        raise RuntimeError("could not determine current WorkingDirectory from systemd")
    if not dropin_paths:
        raise RuntimeError("could not determine current DropInPaths from systemd")
    # Drop-ins apply in lexical filename order with later files taking precedence per-key
    # (see _prune_superseded_dropins in hermes_upstream_apply.py) -- the last-sorting path
    # systemd itself reports is the one actually governing the live unit's directives.
    return Path(working_directory), Path(sorted(dropin_paths)[-1])


def _scoped_test_files(repo: Path, upstream_sha: str, candidate_sha: str) -> list[str]:
    """Files touched by the fork's own replayed commits, mapped to test files to run."""
    diff = _git(repo, ["diff", "--name-only", upstream_sha, candidate_sha], check=False)
    if diff.returncode != 0:
        return []
    changed = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    test_files: set[str] = set()
    for path in changed:
        if path.startswith("tests/") and path.endswith(".py"):
            test_files.add(path)
            continue
        stem = Path(path).stem
        if not stem or stem.startswith("test_"):
            continue
        for candidate in repo.glob(f"tests/**/test_{stem}.py"):
            test_files.add(str(candidate.relative_to(repo)))
    return sorted(test_files)


def _run_scoped_tests(repo: Path, test_files: list[str]) -> tuple[bool, str]:
    if not test_files:
        return True, "沒有對應的 scoped test 檔案（改動內容無法對應到既有測試），略過測試步驟。"
    venv_python = repo / ".venv" / "bin" / "python3"
    if not venv_python.is_file():
        return True, "找不到 .venv/bin/python3，略過測試步驟（未執行測試不代表失敗）。"

    def _preexec() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (TEST_MEMORY_CAP_KB * 1024, TEST_MEMORY_CAP_KB * 1024))

    completed = subprocess.run(
        [str(venv_python), "-m", "pytest", "-p", "no:cacheprovider", "-q", *test_files],
        cwd=str(repo), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, check=False, timeout=1200, preexec_fn=_preexec,
    )
    tail = "\n".join(completed.stdout.strip().splitlines()[-40:])
    return completed.returncode == 0, f"scoped tests: {', '.join(test_files)}\n{tail}"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _self_approve(candidate_path: Path) -> str:
    """Mark the candidate APPROVED with a self-generated token; returns the plaintext token."""
    candidate = _load(candidate_path)
    token = secrets.token_hex(32)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    candidate["status"] = "APPROVED"
    candidate["approval"] = {
        "approved_by": "auto-cron:hermes_upstream_auto_update",
        "approved_at_utc": now,
        "approval_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }
    _write(candidate_path, candidate)
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--systemd-unit", default="hermes-gateway.service")
    parser.add_argument("--upstream-remote", default="upstream")
    parser.add_argument("--upstream-ref", default="main")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    state = Path(args.state_dir).expanduser().resolve()

    # 1. Preflight (review mode) -- cheap gate, mirrors hermes_upstream_update_guard.sh.
    code, preflight = _run_script(PREFLIGHT_SCRIPT, [
        "--repo", str(repo), "--state-dir", str(state), "--mode", "review", "--json",
    ])
    if code:
        issues = preflight.get("issues") or []
        lines = [f"- {i.get('code', '?')}: {i.get('message', '')}" for i in issues if isinstance(i, dict)]
        print("⚠️ Hermes upstream 全自動更新：preflight 被 gate 阻擋，未執行任何更新。\n" + ("\n".join(lines) or f"status={preflight.get('status', '?')}"))
        return 0

    # 2. Review -- does its own isolated rebase attempt, never resolves conflicts itself.
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    code, review = _run_script(REVIEW_SCRIPT, [
        "--repo", str(repo), "--state-dir", str(state), "--run-id", run_id,
        "--upstream-remote", args.upstream_remote, "--upstream-ref", args.upstream_ref, "--json",
    ])
    candidate_path = state / "candidates" / f"{run_id}.json"
    if not candidate_path.is_file():
        print(f"❌ Hermes upstream 全自動更新：review 沒有產生 candidate metadata（run_id={run_id}）；未更新。")
        return 0

    candidate = _load(candidate_path)
    checks = candidate.get("checks") or {}
    if not checks.get("rebase_ok"):
        print("⚠️ Hermes upstream 全自動更新：rebase 有衝突或失敗，需要人工介入解衝突（本 job 從不自動解衝突）。\n\n" + _render_report(repo, candidate_path))
        return 0
    if checks.get("noop"):
        print("✅ Hermes upstream 全自動更新：已是最新，沒有 upstream 更新需要套用。")
        return 0

    upstream_sha = candidate["upstream_sha"]
    candidate_sha = candidate["candidate_sha"]

    # 3. Scoped tests on exactly the files the fork's own commits touch (see /hermes-update
    #    skill's step 2.5 -- never a blind full-suite run on this host).
    tests_ok, tests_report = _run_scoped_tests(repo, _scoped_test_files(repo, upstream_sha, candidate_sha))
    if not tests_ok:
        print(
            "⚠️ Hermes upstream 全自動更新：scoped tests 失敗，未套用（candidate 仍為 PENDING，需人工檢查）。\n"
            f"run_id={run_id} candidate_sha={candidate_sha}\n\n{tests_report}"
        )
        return 0

    # 4. Self-approve (explicit, narrowly-scoped operator exception -- see module docstring)
    #    and auto-discover the currently-live release/drop-in for --execute's rollback pair.
    token = _self_approve(candidate_path)
    try:
        previous_release, previous_dropin = _current_release_identity(args.systemd_unit)
    except Exception as exc:
        print(f"❌ Hermes upstream 全自動更新：無法從 systemd 讀出目前 live release/drop-in，未套用。\n{exc}")
        return 0

    env = os.environ.copy()
    env["HERMES_UPSTREAM_APPROVAL_TOKEN"] = token
    code, apply_result = _run_script(APPLY_SCRIPT, [
        "--repo", str(repo), "--state-dir", str(state), "--run-id", run_id,
        "--previous-release", str(previous_release), "--previous-dropin", str(previous_dropin),
        "--systemd-unit", args.systemd_unit, "--execute",
    ], env=env)

    if apply_result.get("status") != "DONE":
        print(
            "❌ Hermes upstream 全自動更新：apply 失敗（已自動 rollback，main 未變動，未 push）。\n"
            f"run_id={run_id} candidate_sha={candidate_sha}\n"
            f"error_code={apply_result.get('error_code')} message={apply_result.get('message')}"
        )
        return 0

    # 5. Close the branch-ref gap ourselves (apply.py deliberately never touches `main` --
    #    see docs/plans/2026-09-26-upstream-rebase-006.md). Guarded by the same source_sha
    #    check apply.py already performed, re-verified here against a possible race.
    status = _git(repo, ["status", "--porcelain"], check=False).stdout.strip()
    if status:
        print(
            "⚠️ Hermes upstream 全自動更新：deploy 成功，但 repo 工作目錄不乾淨，為安全起見未自動 reset/push main。\n"
            f"candidate_sha={candidate_sha} release={apply_result.get('release_path')}\n請人工檢查後手動執行："
            f"\ngit -C {repo} reset --hard {candidate_sha} && git -C {repo} push origin main --force-with-lease"
        )
        return 0
    current_main = _git(repo, ["rev-parse", "refs/heads/main"]).stdout.strip()
    if current_main != candidate["source_sha"]:
        print(
            "⚠️ Hermes upstream 全自動更新：deploy 成功，但 main 在套用期間被別的操作變動了，為安全起見未自動 reset/push。\n"
            f"candidate_sha={candidate_sha} expected_source_sha={candidate['source_sha']} actual_main={current_main}\n請人工檢查後手動處理。"
        )
        return 0

    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    _git(repo, ["tag", f"backup/pre-auto-update-{date_tag}", "main"])
    _git(repo, ["reset", "--hard", candidate_sha])
    push = _git(repo, ["push", "origin", "main", "--force-with-lease"], check=False)
    if push.returncode != 0:
        print(
            "⚠️ Hermes upstream 全自動更新：deploy 成功且 main 已在本機 reset，但 push 失敗，需人工手動 push。\n"
            f"candidate_sha={candidate_sha}\n{push.stderr.strip()}\n"
            f"手動指令：git -C {repo} push origin main --force-with-lease"
        )
        return 0

    excluded = apply_result.get("excluded_packages") or []
    excluded_note = f"\n排除套件（無對應 wheel）：{', '.join(excluded)}" if excluded else ""
    print(
        "✅ Hermes upstream 全自動更新成功。\n"
        f"run_id={run_id}\ncandidate_sha={candidate_sha}\nrelease={apply_result.get('release_path')}\n"
        f"venv={apply_result.get('venv_path')}\ndrop-in={apply_result.get('dropin_path')}\n"
        f"replayed local commits: {candidate.get('replayed_local_commit_count')}"
        f"{excluded_note}\nmain 已 reset + push 至 {candidate_sha}。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
