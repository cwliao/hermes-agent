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

from release_snapshot import build_snapshot  # noqa: E402

PREFLIGHT_SCRIPT = SCRIPT_DIR / "hermes_upstream_preflight.py"
REVIEW_SCRIPT = SCRIPT_DIR / "hermes_upstream_review.py"
APPLY_SCRIPT = SCRIPT_DIR / "hermes_upstream_apply.py"
REPORT_SCRIPT = SCRIPT_DIR / "hermes_upstream_report.py"

TEST_MEMORY_CAP_KB = 3 * 1024 * 1024  # 3GB per pytest subprocess -- see _run_scoped_tests: this
# host (55-0940189-03) runs other heavy work concurrently and has previously come under real
# memory pressure from an uncapped test run (see /hermes-update skill + memory: "no unbounded
# heavy jobs on DGX"). Applied per CHUNK, not to the whole scoped set at once -- see below.
TEST_CHUNK_SIZE = 15  # files per pytest subprocess; keeps virtual-address usage bounded
# regardless of how large the scoped set is (a big fork-history delta can touch 100+ files;
# running them all in one process exhausted RLIMIT_AS on 2026-09-26 -- not a real test failure,
# a harness artifact from big C-extension-heavy imports (PIL/aiohttp/etc.) reserving address
# space well beyond what RLIMIT_AS budgeted for a single giant run).
MIN_AVAILABLE_MB_FOR_TESTS = 8 * 1024  # bail out (report, don't guess) if the host is already
# this tight on memory -- matches the same DGX memory-pressure lesson: better to stop and let a
# human retry than to gamble another OOM on a host other things depend on.


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


def _available_memory_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def _preexec_memory_cap() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (TEST_MEMORY_CAP_KB * 1024, TEST_MEMORY_CAP_KB * 1024))


_FAILED_NODEID_RE = re.compile(r"^FAILED (\S+)")


def _failed_nodeids(stdout: str) -> list[str]:
    return [m.group(1) for line in stdout.splitlines() if (m := _FAILED_NODEID_RE.match(line))]


def _chunk_failures_on_baseline(venv_repo: Path, chunk: list[str]) -> set[str]:
    """Re-run the SAME CHUNK COMPOSITION (not just the failing nodeids in isolation) against
    venv_repo's own live `main` checkout, to tell a real regression apart from a failure that
    predates this sync entirely -- same principle as the /hermes-update skill's own manual
    "check if it fails on the original checkout too" step.

    Must replay the full chunk, not a narrower nodeid subset: confirmed 2026-09-26 that some
    of these failures are chunk-composition-dependent cross-test pollution (a test earlier in
    the SAME chunk leaves state that makes a later one in that chunk fail/hang), reproducible
    with this exact file grouping against `main` too and NOT reproducible when the "failing"
    nodeids are re-run alone -- an isolated-nodeid rerun falsely looked like a clean baseline
    and made a chunk-ordering artifact look like a genuine candidate regression.

    Any nodeid this can't cleanly attribute is conservatively treated as NOT pre-existing (i.e.
    still blocks), since a baseline check we can't trust proves nothing.
    """
    venv_python = venv_repo / ".venv" / "bin" / "python3"
    try:
        completed = subprocess.run(
            [str(venv_python), "-m", "pytest", "-p", "no:cacheprovider", "-q", *chunk],
            cwd=str(venv_repo), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, check=False, timeout=1200, preexec_fn=_preexec_memory_cap,
        )
    except subprocess.TimeoutExpired:
        # A hang here (confirmed 2026-09-26: this exact class of chunk-composition hang is
        # host-load-dependent and reproduces against unmodified `main` too) must not crash the
        # whole run uncleanly. Returning an empty set makes every nodeid count as "not proven
        # pre-existing" -> still blocks, per this function's own conservative contract above.
        return set()
    return set(_failed_nodeids(completed.stdout))


def _run_scoped_tests(venv_repo: Path, test_root: Path, test_files: list[str]) -> tuple[bool, str]:
    """Run scoped tests against *test_root* (the candidate's own checkout), using the
    interpreter/dependencies already installed under *venv_repo*'s .venv.

    PYTHONPATH is pointed at test_root so its modules shadow venv_repo's own editable
    install for this run -- otherwise the appended editable-install finder would make
    every import resolve back to venv_repo's checkout (currently `main`, not the
    candidate), silently testing the wrong tree. See feedback_verify_hermes_deploy_past_env_vars
    for the same class of gotcha in the deploy path.
    """
    if not test_files:
        return True, "沒有對應的 scoped test 檔案（改動內容無法對應到既有測試），略過測試步驟。"
    venv_python = venv_repo / ".venv" / "bin" / "python3"
    if not venv_python.is_file():
        return True, "找不到 .venv/bin/python3，略過測試步驟（未執行測試不代表失敗）。"

    available_mb = _available_memory_mb()
    if available_mb is not None and available_mb < MIN_AVAILABLE_MB_FOR_TESTS:
        return False, (
            f"主機目前可用記憶體僅 {available_mb}MB（低於 {MIN_AVAILABLE_MB_FOR_TESTS}MB 安全門檻），"
            "為避免在共用的 DGX 主機上引發 OOM，未執行測試、未套用。請稍後在記憶體充裕時重試。"
        )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(test_root)

    # Run in small chunks, each a fresh subprocess: a fork with a long local commit history can
    # touch 100+ files, and running them all in one pytest process can exhaust RLIMIT_AS just
    # from import-time address-space reservations (PIL/aiohttp/etc.), independent of whether the
    # code is actually broken. Chunking bounds peak usage per-process and releases everything
    # between chunks; fails fast on the first genuinely-failing chunk.
    def _run_chunk(chunk: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(venv_python), "-m", "pytest", "-p", "no:cacheprovider", "-q", *chunk],
            cwd=str(test_root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, check=False, timeout=1200, preexec_fn=_preexec_memory_cap, env=env,
        )

    chunks = [test_files[i : i + TEST_CHUNK_SIZE] for i in range(0, len(test_files), TEST_CHUNK_SIZE)]
    summary_lines = [f"scoped tests: {len(test_files)} file(s) across {len(chunks)} chunk(s), against candidate worktree {test_root}"]
    for index, chunk in enumerate(chunks, start=1):
        completed = _run_chunk(chunk)
        if completed.returncode != 0:
            failed = _failed_nodeids(completed.stdout)
            tail = "\n".join(completed.stdout.strip().splitlines()[-40:])
            if not failed:
                # Non-test failure (crash, collection error, etc.) -- nothing to baseline-check
                # against; treat conservatively as blocking.
                summary_lines.append(f"chunk {index}/{len(chunks)} 失敗（非測試層級錯誤），檔案：{', '.join(chunk)}\n{tail}")
                return False, "\n".join(summary_lines)
            baseline_failing = _chunk_failures_on_baseline(venv_repo, chunk)
            new_failures = set(nid for nid in failed if nid not in baseline_failing)
            if new_failures:
                # Before blocking, retry the SAME chunk against the candidate once: this host
                # is known to run other heavy work concurrently, and at least one gateway test
                # is confirmed genuinely host-load-dependent flaky/hang-prone independent of any
                # candidate content (2026-09-26). A nodeid that fails on attempt 1 but passes on
                # attempt 2 is flaky noise, not a regression -- only a nodeid that fails BOTH
                # times is treated as confirmed. Bounded to exactly one retry, not a loop.
                retry = _run_chunk(chunk)
                retry_failed = set(_failed_nodeids(retry.stdout)) if retry.returncode != 0 else set()
                confirmed_new = new_failures & retry_failed
                if not confirmed_new:
                    summary_lines.append(
                        f"chunk {index}/{len(chunks)}: 第一次失敗（{', '.join(sorted(new_failures))}）但重跑一次後未再出現，"
                        "判定為不穩定（flaky），非本次回歸，繼續。"
                    )
                    continue
                retry_tail = "\n".join(retry.stdout.strip().splitlines()[-40:]) if retry.returncode != 0 else tail
                summary_lines.append(
                    f"chunk {index}/{len(chunks)} 失敗，其中在目前 main 上也一樣失敗（視為既有問題、非本次回歸）："
                    f"{', '.join(sorted(baseline_failing)) or '(無)'}；重跑兩次都失敗、確認為新回歸："
                    f"{', '.join(sorted(confirmed_new))}\n{retry_tail}"
                )
                return False, "\n".join(summary_lines)
            summary_lines.append(
                f"chunk {index}/{len(chunks)}: 有失敗但在目前 main 上同樣失敗，判定為既有問題、非本次回歸，繼續："
                f"{', '.join(failed)}"
            )
            continue
        summary_lines.append(f"chunk {index}/{len(chunks)}: OK")
    return True, "\n".join(summary_lines)


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
    #    skill's step 2.5 -- never a blind full-suite run on this host), run against the
    #    candidate's OWN content -- never against venv_repo's live `main` checkout, which is
    #    a different commit than what's about to be deployed.
    #
    #    The test tree must be a `.git`-free COPY (via build_snapshot, the same helper apply.py
    #    itself uses for the real release), not a raw `git worktree`. Confirmed 2026-09-26: a
    #    git worktree's `.git/worktrees/<name>/` metadata always lives inside venv_repo's own
    #    `.git` (that's structural to how worktrees work -- true regardless of where the
    #    worktree's working directory itself is placed, /tmp included), and several tests
    #    (via run_agent -> hermes_bootstrap -> pm.environments.activate_dependencies) stat a
    #    path under exactly that location at import time. tests/home_io_guard.py then fails
    #    them for touching "the real hermes home" -- a false positive with NOTHING to do with
    #    the candidate's actual content, but indistinguishable from a real regression by the
    #    baseline check (which runs against venv_repo directly, no worktree, so it never trips
    #    this). Verified directly: the exact same tests pass cleanly against a build_snapshot
    #    copy of the identical commit.
    git_worktree = Path(tempfile.gettempdir()) / f"hermes-upstream-auto-update-src-{run_id}"
    test_root = Path(tempfile.gettempdir()) / f"hermes-upstream-auto-update-test-{run_id}"
    add = _git(repo, ["worktree", "add", "--detach", str(git_worktree), candidate_sha], check=False)
    if add.returncode != 0:
        print(f"❌ Hermes upstream 全自動更新：無法建立 candidate 測試用 worktree，未套用。\n{add.stderr}")
        return 0
    try:
        shutil.rmtree(test_root, ignore_errors=True)  # defensive: leftover from an interrupted prior run
        build_snapshot(git_worktree, test_root, candidate_sha)
    except Exception as exc:
        print(f"❌ Hermes upstream 全自動更新：無法建立 candidate 測試快照，未套用。\n{exc}")
        return 0
    finally:
        _git(repo, ["worktree", "remove", "--force", str(git_worktree)], check=False)
    try:
        tests_ok, tests_report = _run_scoped_tests(repo, test_root, _scoped_test_files(repo, upstream_sha, candidate_sha))
    finally:
        shutil.rmtree(test_root, ignore_errors=True)
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
