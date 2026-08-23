# CLI resume transcript replay / duplicate persistence

Status: **implementation and synthetic ten-call acceptance complete; deployment verification remains open**.

This is a follow-up to
[`2026-08-23-session-transcript-replay-idempotency-001.md`](2026-08-23-session-transcript-replay-idempotency-001.md).
That work fixed several gateway-side persistence paths, but the synthetic
probe below found a sibling gap in the CLI resume path.

## Reproduction

On the deployed source-equivalent checkout, run ten sequential quiet CLI calls
against one newly-created named session. Each prompt contains a unique nonce
and forbids tools, cron, and reuse of prior results:

```text
Synthetic replay probe only. This is turn N of 10, nonce SYNTH-N-8f3c.
Do not call tools, do not run cron/jobs, do not claim any previous job result,
and do not use prior unfinished work. Reply with exactly one line:
PROBE_ACK N SYNTH-N-8f3c
```

Observed session: `20260823_115458_b9514b`.

- Ten calls were attempted against the same resumed session.
- Calls 7 and 9 timed out without a final response.
- Call 10 returned both the previous unfinished nonce (`SYNTH-9-8f3c`) and
  the current nonce (`SYNTH-10-8f3c`) in one assistant row.
- The state DB contained 58 rows: 31 user and 27 assistant rows. The first
  eight nonce turns were persisted four times each; the final assistant row
  contained the combined stale/current output.
- This is a CLI resume/persistence reproduction. It does not by itself prove
  that native Telegram ingress follows the same missing call path.

The synthetic rows are retained as evidence for this ticket. They contain no
secrets, tools, cron requests, or production job identifiers.

## Primary source findings (implementation review)

1. The CLI initialization path in
   `hermes_cli/cli_agent_setup_mixin.py::_init_agent()` restores a resumed
   session with:

   ```python
   self._session_db.get_messages_as_conversation(
       self.session_id, repair_alternation=True
   )
   ```

   It omits `include_row_ids=True`.

2. `hermes_state.py::_rows_to_conversation()` only adds the durable `_row_id`
   when that opt-in flag is true. Therefore the CLI's restored dictionaries
   have no durable identity after a process boundary.

3. `run_agent.py::_flush_messages_to_session_db_unlocked()` skips durable
   history by `_row_id`, `_db_persisted`, or the current in-memory history
   identity. The first two are unavailable after this CLI restore. That is a
   real cold-copy safety gap, but it is not yet sufficient to explain every
   duplicate observed in the ten-call probe: the quiet CLI normally passes the
   same restored dicts as `conversation_history`, and that identity baseline
   should skip them. The implementation must therefore capture which flush
   call had a missing/rewritten baseline before claiming the single missing
   flag is the complete trigger.

4. The newer `SessionDB.get_resume_conversations()` path already requests
   `include_row_ids=True`, and the gateway's lease-wait reload path does too.
   The ACP live-restore path at `acp_adapter/session.py:555` still omits it.
   `gateway/session.py:4004` forwards a caller-controlled `include_row_ids`
   flag, so every live-replay caller must be checked explicitly; display,
   export, and diagnostic consumers may remain opt-out. The audit must cover
   every CLI, TUI, ACP, desktop, and gateway restore/copy path rather than
   assuming the two safe callers cover all entry points.

The stale visible result may be the persisted unfinished assistant row being
reintroduced, a late provider response racing the next invocation, or both.
The implementation must instrument/verify turn ownership so the fix does not
silently hide a provider-late-response race.

## Implementation review findings

- **P1 — required fix:** add `include_row_ids=True` to the CLI direct resume
  read at `hermes_cli/cli_agent_setup_mixin.py:434`.
- **P1 — sibling live-replay fix:** add the same durable identity to the ACP
  restore at `acp_adapter/session.py:555`.
- **P1 — reproduction isolation still open:** add temporary diagnostic
  counters/logging around `_flush_messages_to_session_db_unlocked()` for the
  session id, turn id, `conversation_history is None`, number of identity
  matches, number of `_row_id` matches, and scan-prefix reset. The retained
  probe rows have identical historical timestamps in multiple appended
  batches, which is consistent with a rebuilt/empty baseline or a second
  process flush, but does not distinguish those mechanisms by itself.
- **P1 — full timeout-race acceptance remains:** the durable session-turn lease
  fences SQLite transcript appends while the holder is active, and the
  provider worker itself does not directly append transcript rows. A narrow
  delayed-provider test now proves that a worker returning after A's timeout
  cannot change B's response slot. The remaining acceptance is the full
  two-process test asserting both response ownership and DB row ownership.
- **P2 — caller audit:** `gateway/session.py:4004` is safe only when its live
  replay callers request row ids. `hermes_cli/context_switch_guard.py:190`,
  `hermes_cli/sessions_cmd.py:600`, and `agent/trace_upload.py:342` appear to
  be diagnostic/display paths and should not be changed without confirming
  that their returned objects are later used as a writable live baseline.

## Required implementation scope

- Preserve durable row identity through every process-boundary resume path.
- Make normal close, timeout, interrupt, and retry persistence idempotent.
- Keep explicit rewrite/compaction/rewind paths able to clear `_row_id` and
  intentionally create replacement rows.
- Never delete or rewrite existing live production transcript rows as part of
  the fix. Recovery or cleanup of the retained synthetic evidence requires a
  separate explicit operation.

## Required cross-review questions

1. Is `include_row_ids=True` at the CLI restore site sufficient, or are there
   other restore/copy paths that discard `_row_id` before the flush?
2. Does the close-path safety net preserve the loaded row identity when the
   agent process is interrupted between input staging and turn completion?
3. Do alternation repair, compaction rotation, in-place compaction, rewind,
   and branch/fork semantics intentionally clear or preserve row identity?
4. Can a timed-out provider response still append after the caller has moved
   to the next turn? If so, what turn/session lease or cancellation fence
   prevents that late response from becoming visible in the next response?
5. Are the regression tests exercising two real processes with a temporary
   `HERMES_HOME`, rather than only testing one in-memory agent instance?

## Acceptance tests

- Deterministic persistence unit test: restored rows carry `_row_id` and are
  never appended again; a new user/assistant pair is appended exactly once.
- Direct CLI resume test: exercise `_init_agent()`'s quiet/single-query path,
  then assert the exact arguments and row identities passed into the first
  turn flush; include a variant where the flush baseline is rebuilt or absent
  so the reproduction trigger is not hidden by an in-memory identity match.
- Two-process CLI resume test using temporary `HERMES_HOME`:
  - normal completed turn;
  - interrupted/timeout turn;
  - next turn with a unique nonce;
  - no prior nonce or stale assistant row is returned as the current result.
- Ten-call synthetic regression matching the reproduction above, with a
  machine-checkable invariant: each logical user nonce has one active user row
  and each completed assistant turn has one active assistant row.
- Delayed-provider race test: process A is interrupted/times out while its
  provider worker is held; process B starts the next turn; release A's worker
  afterwards and assert no A response appears in B's output or durable rows.
- Existing replay, gateway persistence, compaction, Telegram batching/media,
  and agent regression suites remain green.
- Independent implementation review passes before merge.
- Deployment verification must compare the release marker, process cwd,
  `PYTHONPATH`, `VIRTUAL_ENV`, and `HERMES_RELEASE_SHA`; source checkout
  cleanliness alone is not deployment evidence.

## Current state

- Implemented, not yet committed: the direct CLI resume path now restores
  durable `_row_id` values with `include_row_ids=True`.
- Implemented, not yet committed: ACP live restore now preserves `_row_id`,
  and ACP forked histories clear parent `_row_id`/`_db_persisted` before the
  child writes fresh rows.
- Added regression coverage for the exact CLI call arguments, cold ACP
  restore identity, and parent/child fork identity separation.
- Targeted validation: `67 passed in 12.53s`; the independent second review
  also ran the relevant ACP/CLI/dedup/lease/server subset (`48 passed in
  8.92s`).
- Delayed-provider narrow race test: `1 passed`; full two-process SessionDB
  acceptance test: `1 passed`, repeated `10/10`; full request-client race file:
  `7 passed, 1 skipped`. The two real child processes share a temporary
  `HERMES_HOME`: A's stale lease append is rejected after B completes, B
  receives only its own response, and the final DB has no A late-assistant row.
- Added ten-process cold-resume synthetic regression: `1 passed`, repeated in
  three independent pytest invocations. Each child models one quiet CLI
  process boundary, restores row ids, appends one unique `SYNTH-N-8f3c` pair,
  and the final DB invariant is exactly 20 rows with 20 unique row ids and one
  user/assistant pair per nonce.
- Related focused suite after the new test: `38 passed, 1 skipped`.
  `git diff --check` is clean. The
  repository's `venv/bin/ruff` executable is not present, so lint was not run.
- No commit, push, or deployment has been made for this ticket.
- The working tree is intentionally dirty with only this ticket's code, tests,
  and plan changes; the live gateway remained healthy on release
  `d0977abe2d`.

## Independent cross-review disposition

The first read-only review identified a real ACP fork defect: copied parent
row identities could cause a failed child write to look already durable. The
implementation now strips those identities before child persistence, and the
new fork test verifies that the child receives fresh, disjoint row ids.

The second read-only review confirmed the CLI/ACP restore changes, the fork
identity reset, and the relevant targeted tests. No additional correctness
change was requested.

The follow-up read-only review of the delayed-provider test also passed: it
found no introduced correctness issue and confirmed the targeted ACP, CLI, and
request-race tests. The full two-process test was then rerun independently and
passed `10/10` repetitions. A further read-only cross-review of the new
ten-process regression and the two-process test passed; it found no introduced
correctness issue.

Disposition: **implementation cross-review passed; timeout-race and synthetic
ten-call acceptance passed; ticket remains open only for deployment
verification**. This change is not yet a deployment claim.
