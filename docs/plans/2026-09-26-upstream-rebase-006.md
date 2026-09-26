# Upstream rebase 006 (2026-09-26)

Status: **Deployed and verified live.** `main@4e34b48dee` is running in
production as of 2026-09-26 09:13 CST.
Priority: High (771-commit full rebase; recovered a 66-commit deploy gap
left by rebase-005 and absorbed 284 further upstream commits; also fixed
three real bugs in `scripts/hermes_upstream_apply.py` found along the way)
Repository: `~/.hermes/hermes-agent`
Related: `docs/plans/2026-09-25-upstream-rebase-005.md` (its "Deploy
incident" section documents the first two `hermes_upstream_apply.py` bugs
this round's deploy hit the third of),
`docs/plans/2026-09-18-upstream-rebase-1356-commits-002.md`,
`docs/plans/2026-09-22-upstream-rebase-003.md`,
`docs/plans/2026-09-23-upstream-rebase-004.md`

## Why this round happened

A routine "check upstream update" surfaced that rebase-005's approved and
deployed candidate (`530cd6ff91`) was never actually reflected in the `main`
branch ref — `hermes_upstream_apply.py` builds its release from a detached
worktree at the candidate SHA and never resets `main` to match. `main`
stayed on my own manually-rebased tip (`e56b0c45e6`, based on upstream
`c16e8037d2`), 66 commits behind what was actually running in production
(which had been rebased by the review script onto the newer `fdec926ef5`).
A same-day merge of `feat/goal-reply-resume` (376 commits, see
`docs/plans/2026-09-25-upstream-rebase-005.md`'s merge commit) was built on
top of that stale `main`, and redeploying it silently regressed those 66
upstream commits' worth of changes (confirmed: all 137 files they touched
differed from what was actually live beforehand).

The fix requested and executed: don't patch the 66-commit gap in isolation —
do a full fresh rebase of every local-only commit onto the *current* latest
upstream tip (`283de37b6b`, 284 commits newer than the stale base), which
recovers the gap and absorbs today's upstream sync in one pass.

## Scope and starting point

- Worktree: `upstream-rebase-006-20260926` at
  `/home/cwliao/.hermes/worktrees/upstream-rebase-006-20260926`
- Base before rebase: `main` @ `53147d8045` (the `feat/goal-reply-resume`
  merge commit)
- `git fetch upstream main`: `fdec926ef5..283de37b6b`
- At the start: upstream ahead by 284 commits from where the last real
  rebase landed; local-only commits to replay: **771** (393 from rebase-005
  + 376 from the goal-reply-resume merge + 2 fixup/doc commits, all now
  unified onto one lineage for the first time)
- Rollback tag: `backup/pre-upstream-rebase-006-20260926` (on `53147d8045`)
- New `main` tip after `git reset --hard` + the post-rebase fixup commit:
  `4e34b48dee`

## Conflict density and resolution summary

~35-40 real conflicts across the 771 replayed commits, resolved with a mix
of manual per-hunk resolution (for anything genuinely ambiguous or where
both sides had real, different intent) and a verified batch shortcut for
the long run of already-known-content commits (the `goal-reply-resume`
portion, whose final state I had already hand-merged once the day before —
see "Batch-resolution shortcut and its regression" below for the risk this
introduced and how it was caught).

Representative categories:

| Category | Example |
|---|---|
| Two independent guards, keep both | `hermes_cli/model_switch.py`'s `is_known_non_chat_model` (HEAD) + `_forbidden_model_message` (incoming) — different features, not a rename |
| Consolidated function needs a feature threaded through | `agent/auxiliary_client.py`'s `_resolve_codex_credential_and_base()` (HEAD's later consolidation of pool-selection + fallback) was missing the `model` parameter the incoming commit's whole point was to add — the real fix was adding `model` to the consolidated function, not choosing either side wholesale |
| Duplicate content from diff3 misalignment (no real conflict) | `hermes_cli/kanban_db.py`'s `find_dead_graphs`/`gc_events` appeared once inside a conflict marker and again unconflicted immediately after — same failure mode as rebase-005; fixed by deleting the true duplicate, keeping the single correct copy |
| A test's docstring and body mismatched after diff3 misalignment | `tests/tools/test_delegate.py`'s `test_saved_tool_names_set_on_child_before_run` had a docstring describing one behavior but a body that was actually a different, adjacent test's content, borrowed by the misalignment — fixed by pulling the real body from `feat/goal-reply-resume`'s own copy of the file directly |
| Deliberately-removed upstream feature (not a bug) | `agent/chat_completion_helpers.py` is missing `_bound_openai_codex_stale_timeout`/`_stream_env_stale_base`/`cap_to_run_budget` — confirmed via a clean isolated cherry-pick of the file's real local-commit history onto `upstream/main` that this is an intentional local simplification from commit `ece51989c1`, not a lost feature |

## Batch-resolution shortcut and its regression (found and fixed)

For the long run of conflicts caused by replaying `goal-reply-resume`'s 376
commits (already fully and carefully hand-merged the day before), I used a
scripted loop: for any conflict where every touched file already existed in
the known-good `53147d8045` merge commit, reset the file(s) to that commit's
content and continue. This correctly and quickly resolved dozens of
docs/test/feature commits — but it has a real failure mode: **the reference
commit (`53147d8045`) predates today's upstream sync**, so any file that
also legitimately received *new, unrelated* upstream content between the old
base and `283de37b6b` had that content silently reverted, even though the
local commit actually conflicting there never touched it.

Caught via a repo-wide sweep: for every file differing from `upstream/main`,
compare its top-level `def`/`class`/UPPER_CASE-constant names against
`upstream/main`'s version and flag anything missing. Two real regressions
surfaced and were fixed by hand (see commit `d037c308ed`):

- `hermes_cli/gateway.py` was missing `GATEWAY_RESTART_WATCHER_TIMEOUT_S`,
  `_restart_argv_is_host_gateway`, `_host_gateway_watcher_env` — a real
  upstream restart-watcher host-vs-profile feature with nothing to do with
  the local commit (`#91`, a CLI/gateway code-drift doctor check) that
  conflicted there. Fixed by taking `upstream/main`'s version of the file
  and re-inserting the one genuine local addition
  (`_read_systemd_unit_environment`) at its correct position.
- `plugins/kanban/dashboard/plugin_api.py` was missing `_since_param`/
  `_latest()` (a cursor-based event-replay fix avoiding a board-open
  refetch storm), reverted to an older `_int_param` helper. Fixed via a
  clean cherry-pick of the one real local commit (`df1ec53891`) onto
  `upstream/main`.

Three further files flagged by the same sweep (`agent/chat_completion_helpers.py`,
`tests/test_log_isolation.py`, `tests/tools/test_kanban_tools.py`) were
confirmed as false positives — a clean cherry-pick of each file's actual
local-commit history onto `upstream/main` reproduced the current content
exactly, meaning the "missing" symbols were deliberate local
renames/removals, not lost upstream features.

**Lesson for next time**: a batch-reset shortcut during a rebase must
compare against the rebase's actual upstream target, never an earlier
commit, however well-verified that commit was at the time it was made.

## Post-rebase verification (capped, scoped — no blind full-suite sweep)

```
grep, full repo, all conflict-marker patterns: 2 hits, both confirmed false
  positives (same two files as rebase-005: a test embedding conflict-marker
  text as fixture data; a reST section-underline in a docstring)
ast.parse sweep, whole repo: 0 syntax errors
pytest --collect-only, FULL repo: 57023/57081 tests collected, ZERO
  collection/import errors (58 deselected is normal marker-based exclusion)
```

Scoped test run across every touched/adjacent file: 37 failures, all
confirmed identical on unmodified `main` (the same pre-existing
`test_doctor.py` failures and the worktree-path `home_io_guard` artifact
documented in rebase-005) — zero new regressions.

## A third `hermes_upstream_apply.py` bug found during THIS deploy

The "each deploy's drop-in filename carries strictly more leading `z`
characters than every prior one" convention (see rebase-005's "Deploy
incident") has no ceiling. After ~30 releases the longest existing filename
had grown to 222 z's (247 characters); computing "20 more" for this
deploy produced a 267-character name, exceeding the filesystem's `NAME_MAX`
(255) — the write failed outright with `OSError: File name too long`,
blocking the deploy after everything else (venv, release, smoke test) had
already succeeded.

Fixed in `scripts/hermes_upstream_apply.py` (commit `4e34b48dee`):
`_compute_dropin_path` now prunes first, in two passes:

1. `_prune_superseded_dropins`: cross-file analysis over every directive key
   (`ExecStart`/`ExecStopPost`/`WorkingDirectory`/each `Environment=`
   variable) each drop-in sets. A file is provably dead if every key it
   sets is also set by some later-sorting file, since systemd merges those
   per-key and the later file already re-asserts everything. Verified
   against the real production directory (137 files): 136 were provably
   dead, correctly leaving only the true current winner.
2. A second pass removes anything still matching this script's own
   self-managed naming pattern (`z+-upstream-<sha>.conf`) outright, since
   the new file about to be written always carries forward every key from
   whatever was live (`_render_dropin`'s contract) — this catches the case
   the cross-file check alone can't (two releases in a row both
   introducing a brand-new key the older one never had).

New unit tests cover both the cross-file dead-code elimination (a
foundational, unrelated drop-in with a unique key must survive) and the
regression itself (a 20-file synthetic history exceeding `NAME_MAX` is
correctly reduced to a single short filename).

**A self-inflicted near-incident while fixing this**: after validating the
new pruning logic thoroughly against a *copy* of the real 137-file
directory, I called `_compute_dropin_path` directly against the *real* live
systemd drop-in directory to compute today's actual deploy filename — this
correctly pruned (deleted) all 137 files as a side effect, but I only
computed the new filename in that call, without also writing the
replacement content in the same step. This left the live service's drop-in
directory transiently empty (production kept running fine since nothing
triggered `daemon-reload` in the gap, but a stray reload/restart in that
window would have lost every environment override — including which
release/venv to run). Recovered within the same turn by writing a cached
copy of the already-rendered, already-verified drop-in content for the
target release before any reload happened.

**Lesson for next time**: a function confirmed to delete real files as a
side effect must never be called against a real/production path for "just
the read/compute half" of a two-part operation — the paired write has to
happen in the same breath, no matter how well the logic was already
validated against a copy.

## Deploy sequence actually used

```bash
# venv + release (already built and smoke-tested before the drop-in bug was found)
git worktree add --detach <worktree> main
python3 -c "from release_snapshot import build_snapshot; build_snapshot(...)"
python3 -c "from hermes_upstream_apply import _provision_release_venv, _previous_extras; ..."
# -> venv gateway-d037c308ed (Python 3.14), paddlepaddle excluded (no cp314 wheel, same as rebase-005)

# drop-in (blocked once by the filename-length bug, then fixed and retried)
python3 -c "from hermes_upstream_apply import _compute_dropin_path, _render_dropin; ..."
systemctl --user daemon-reload
systemctl --user restart hermes-gateway.service
```

Verified after restart: `systemctl --user is-active` stable across repeat
checks (no crash-loop), same PID sustained, `import hermes_cli`/`gateway`
resolved from a **neutral** cwd correctly points at the new release
directory (not the checkout — see the neutral-cwd lesson from rebase-005),
and the live process's own `/proc/<pid>/cwd` matches `WorkingDirectory=` in
the effective drop-in.

## Known residual gaps (carried over, unchanged from rebase-005)

- `paddlepaddle`/`paddleocr` still have no Python 3.14 wheels; excluded from
  every 3.14 venv build. OCR falls back to Tesseract. Not a regression —
  same gap as rebase-005, revisit when upstream publishes `cp314` wheels.
- `uv.lock` cannot fully regenerate in this sandbox (pre-existing
  `spacy==2.0.17`/`kittentts` build failure under Python 3.14).
- The `feat/goal-reply-resume` branch and its worktree were cleaned up
  (deleted) after confirming full merge into `main` — no longer an open
  question.

## Cleanup performed

- Removed the `upstream-rebase-006-20260926` worktree and branch (confirmed
  fully merged into `main` first).
- Removed the two now-superseded release-build worktrees
  (`apply-hermes-upstream-006-20260926`, `apply-merge-hermes-merge-20260926-53147d80`).
- Left `/tmp/apply-worktree-1790058056` alone (unrelated, predates this
  session's work, per the standing rule to only clean up what this
  session's own work superseded).
