# SWARM-LANE-TIMEOUT-RETEST-002-LEFTOVERS follow-up: notify-subscription honest-return fix

Status: implemented. Root cause not definitively pinned down despite
further investigation (three plausible theories examined and ruled
out); shipping a root-cause-agnostic defensive fix instead of
continuing to chase an intermittent, hard-to-reproduce-from-outside-
the-live-gateway cause. Needs cross-review before merge, per this
effort's established practice.

## Recap of the problem

`docs/plans/2026-08-21-swarm-lane-timeout-retest-002-leftover-issues.md`,
Finding 3: a real swarm's `kanban_swarm` tool call reported
`{"subscribed": true}` for its synthesizer task, but
`kanban_notify_subs` held zero rows for that task's id, its verifier's
id, or its root's id, minutes later. Confirmed against the live board
directly, independently re-confirmed by cross-review before that
document merged.

## Further investigation (this session)

Three specific, checkable theories from that document's "what was NOT
investigated" list were pursued and each ruled out:

1. **`add_notify_sub`'s write mechanism itself.** Called directly
   against a fresh task on the live board, then re-queried via a
   *freshly reconnected* connection (not the same connection object,
   to rule out any read-your-own-writes illusion). The row was there
   both times. The underlying `INSERT OR IGNORE` + `write_txn`
   mechanism is sound in isolation.

2. **Delete-cascade from an unrelated action.** `kanban_notify_subs`
   rows are only ever deleted by `delete_task`/`delete_archived_task`
   (`hermes_cli/kanban_db.py`), both scoped to a single `task_id` with
   no cascade to sibling tasks. Neither function was called on the
   synthesizer or its swarm during this session's investigation
   (`archive_task` -- a different function entirely -- was called once,
   on the *worker* task, not the synthesizer). Ruled out.

3. **Thread-pool ContextVar loss.** The codebase has a documented,
   real concern here (`gateway/session_context.py`'s extensive
   docstring on "cross-session ContextVar inheritance leak"), and a
   concurrent tool-execution path (`agent/tool_executor.py::
   execute_tool_calls_concurrent`) that explicitly guards against it
   via `propagate_context_to_thread`. But the common case -- a single
   `kanban_swarm` call, not running concurrently alongside sibling tool
   calls in the same turn -- goes through
   `execute_tool_calls_sequential`, which contains no
   `ThreadPoolExecutor`/`propagate_context_to_thread` reference at all;
   it runs tool functions directly on the calling thread, which already
   has the correct ContextVars bound. This doesn't rule out the
   *concurrent* path having the same class of problem in some other
   scenario, but it does rule this out as *the* explanation for the
   common single-call case this specific incident was.

**No fourth theory was confirmed either.** The exact mechanism by which
`_maybe_auto_subscribe` returned `True` (meaning `add_notify_sub` was
called and raised nothing) while the row is not readable back later
remains unknown.

## Fix: verify the write before trusting it, regardless of cause

`tools/kanban_tools.py::_maybe_auto_subscribe` now reads back its own
write before reporting success: immediately after calling
`add_notify_sub`, it queries `list_notify_subs(conn, task_id)` (same
connection, same still-open write transaction scope) and only returns
`True` if a row matching the platform/chat_id it just wrote is actually
present. If not, it logs a specific WARNING (distinct from the
existing "add_notify_sub failed" exception-path warning, so the two
failure shapes are distinguishable in logs) and returns `False`.

This is deliberately root-cause-agnostic. Whatever is silently
dropping the write -- something not yet identified in this codebase, a
future regression, or something host-specific to this one incident --
the function's own return value becomes trustworthy either way. A
`False` return already has a documented, exercised fallback path (the
caller can fall back to an explicit `kanban_notify-subscribe` or to
polling, per `_maybe_auto_subscribe`'s own docstring) -- this fix makes
that fallback actually reachable instead of the caller being told
everything is fine when it silently isn't.

This mirrors the whole investigation chain's recurring lesson,
literally: verify a claimed success against the database before
trusting it. Previously that discipline was something *this
investigator* had to apply externally, by hand, every time a worker or
a tool result claimed success. Now the system applies it to itself for
this one specific claim.

## What this does NOT fix

- Does not identify the actual root cause of the original incident.
  A future investigator with live gateway access (able to reproduce a
  real Telegram-triggered swarm creation while instrumenting the
  process, rather than reasoning from logs/state.db after the fact)
  is better positioned to find it than static analysis was.
- If the underlying cause turns out to be something that also silently
  corrupts *other* writes sharing the same code path or connection
  (not just this one notify-sub insert), this fix only covers the
  specific symptom investigated here.

## Verification

- Direct reproduction of the write mechanism (item 1 above) against
  the live board, both same-connection and fresh-reconnect reads.
- 2 new tests in `tests/tools/test_kanban_tools.py`:
  `test_maybe_auto_subscribe_reports_false_when_write_does_not_stick`
  (mocks `add_notify_sub` to return cleanly without actually
  persisting anything -- the exact failure shape the exception-path
  test doesn't cover -- and confirms `subscribed=False` and no row) and
  `test_maybe_auto_subscribe_confirms_a_genuine_write` (confirms the
  read-back check produces no false negative for an ordinary
  successful write).
- 200 tests pass across `test_kanban_tools.py`, `test_kanban_swarm.py`,
  and `test_kanban_cli.py`.
- Manual end-to-end check against the live production board (not a
  mock): bound real `gateway.session_context` session vars
  (`platform=telegram`, the real chat id), called
  `_maybe_auto_subscribe` directly, confirmed it returned `True` and
  the row was genuinely present via a direct query -- then cleaned up
  the throwaway task.
