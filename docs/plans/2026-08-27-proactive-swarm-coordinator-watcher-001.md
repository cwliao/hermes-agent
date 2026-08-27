# Kanban swarms have no proactive/periodic supervision -- only passive, event-triggered watchers

## Problem

Once a coordinator session calls `kanban_swarm`, nothing in the system
actively monitors that swarm's health until something else happens to
trigger a reaction. Confirmed by enumerating every async watcher actually
registered by the gateway (`gateway/kanban_watchers.py`):

- `_kanban_dispatcher_watcher` (`gateway/kanban_watchers.py:1445`) -- only
  acts on tasks whose status has already flipped to `ready` (dependency
  satisfied); it does not evaluate swarm-level state at all.
- `_kanban_notifier_watcher` (`gateway/kanban_watchers.py:294`) -- only
  forwards a message when a task's status changes; pure event relay, no
  judgment.
- `kanban_heartbeat` / `heartbeat_current_worker_from_env()`
  (`tools/kanban_tools.py:484`) -- a worker proving *itself* still alive
  (extends its own claim); nothing reads these heartbeats to notice a
  worker has gone quiet and act on it.
- The circuit-breaker / triage / auto-decompose path
  (`gateway/kanban_watchers.py:1859-1919`, using
  `hermes_cli/kanban_decompose.py::decompose_task()`) is purely reactive
  -- it only fires after a task has already repeatedly re-blocked
  (`block_loop_detected`). It does not run periodic health checks; it
  waits for failure to accumulate first.

There is no periodic pass that, for each currently-active swarm (root with
a non-terminal synthesizer -- the same liveness signal established in
`docs/plans/2026-08-27-duplicate-swarm-creation-guard-001.md`), checks
things like:

- Does this swarm now have sibling orphan tasks that don't belong to its
  topology (the exact incident in
  `docs/plans/2026-08-27-orphan-pre-swarm-task-guard-001.md`, filed
  alongside this doc)?
- Has any worker gone silent (no heartbeat, no status change) for
  longer than its expected budget, without yet tripping the circuit
  breaker's re-block-count threshold?
- When the synthesizer completes, does its output actually account for
  all N lanes, or did it silently synthesize from a subset (verifier's
  `gate=pass` only proves the verifier's own contract check passed, not
  that a human/independent judgment of completeness happened)?

Today, all of this is either invisible until the user manually asks
("s o", "so what now" -- see this session's own transcript) or discovered
only in hindsight during a live regression test, as happened twice today
(commit `dcc827a3d5`'s incident, and this same session's orphan-task
incident).

## Scope note -- this is a new capability, not a bug fix

Unlike every other ticket filed today, this is not a defect in existing
code -- it's a missing capability. Flagging explicitly so reviewers judge
it on cost/value grounds (new watcher, new data model, new failure modes
of its own), not as an urgent correctness fix.

## Proposed shape (for reviewer debate -- deliberately not fully speced)

A new periodic watcher, `_kanban_swarm_supervisor_watcher`, following the
existing `_kanban_notifier_watcher`/`_kanban_dispatcher_watcher` pattern
(same singleton-lock-per-gateway-process discipline, same
`_kanban_dispatch_allowed()`/ESTOP gating, same ~5-60s poll interval --
exact interval TBD by reviewers weighing DB load vs. responsiveness).

Each tick, for every active swarm (non-terminal synthesizer):

1. **Orphan check**: reuse/call the Layer B sweep logic from
   `docs/plans/2026-08-27-orphan-pre-swarm-task-guard-001.md` if that
   ships, generalized from "only at kanban_swarm-call-time" to "on every
   tick" (catches an orphan created *after* the swarm, not just
   immediately before it -- a case the other ticket doesn't cover).
2. **Stall check**: for each non-terminal worker/verifier/synthesizer
   node in the topology, compare `last_heartbeat_at` (or last status
   change, if heartbeat data isn't reliably present for a given task) to
   an expected-budget threshold (need to establish what "expected" means
   here -- `worker_max_runtime_seconds`-style config likely already
   exists per
   `docs/plans/...telegram-agent-task-bypass...` and the
   context-compaction stale-constraint incident earlier today; reuse it,
   don't invent a new number). On breach, do not kill/retry automatically
   -- record a diagnostic event (same `record_swarm_stall_diagnostic`
   pattern as commit `dcc827a3d5`) so it's visible without the user having
   to notice a long silence and ask.
3. **Completeness check on synthesis**: out of scope for V1 of this
   watcher -- flagged as a candidate follow-up, not designed here; judging
   "did the synthesizer actually cover all lanes" needs either a
   structural check (each lane's output referenced somewhere in the
   synthesized doc) or an LLM-graded check (cost, reliability concerns of
   its own). Reviewers: confirm this should be deferred rather than
   speced now.

## Explicitly open questions for reviewers

1. Poll interval and DB-load budget -- this watcher would run one query
   per active swarm per tick; need a sense of typical concurrent-swarm
   count on this host to judge cost.
2. Should stall/orphan diagnostics be silent (visible only if someone
   inspects `kanban_show`/events) or should they proactively notify (same
   channel as `_kanban_notifier_watcher`, e.g. Telegram)? Proactive
   notification directly answers today's user request ("有沒有主動協調的
   功能 不要被動等工作做完呢") but risks notification spam if thresholds
   are miscalibrated.
3. Interaction with the existing circuit breaker: should a stall
   diagnostic from this watcher count toward or reset the existing
   `block_loop_detected` counter, or stay fully independent? Recommend
   independent for V1 (avoid coupling two systems with different
   failure-tolerance philosophies) unless reviewers see a strong reason
   otherwise.
4. Does this watcher need its own singleton lock file (like the
   dispatcher's `.dispatcher.lock`) to avoid duplicate ticks across
   multiple gateway processes, or can it safely share dispatch's existing
   lock? Given it only reads + writes diagnostic events (not task
   lifecycle mutations), a race is lower-stakes than the dispatcher's, but
   reviewers should confirm before assuming that's safe.

## Non-goals (V1)

- Not automatically killing, restarting, or reassigning stalled workers --
  diagnostics only, human/model decides next action from the visible
  event.
- Not the synthesis-completeness check (see above) -- flagged for a later
  ticket if wanted.
- Not replacing the existing circuit breaker / auto-decompose path --
  additive, independent signal.

## Verification plan (once a concrete design lands from reviewer input)

- Unit tests for the orphan/stall detection queries in isolation (given a
  fixture swarm topology + fabricated heartbeat/timestamp data).
- Test the watcher's own lock/ESTOP gating mirrors the dispatcher's
  (reuse existing test patterns from `tests/gateway/` if present).
- Live regression: run a real swarm, artificially stall one worker
  (e.g. suspend its process or hold its lock), confirm a diagnostic event
  appears within one poll interval without user intervention.
