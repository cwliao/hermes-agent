---
title: "SWARM-WORKER-DEADLINE-001 — worker response deadline with needs_input surfacing"
status: DESIGN_PROPOSED
date: 2026-08-25
type: design-proposal
ticket: SWARM-WORKER-DEADLINE-001
target_repo: hermes-agent
---

# SWARM-WORKER-DEADLINE-001

## Motivation

Live E2E observed 2026-08-25 (root `t_4ab5f8b0`): 3 of 4 workers
(native_hermes, claude, agy) completed; the grok worker (`t_b371603b`)
blocked with `block_kind=needs_input` ("no explicit goal details... need
clarification"). The verifier waits for `verified_lane_count` to equal
the full original worker count, so it never became ready, and the
synthesizer never ran. The swarm has been stuck for 10+ minutes with no
notification to the user that a worker is waiting on clarification --
the user has no way to answer it, and no way to know the swarm is stuck.

An existing mechanism (`SWARM-PARTIAL-QUORUM-001`,
`excuse_blocked_workers_below_quorum`) already excuses blocked workers
once enough siblings reach `done` -- but it is opt-in
(`worker_quorum=None` by default, not set by the live swarm-creation
path) and deliberately EXCLUDES sticky blocks
(`needs_input`/`capability`) from auto-excuse, by design, per a prior
cross-review that distinguished "gave up after retries" (safe to
auto-excuse) from "a worker is asking a human a real question" (must
not be silently discarded). That distinction is correct and must be
preserved -- this ticket does not relax it. It adds a NEW, separate
path: a time deadline, after which an unanswered worker (regardless of
why it hasn't produced a result) is excused, but a `needs_input` worker
is NOTIFIED to the user before being excused, not silently dropped.

## Approved default

`worker_deadline_seconds = 660`, measured from the swarm root's
creation time. Applies as the new DEFAULT behavior for every swarm
(unlike `worker_quorum`, which stays opt-in) -- confirmed with the user.

Revised from an initial 300s after design-review consensus (claude/
codex/agy, 2026-08-25): external CLI lanes (claude/grok/agy) default to
`max_runtime_seconds=600s`, so a 300s root-based deadline would excuse
(and terminate) healthy external workers still inside their own
legitimate runtime budget. 660s matches `_SYNTHESIZER_OVERALL_DEADLINE_
SECONDS` already established in KANBAN-SWARM-002 -- reusing an existing,
already-reviewed constant rather than introducing a second magic
number -- and clears the 600s external-lane ceiling with a 60s margin.

## Scope

1. **New dispatcher-tick function** `excuse_overdue_workers(conn)`,
   parallel to and independent from `excuse_blocked_workers_below_quorum`
   (called alongside it, before `recompute_ready`, same tick).
   For every task where `contract.role == "worker"` and status is one
   of `running`/`ready`/`todo`/`blocked` (not yet `done`/`archived`):
   - Resolve the swarm root's creation time (root `created_at` or
     `started_at` -- confirm which the swarm-creation path actually
     sets and use that consistently with the rest of the codebase's
     "first attempt start" convention already established in
     KANBAN-SWARM-002).
   - If `now - root_created_at < worker_deadline_seconds`: skip, not
     overdue yet.
   - If overdue and `status == 'running'`: this worker has a live
     process. Do NOT blindly archive, and do NOT call `reclaim_task`
     (releases the claim unconditionally without confirming the PID is
     actually dead -- unsafe for this use case, per design review).
     Reuse the EXACT pattern `release_stale_claims` already uses:
     `_terminate_reclaimed_worker` -> `_worker_survived_termination` ->
     if the worker survived, `_defer_reclaim_for_live_worker` (extend
     and retry next tick, do NOT excuse); only `archive_task` once
     termination is confirmed. This is an existing, already-tested
     primitive -- do not re-derive new kill/poll logic (KANBAN-SWARM-002
     added its own only because the synthesizer needed a LONGER,
     role-specific grace window; workers don't need a new grace value,
     so there's no reason to duplicate the pattern here).
   - If overdue and `status == 'blocked'` with a sticky block kind
     (`needs_input`/`capability`, via the same `kb._has_sticky_block`
     primitive `excuse_blocked_workers_below_quorum` already uses):
     **notify before excusing** (see Notification below), then excuse.
   - If overdue and `status` is `ready`/`todo`/non-sticky-`blocked`:
     excuse directly (same archive path as the existing quorum
     mechanism), no notification needed (nothing worth surfacing).
   - "Excuse" = the same `kb.archive_task` call
     `excuse_blocked_workers_below_quorum` already uses, so both paths
     produce identical downstream task state -- no new terminal state
     invented.

2. **Verifier contract must become deadline-aware.** Today
   `expected_lane_count` is a fixed number baked into the contract at
   swarm-creation time; a verifier that never sees all original workers
   reach `done` can never satisfy `verified_lane_count ==
   expected_lane_count`. Revised per design review: writing
   `excused_worker_ids` to the blackboard and archiving the worker MUST
   happen in the SAME write transaction/tick (not two separate calls
   that could interleave with a concurrent verifier read) -- reuse
   `write_txn` around both the `post_blackboard_update` call and the
   `archive_task` call, not two independent transactions. `validate_
   completion` (kanban_swarm.py) currently parses only the static
   `task.body` with no DB connection -- giving it live access to the
   blackboard's `excused_worker_ids` requires a signature change
   (threading a `conn` through, or precomputing the effective count
   once excusing finishes for this tick and stamping it as a static
   value the verifier's own contract text is regenerated to reference).
   This is a larger implementation surface than originally scoped --
   confirm the exact mechanism before writing code, don't improvise it
   mid-implementation. Floor: reuse `MIN_EXTERNAL_LANES`-style reasoning
   already in this file (do not invent a second floor constant) --
   never let a verifier pass on 0 real lanes.

3. **Notification for a `needs_input` worker excused by deadline.**
   Requirement: the user must see the worker's actual question, not a
   silent excuse. Revised per design review: do NOT do a synchronous
   "direct push" from inside `excuse_overdue_workers` -- that function
   runs inside `dispatch_once`, a synchronous DB routine that can be
   invoked standalone via CLI (`hermes kanban dispatch`) with no
   gateway platform adapter or event loop present; a direct push call
   would have nothing to actually deliver through in that context.
   Instead, `excuse_overdue_workers` appends a new, structured
   `task_event` (bounded payload: task id, lane/skill id, the worker's
   `needs_input` reason text truncated to the same 400-char cap other
   event payloads already use) on the SWARM ROOT (which already carries
   `origin_platform`/`origin_chat_id`/`origin_thread_id`, inherited by
   every child). The existing async `_kanban_notifier_watcher` in
   `gateway/kanban_watchers.py` (already has subscription-cursor
   handling, dedup, adapter resolution, and retry -- do not bypass it)
   picks up the new event kind the normal way IF something is
   subscribed to the root. Since nothing currently auto-subscribes to
   root (only the synthesizer gets auto-subscribed, per prior
   investigation), this ticket must ALSO auto-subscribe the root at
   swarm-creation time specifically for this new event kind -- read
   `_maybe_auto_subscribe`'s exact semantics before implementing, and
   make sure this addition doesn't also start delivering the root's
   OTHER events (i.e. only wire the new event kind into the watcher's
   render/dispatch logic, don't let existing root event kinds that were
   never meant to notify suddenly start firing because root now has a
   subscription row). Message content must explicitly state the swarm
   is proceeding without this lane's input, per KANBAN-SWARM-002's
   "notifier messages must stay truthful" principle -- never implies
   the swarm is waiting for the user's answer when it has already
   moved on.

4. **Partial shared-state written before excuse.** Acknowledged gap,
   no rollback attempted (archiving does not undo files/comments a
   worker already wrote). Mitigation: the synthesizer's prompt/context
   must be told which lanes were excused (via the same blackboard
   `excused_worker_ids` list) so it does not treat an excused lane's
   partial artifacts as a trustworthy, complete contribution.

## Explicitly out of scope for this ticket

- Does NOT change `excuse_blocked_workers_below_quorum` or
  `worker_quorum` semantics -- additive, parallel mechanism.
- Does NOT let the user's answer to a surfaced `needs_input` question
  feed back into the swarm after the deadline -- once excused, that
  lane's slot is gone for this swarm run. (Open question for a later
  ticket: should a fast human reply within some grace window still be
  usable? Not attempted here -- keeps this ticket's blast radius small.)
- Does NOT touch KANBAN-SWARM-002's synthesizer-role logic at all --
  fully independent code paths (workers vs. synthesizer).

## Required before implementation

Per the KANBAN-SWARM-002 precedent: cross-review this design (claude/
codex/agy/grok/groq via agentpool's dispatch-fanout.js) before writing
code, same as the prior ticket's two-round (design, then code) process.
