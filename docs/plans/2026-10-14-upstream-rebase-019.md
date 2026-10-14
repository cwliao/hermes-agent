---
Status: rebase complete on branch; main NOT moved, push and deploy NOT executed
Priority: high
Date: 2026-10-14
Repository: hermes-agent
Related: 2026-10-14-upstream-rebase-018.md, hermes_upstream_apply.py
---

# Upstream rebase 019

Worktree `~/.hermes/worktrees/upstream-rebase-019-20261014`, branch
`upstream-rebase-019-20261014`, rebased from `main` (`678ee3be87`) onto upstream `d97007c32d`.
479 upstream commits absorbed; 486 fork commits replayed, 485 kept (one dropped as empty, see below).

## Conflicts

| Commit | File | Resolution | Test result |
|---|---|---|---|
| `8f7dbecaf8` fix: guard against stale model answer replay | `run_agent.py` (`_compute_non_stream_stale_timeout`) | The fork commit only inlined upstream's `cap_to_run_budget` helper. Took upstream entirely: kept both imports (`cap_to_run_budget`, `long_decode_stale_multiplier`), restored the `cap_to_run_budget(self, timeout)` call. The commit became empty and was dropped. | stale-timeout, run-budget, reasoning-floor, codex-ttfb, timeouts: 74 passed |

## Verification

- 0 syntax errors; no real conflict markers (`tests/test_audit_old_updater_imports.py:225` is a fixture string).
- Tests ran from a `git archive` copy of the tip (git-free, so `home_io_guard` does not trip on the
  worktree metadata under the real hermes home), with `PYTHONPATH` pointing at the copy.
- Only one conflict in 486 replays; the resolution is a strict subset of upstream's own function body.

## Known residual gaps

- Only the conflict-adjacent test files were run; no full-suite sweep (memory cap policy).
- `get_local_stale_timeout()` early return in the same function is a fork customization from an earlier commit and was left as is.

## Operator hand-off

Move `main` (rollback tag first), push, and deploy through `scripts/hermes_upstream_apply.py`
(previous release `hermes-upstream-20261014-rebase018`, drop-in
`zzzzzzzzzzzzzzzzzzzz-upstream-678ee3be87.conf`) were not executed. Remove worktree
`upstream-rebase-018-20261013` (tip is an ancestor of `main`) and, after deploy, this worktree.
