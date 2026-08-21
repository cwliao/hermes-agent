# SWARM-PARTIAL-QUORUM-001

Status: implemented, 2026-08-21. Needs cross-review before merge, per
this effort's established practice.

## Problem

Real Telegram-triggered 4-lane swarms consistently never delivered a
final result, even after fixing the three prior root causes in this
same investigation chain (PR #93-96: contention-driven timeouts, a
Tirith false positive, unclear blackboard-write instructions, and
`native_hermes`'s own too-tight default). Auditing every swarm ever
created on this board found the pattern directly: verifier/synthesizer
completed in only 4 of the last 12 real swarms; every one of the last 4
consecutive attempts (spanning the exact window this whole
investigation covers) left both stuck at `todo` forever.

**Root cause**: `hermes_cli/kanban_db.py::recompute_ready` promotes a
task from `todo` to `ready` only once *every* parent is `done` or
`archived`. `create_swarm` makes the verifier a child of all four
worker tasks. A worker that exhausts the dispatcher's retry budget
lands in `blocked` (via `_record_task_failure`'s circuit breaker) --
neither `done` nor `archived` -- and stays there permanently unless a
human runs `kanban unblock` or `kanban archive` by hand. One
permanently-failed lane therefore deadlocks the verifier, and
transitively the synthesizer, and transitively the Telegram
notification that only fires on synthesizer completion -- forever,
with no automatic recovery.

**Compounding discovery**: while investigating, the bot itself (a
small local model, `ornith:35b`, running this exact swarm over
Telegram) confidently told the user a `min_3_workers_success` /
`≥3 of 4 lanes` quorum gate already existed and was about to fire.
Grepped the entire codebase -- no such mechanism existed anywhere. The
bot was not lying about a real feature; it fabricated one it wished
existed, then narrated fake progress ("🔄 spawning workers...") against
a swarm that had actually been dead for hours. The user asked for this
exact quorum semantics independently, in their own words, in the same
Telegram message -- confirming it's a real, wanted capability, not
just something to correct the bot's narration about.

## Fix: `worker_quorum`, opt-in, `None` by default

`create_swarm()` gains a `worker_quorum: Optional[int] = None` keyword
argument (lane-bound swarms only; raises `ValueError` for a non-lane
swarm or an out-of-range value). Two changes work together -- neither
alone is sufficient:

1. **The verifier's own completion contract.** `expected_lane_count` in
   the verifier's `[swarm:contract]` line becomes `worker_quorum`
   instead of the full worker count when a quorum is set.
   `validate_completion` already reads `expected_lane_count` straight
   from the contract embedded in the task body, so it automatically
   requires `verified_lane_count == worker_quorum` with no changes to
   `validate_completion` itself. Without this half, the verifier's own
   kernel-enforced contract would keep rejecting a partial pass even if
   the dispatch graph let it run (`_completion_requirements`'s own
   docstring: "a smaller count is rejected... do not complete with a
   subset" -- that text was deliberately written for the all-lanes
   case and is exactly what needed adjusting for a genuine quorum).
2. **`excuse_blocked_workers_below_quorum(conn)`** (new function in
   `kanban_swarm.py`, called from `kanban_db.dispatch_once` once per
   tick, right before `recompute_ready`): finds `blocked` tasks
   carrying a swarm worker contract, looks up their swarm's topology
   blackboard entry for a configured `worker_quorum`, counts how many
   sibling workers already reached `done`, and archives the blocked
   task once that count meets the quorum. `archive_task` already calls
   `recompute_ready` internally, so the verifier can become `ready` in
   the same tick it's finally excused.

Swarms created without `worker_quorum` are completely unaffected --
both changes are no-ops for them, and the strict all-workers-required
behavior is unchanged from before this feature existed.

The dispatcher-side hook required a deferred import
(`kanban_db.dispatch_once` importing `kanban_swarm` inside the function
body, not at module level) since `kanban_swarm.py` already imports
`kanban_db` -- a module-level import the other direction would be
circular.

CLI (`hermes kanban swarm --worker-quorum N`) and the `kanban_swarm`
tool schema both expose the new parameter with the same rationale in
their help/description text, matching how `worker_max_runtime_seconds`
was already exposed.

## What this does NOT fix

- Does not change anything about *why* a worker fails in the first
  place -- it only stops one permanent failure from silently deadlocking
  everything downstream. The prior fixes (PR #94-96) still matter for
  reducing how often a lane fails at all.
- Does not retroactively fix already-stuck swarms sitting in `kanban.db`
  from before this deploy (their verifier tasks have no `worker_quorum`
  in their topology blackboard, since they were created before this
  existed) -- those need manual `kanban archive`/`kanban unblock`
  intervention, or should simply be abandoned in favor of a fresh swarm.
- Does not address the bot's fabrication of a nonexistent feature as a
  general pattern -- that's a model-capability/prompting concern
  (`ornith:35b` is a small local model), out of scope for a kanban-swarm
  code fix. Recorded here as a concrete example of why worker
  self-reports (block reasons, status narration) need independent
  verification against the actual database, same lesson as the earlier
  agy fabrication investigation in this same session
  (`docs/plans/2026-08-20-swarm-agy-headless-oauth-block-001.md`).

## Verification

- New tests in `tests/hermes_cli/test_kanban_swarm.py`: quorum bounds
  validation, lane-mode requirement, verifier contract/topology
  correctness with and without a quorum, `excuse_blocked_workers_
  below_quorum` archiving once satisfied and unblocking the verifier to
  `ready` in the same call, staying a no-op below quorum, staying a
  no-op for swarms with no quorum configured, and never touching an
  unrelated blocked task with no swarm contract at all. 27 tests pass
  in `test_kanban_swarm.py`.
- Manually confirmed via direct SQL against `~/.hermes/kanban.db` that
  the historical pattern (4/12 swarms ever completed; last 4
  consecutive attempts all stuck) is real, not an assumption.
