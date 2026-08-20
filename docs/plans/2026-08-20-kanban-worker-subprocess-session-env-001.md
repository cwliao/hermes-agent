# WORKER-SUBPROCESS-SESSION-ENV-001

Status: implemented and cross-reviewed. Ready to merge (pending user
approval to push/open a PR -- not done unilaterally). Independent review (a
separate agent, re-derived every claim from source rather than trusting this
ticket's prose) confirmed `_default_spawn`'s env construction, confirmed
`_maybe_auto_subscribe`/`_maybe_auto_subscribe_swarm`'s silent-False
behaviour, confirmed `_inject_session_context_env` as real working precedent,
and additionally checked whether `tasks.session_id`/`HERMES_SESSION_ID`
could shortcut the fix -- it can't (already deliberately excluded upstream
as a notify-target proxy, per the code comment near
`tools/kanban_tools.py`'s `_maybe_auto_subscribe`, referencing a prior
revert). Reviewer's recommendation, adopted below: implement candidate 1
(persist platform/chat_id/session_key on the task row at creation, propagate
to descendants) -- candidate 2 (reuse `kanban_notify_subs`) is strictly
weaker since it has no data to propagate until *someone* has already
subscribed, which can't bootstrap a worker's own first nested subscribe
attempt.

## Context

Follow-up from
[`2026-08-20-session-handover-gate8-swarm-line.md`](2026-08-20-session-handover-gate8-swarm-line.md).
That handover flagged an intermittent defect in `kanban_notify_subs`
(sometimes a swarm's synthesizer result reaches Telegram, sometimes it
silently doesn't) and left two threads open: "which code path is actually
running live" and "the dispatcher-subprocess gap is separate and already
confirmed." This ticket is the second thread only. The live-path mystery from
the handover is resolved as a *different* ticket's finding (see
`2026-08-20-notify-subs-debug-log-location-and-hermes-cli-drift-001.md`): a
follow-up debug-tracing session confirmed `tools/kanban_tools.py::
_maybe_auto_subscribe` executes correctly and successfully wrote a
`kanban_notify_subs` row for a live `kanban_swarm` tool call today (tenant
`fall-jokes-v3`, synthesizer `t_8bdb7c47`) -- the earlier session's debug log
had actually fired; it was written to `~/.hermes/logs/agent.log`, not
`~/.hermes/logs/gateway.log`, which is where that session was looking.

That successful run does not mean the underlying intermittent-failure symptom
is explained. This ticket is the one concretely-confirmed-by-code-reading gap
that remains, independent of whichever notify-subscribe call fires on the
orchestrator's own turn.

## The gap

`hermes_cli/kanban_db.py::_default_spawn` is what the kanban dispatcher uses
to spawn a worker subprocess for a claimed task. It builds the child's
environment as:

```python
env = dict(os.environ)
# ... then several explicit env["..."] = ... assignments follow
```

None of those explicit assignments set `HERMES_SESSION_PLATFORM`,
`HERMES_SESSION_CHAT_ID`, or `HERMES_SESSION_KEY` -- confirmed by reading the
function directly, not inferred. `os.environ` at the point `_default_spawn`
runs is the **dispatcher's own** process environment (the embedded gateway
process, or a standalone `hermes kanban dispatch` process), which does not
carry the *per-turn* ContextVar-derived session identity of whichever
Telegram/Discord/etc. conversation originally asked for the swarm -- those
are set per-request via `gateway/session_context.py`'s ContextVars, not
inherited from the long-lived dispatcher process's own OS environment.

Consequence: `tools/kanban_tools.py::_maybe_auto_subscribe` and
`hermes_cli/kanban.py::_maybe_auto_subscribe_swarm` both resolve platform/
chat_id via `get_session_env("HERMES_SESSION_PLATFORM"/...)`, which falls
back to `os.environ` when the ContextVar is unset. Inside a dispatcher-spawned
worker subprocess, the ContextVar is never set (fresh process) *and* the env
var fallback is also never set (this gap) -- so any `kanban_create` or
`kanban_swarm` call made **from inside a worker's own turn** (e.g. a worker
that itself wants to fan out sub-work and get notified) will always silently
resolve to `platform=""`/`chat_id=""` and return `False` from
`_maybe_auto_subscribe` with no exception, no warning above DEBUG, and no
visible symptom until someone checks `kanban_notify_subs` directly.

This is distinct from -- and narrower than -- the orchestrator-level
subscribe that happens synchronously inside the `kanban_swarm` tool call
itself (that one runs in the *live* conversational turn's process, which does
have the ContextVars set correctly, per the confirmed-working trace in the
companion ticket). This gap only bites subscribes attempted **from within** a
worker subprocess.

## Prior art already in the codebase for the fix

`tools/environments/local.py::_inject_session_context_env` already solves
exactly this problem for the `terminal` tool's own subprocess spawns -- it
reads the current ContextVars and injects them into the subprocess env before
exec. `_default_spawn` needs the equivalent: read
`HERMES_SESSION_PLATFORM` / `HERMES_SESSION_CHAT_ID` / `HERMES_SESSION_KEY`
(and probably `HERMES_SESSION_THREAD_ID` / `HERMES_SESSION_USER_ID` /
`HERMES_SESSION_PROFILE` to match what `_maybe_auto_subscribe` also reads)
from the *dispatching* context -- i.e. from whichever session originally
created the task being dispatched, most likely already recorded on the task
row itself (`kanban_notify_subs` for the swarm's synthesizer, or fields on
the `tasks` row) rather than from the dispatcher's own ambient ContextVars,
since the dispatcher is a long-lived background loop, not a per-request
handler, and does not have a "current conversation" ContextVar to read from
at spawn time.

**Design decision, resolved by cross-review:** candidate 1 below. Candidates
2 and 3 are recorded for context on why they were rejected.

1. Store platform/chat_id/session_key on the `tasks` row at creation time
   (e.g. as part of `created_by`/a new column) and have `_default_spawn` read
   them off the task being spawned, propagating down from whichever session
   created the root swarm task.
2. Look up the existing `kanban_notify_subs` row for the task (or its swarm
   root) and reuse those values, on the theory that if nobody subscribed,
   there is also nothing to propagate.
3. Something else -- e.g. only propagate when the *spawning* task itself is a
   dispatcher-tracked descendant of a swarm with a known subscriber.

Whichever design is chosen, match `_inject_session_context_env`'s variable
set exactly, and add a regression test that spawns a worker (or mocks
`_default_spawn`) and asserts the child env contains the three vars when the
parent task has known session identity.

## Suggested implementation shape (for the cross-reviewer to weigh in on before code is written)

- Extend `_default_spawn`'s env-building to look up the task's originating
  session identity (per whichever of the three candidates above the reviewer
  agrees is correct) and set `HERMES_SESSION_PLATFORM` / `_CHAT_ID` / `_KEY`
  (+ thread/user/profile if available) into the child env dict before spawn.
- Add a test in `tests/hermes_cli/test_kanban_db.py` (or wherever
  `_default_spawn` is currently tested) asserting the env propagation.
- Do **not** change `_maybe_auto_subscribe` / `_maybe_auto_subscribe_swarm`
  themselves -- they already do the right thing once the env vars are
  present; this ticket is entirely about the spawn boundary.

## Implementation cross-review

Implemented and independently cross-reviewed (separate agent, re-derived
every claim from the diff and reran the test suite itself rather than
trusting the commit message). Verdict: ready to merge, no correctness bugs
found. One nuance the reviewer flagged, worth recording: `create_task()`'s
parent-inheritance is shared code, so `hermes_cli/kanban.py::_cmd_create`
(CLI `--parent`) and `plugins/kanban/dashboard/plugin_api.py`'s dashboard
task creation -- neither of which was edited -- will now also transitively
stamp `origin_*` onto a child task created under a parent that has it set.
This is intentional (the ticket's design decision propagates to all
descendants, not just tool-created ones) and inert until `_default_spawn`
reads it, but it is a real behavior change to those call sites' output rows
even though their own code is untouched.

Test suite: the full `-k kanban` sweep across `tests/` shows 17 pre-existing
failures (stale-claim/PID/detect-stale/reap tests in `test_kanban_db.py`,
fanout tests in `test_kanban_decompose.py`, hook tests in
`test_kanban_lifecycle_hooks.py`, `test_kanban_worker_runs.py::
test_terminate_run_ok`) -- independently reproduced identically on a clean
`origin/main` checkout with zero changes (17 failed / 1097 passed there vs.
17 failed / 1102 passed on this branch; the +5 is exactly this ticket's new
tests). Confirmed test-order/isolation flakiness unrelated to this diff, not
a regression.

## Process notes

- This ticket must go through cross-review (a second independent read) before
  implementation starts, and the implementation itself must be cross-reviewed
  again before merge -- per this effort's established working rule.
- Confirm with the user before any deploy/restart needed to verify the fix
  live; `hermes-gateway.service` on `55-0940189-03` is the only environment
  and is live production.
