# Fully automated upstream sync pipeline (2026-09-26)

Status: **Deployed and validated live.** First fully unattended real deploy
completed and independently verified 2026-09-26 14:37 CST; `main@9d8e08f4e1`
is running in production.
Priority: High (replaces a review-only daily cron with an end-to-end
automated preflight → review → scoped-test → apply → restart → push
pipeline; found and fixed 9 real bugs in the new pipeline plus 1 genuine
pre-existing flaky test bug along the way)
Repository: `~/.hermes/hermes-agent`
Related: `docs/plans/2026-09-26-upstream-rebase-006.md` (the branch-ref gap
and apply.py fixes this new pipeline builds on and closes automatically),
`docs/plans/2026-09-18-upstream-auto-update-recurring-conflicts-root-cause-001.md`

## Why this round happened

The daily cron job `e1efc7e8cbbe` ("Hermes upstream update guard") had run
`hermes_upstream_update_guard.sh` since the pipeline's inception, but that
script only ever runs preflight + review and reports the result to
Telegram — every real update still required an operator to manually run
`hermes_upstream_apply.py --execute` with a hand-supplied approval token,
and manually `git reset --hard <candidate_sha>` + `push --force-with-lease`
afterward (the branch-ref gap `apply.py` itself never closes — see
rebase-006). The operator asked for this to become a genuinely unattended
cron: on a clean candidate, apply automatically and report success; on any
problem, stop and report via Telegram instead of guessing.

## What was built

Two new files, both symlinked from `~/.hermes/scripts/` back to this repo's
`scripts/` directory (see "Deployment drift cleanup" below):

- `scripts/hermes_upstream_auto_update.py` — the actual driver.
- `scripts/hermes_upstream_auto_update.sh` — thin lock/marker cron wrapper,
  same shape as the old `hermes_upstream_update_guard.sh`.

Flow: preflight (review mode) → review (isolated rebase attempt, never
resolves conflicts itself) → if `rebase_ok` and not `noop`: build a
`.git`-free snapshot of the candidate via `release_snapshot.build_snapshot`
(the same helper `apply.py` uses for real releases) → run scoped pytest
(only files this fork's own replayed local commits touch) in small chunks,
each baseline-compared against `main` and retried once before a failure is
confirmed a real regression → if clean, self-generate the
`HERMES_UPSTREAM_APPROVAL_TOKEN`/`approval_token_sha256` pair (a narrow,
explicit, operator-authorized exception to the normal human-approval gate,
scoped to this script only) → `hermes_upstream_apply.py --execute` with
`--previous-release`/`--previous-dropin` auto-discovered from the live
systemd unit → on success, `git reset --hard <candidate_sha>` +
`push --force-with-lease` on `main`, closing the branch-ref gap `apply.py`
itself never closes. Any non-clean outcome at any stage stops short of
touching anything live and prints a report for Telegram instead.

`hermes cron edit e1efc7e8cbbe --script hermes_upstream_auto_update.sh`
repointed the existing job in place — same schedule (`30 4 * * *`), same
Telegram target, same job id.

## Bugs found and fixed while building this (9, all in the new pipeline)

Each was caught by a real supervised test run against this fork's actual
442–453-commit local-history delta, not synthetic tests — see
`tests/test_upstream_apply.py`/`test_upstream_report.py` for the unit tests
that also came out of this, and the local-memory reference
`reference_hermes_upstream_pipeline_scoped_test_lessons_2026_09_26` for the
full generalizable lessons.

1. Scoped tests initially ran against `venv_repo`'s live `main` checkout,
   not the candidate's own content — silently validating the wrong commit.
2. A 100+ file scoped set in one pytest process risked real memory growth
   with no per-process bound — fixed by chunking (`TEST_CHUNK_SIZE`).
3. The scoped-test worktree lived under `~/.hermes/hermes-upstream-state/`
   — but `tests/home_io_guard.py` fails any test that touches a path under
   the real `HERMES_HOME` tree, and flagged the worktree purely for being
   located there. Moved to `/tmp`.
4. No baseline comparison existed at all — any pre-existing test failure
   would have blocked every future sync forever. Added a baseline check
   against `main`.
5. **The deepest one**: a `git worktree`'s `.git/worktrees/<name>/`
   metadata always lives inside the *original* repo's own `.git` dir —
   true regardless of where the worktree's working directory itself is
   placed. Several tests (via `run_agent → hermes_bootstrap →
   pm.environments.activate_dependencies`) stat a path under exactly that
   location at import time, so `home_io_guard` failed them for touching
   "the real hermes home" with nothing to do with the candidate's actual
   content — this initially looked exactly like a genuine new regression
   (19 `tests/cron/test_scheduler.py` failures) and a fix was almost
   dispatched to codex/agy for a bug that didn't exist, before a clean
   `build_snapshot`-based reproduction proved it false. Fixed by testing
   against a `.git`-free snapshot copy instead of the raw worktree.
6. `REPO_ROOT` was never added to `sys.path`, so `release_snapshot`'s
   `hermes_cli` import failed under the cron wrapper's bare `python3`
   (caught in a supervised run before it could reach apply/deploy).
7. The baseline check re-ran only the isolated *failing nodeids*, not the
   full chunk composition — missed chunk-composition-dependent cross-test
   pollution that reproduces identically against `main` too. Fixed by
   replaying the full chunk on both sides.
8. The baseline check didn't set `PYTHONPATH` (the candidate run always
   does, for import-shadowing) — any test merely sensitive to `PYTHONPATH`
   *being set at all* (e.g. `test_calendar_guard.py`, which echoes env vars
   into rendered output) looked like a permanent unfixable regression on
   every future run. Fixed by mirroring the env override symmetrically.
9. `RLIMIT_AS` turned out to be the wrong memory-capping tool entirely — it
   caps virtual address space, not real usage, and C extensions
   (aiohttp/PIL) reserve large virtual ranges independent of actual RSS.
   Three separate cap/chunk-size tuning attempts hit the identical fatal
   abort; removed the cap, kept the real `/proc/meminfo` `MemAvailable`
   pre-check as the actual safety gate. Separately, a subprocess `timeout=`
   with no exception handling meant a genuine hang burned the whole 1700s
   budget before crashing uncleanly with no report — cut the per-chunk
   timeout to 240s and wrapped it properly.

## The one real pre-existing bug found (not invented)

`tests/gateway/test_kanban_health_window.py::test_dispatcher_health_telemetry_does_not_raise_nameerror`
mocked `fn.__name__ == "_ready_nonempty"` (a private closure defined
earlier in the same file), but the real call site
(`gateway/kanban_watchers.py:731`) invokes `dispatcher.ready_nonempty` — a
public bound method, no underscore, a completely different symbol. The
mock's exit-signal condition never fired, so the watcher loop under test
spun indefinitely; `asyncio.wait_for`'s outer 3s timeout only reliably
caught it when the host wasn't under concurrent load, making it look
load-dependent rather than deterministic (this host, `55-0940189-03`, runs
other heavy jobs concurrently). Dispatched to `agy` (no progress, exhausted
its turn budget) then `codex` (found the real root cause, left it
uncommitted); verified the diagnosis directly against the actual call site
before trusting it, ran the fix 5/5 clean, committed
(`8e1addb587`, later replayed as part of the day's sync — see below), and
merged to `main` via fast-forward before the final validation run.

## First real automated deploy (validated, not just self-reported)

- run_id `20260926-061702`, candidate_sha `9d8e08f4e1e5...` (453 replayed
  local commits, 46 files changed vs. the 006 baseline's tip)
- New venv `gateway-9d8e08f4e1` (Python 3.14, `paddlepaddle` excluded —
  same known gap as every prior round, no `cp314` wheel)
- New release `hermes-upstream-20260926-061702`, new systemd drop-in
- Gateway restarted, Telegram reconnected, `main` reset + pushed
- **Independently verified** (not just trusting the script's own report):
  `/proc/<MainPID>/cwd` and `/exe` match the new release/venv exactly,
  `git -C ~/.hermes/hermes-agent rev-parse HEAD` matches candidate_sha
  exactly, `journalctl` shows a clean startup with no traceback or
  crash-loop.

## Deployment drift cleanup (found and fixed along the way)

`~/.hermes/scripts/` (the directory Hermes cron actually resolves scripts
against — not this repo's own `scripts/`) is not a git repo, and had
silently drifted from this checkout in both directions:

- `hermes_upstream_report.py`/`hermes_upstream_update_guard.sh` there
  carried a real, undocumented feature (Telegram alert de-duplication via a
  fingerprint state file) that had never been committed here — ported back
  into git with test coverage.
- `hermes_upstream_apply.py`/`hermes_upstream_preflight.py` there were
  stale, missing a committed orphan-ref bugfix and every one of
  rebase-006's `apply.py` fixes.

All seven live pipeline files under `~/.hermes/scripts/` (`apply.py`,
`preflight.py`, `review.py`, `report.py`, `update_guard.sh`,
`auto_update.py`, `auto_update.sh`) are now symlinks back to this repo, so
this class of drift cannot recur. Two further files found there
(`hermes_upstream_apply.sh`, `hermes_upstream_common.sh`) were confirmed
dead — never tracked in this git repo at all, not referenced by
`~/.hermes/cron/jobs.json` or any systemd unit, using an older pre-Python-
pipeline manual-step convention — backed up to
`~/.hermes/scripts/.pre-symlink-backup-20260926/` and removed from the live
directory.

## Known residual gaps (carried over, unchanged from rebase-006)

- `paddlepaddle`/`paddleocr` still have no Python 3.14 wheels; excluded
  from every 3.14 venv build. Not a regression — revisit when upstream
  publishes `cp314` wheels.
- `uv.lock` cannot be regenerated cleanly in this environment while
  `paddlepaddle` stays hard-pinned for `python_version >= '3.14'` with no
  matching wheel — a structural, likely-permanent condition, not something
  today's work broke. Produces a harmless (non-blocking) warning on
  ordinary `hermes` CLI invocations via its own background dependency-
  completion check; deliberately not fixed this round (would require a
  paddlepaddle policy decision first).
- `test_dispatcher_health_telemetry_does_not_raise_nameerror`'s underlying
  fragility (a fully synchronous/tight-spinning code path can starve
  `asyncio.wait_for`'s own cancellation scheduling under real host load)
  is fixed for THIS specific test, but the general pattern — a coroutine
  that doesn't genuinely yield can defeat `wait_for` under load — may exist
  elsewhere in `gateway/kanban_watchers.py` or similar watcher code; not
  audited this round.

## Cleanup performed

- Removed the temporary dispatch worktree/branch used to hand the flaky
  test off to codex (`fix/kanban-health-window-flaky-hang-20260926`, fully
  merged into `main` first).
- Removed two confirmed-dead legacy scripts from `~/.hermes/scripts/`
  (backed up first, see above).
- Left `/tmp/apply-worktree-1790058056` alone (unrelated, predates this
  session's work, per the standing rule to only clean up what this
  session's own work superseded).
