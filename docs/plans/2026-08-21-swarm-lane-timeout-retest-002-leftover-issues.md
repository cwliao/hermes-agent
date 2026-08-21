# SWARM-LANE-TIMEOUT-RETEST-002-LEFTOVERS

Status: evidence-gathering only. No code or config changed to produce
this document. Consolidates the leftover, unresolved findings from the
live `autumn-jokes-v6` Telegram swarm test
(`docs/plans/2026-08-21-swarm-lane-timeout-retest-002-findings.md`),
one of which (the notify-subscription gap) was investigated further
after that document was written and PR #98 merged. Needs cross-review,
per this effort's established practice -- especially the new finding
below, since its root cause is not yet nailed down.

## 1. `worker_quorum` not actually passed despite an explicit user request (documented, unfixed)

Already covered in `2026-08-21-swarm-lane-timeout-retest-002-findings.md`,
Finding 2. Restated here for completeness: the user explicitly asked
the bot, in the same Telegram message that triggered the `v6` swarm, to
set `worker_quorum`. The resulting swarm's topology blackboard shows
`"worker_quorum": null`. Not a `kanban_swarm` bug -- the parameter
works correctly once actually supplied (re-confirmed multiple times
this session, including live on `v6` itself via a manual
`kb.archive_task` standing in for what the automatic excuse mechanism
would have done). A model/prompting gap: the calling model did not
reliably translate an explicit user instruction into the corresponding
tool argument, in a very long (11-day, 300+ message) single Telegram
conversation.

## 2. Content drifts across each LLM handoff (documented, unfixed)

Already covered in the same document, Finding 3. Each joke's text
changed at every hop (worker's own blackboard post -> verifier's
restatement -> synthesizer's restatement) because each stage
re-summarizes the prior stage's output in its own words rather than
quoting it verbatim. A future prompt change (explicitly instructing
verifier/synthesizer to quote blackboard content verbatim) is the
likely fix, not attempted here.

## 3. NEW: synthesizer completion never triggers a Telegram notification, despite the tool reporting `"subscribed": true`

**Not previously documented.** Discovered while manually completing the
stuck `v6` synthesizer task after PR #98's fix (see the parent
document) -- intending to let the completion's normal notification path
deliver the result to Telegram, then noticing no message arrived.

**What's confirmed:**

- The `kanban_swarm` tool call that created the `v6` swarm returned
  `{"ok": true, "subscribed": true, "root_id": "t_0b661c55", ...,
  "synthesizer_id": "t_93d3d51e"}` -- read directly from the persisted
  tool-call result in `state.db`, not the bot's narration.
- `SELECT * FROM kanban_notify_subs WHERE task_id IN ('t_0b661c55',
  't_93d3d51e', 't_eea54c22')` against the live `~/.hermes/kanban.db`
  returns **zero rows** for the root, synthesizer, or verifier of this
  swarm. `SELECT COUNT(*) FROM kanban_notify_subs` for the whole board
  is 5 -- all five belong to *other*, earlier swarms from the same
  Telegram session (confirmed by cross-referencing task ids against
  their own creation timestamps), none to `v6`.
- So the tool's own `subscribed: true` return value does not match
  the database's actual state for this specific swarm. This is a real
  discrepancy, not a misread on this investigator's part -- re-queried
  twice against the correct board (`~/.hermes/kanban.db`, the default
  board this Telegram session's swarms land in, not the `kanban` named
  board used for this session's own manual CLI testing throughout the
  day -- see the earlier `commit push deploy merge clean dirty tree`
  turn's board-mixup near-miss for why that distinction matters on this
  host).

**Root cause: not yet determined.** Two hypotheses, neither confirmed:

1. `_maybe_auto_subscribe`'s `add_notify_sub` call
   (`hermes_cli/kanban_db.py`) runs inside its own `write_txn(conn)`
   block, which should commit independently of whatever the rest of
   `create_swarm`'s call does. If the subscription row genuinely
   committed at creation time, something else must have removed it
   afterward -- no known cleanup/gc job was found to specifically prune
   `kanban_notify_subs` rows, but this wasn't exhaustively ruled out
   either.
2. Alternatively, `_maybe_auto_subscribe` could have silently returned
   `False`-that-got-reported-as-`True` due to a bug in how `_handle_swarm`
   surfaces the `subscribed` field, or the write could have failed
   inside `add_notify_sub` in a way that wasn't logged (the function has
   no explicit error path of its own -- errors would only surface via
   the caller's `except Exception` in `_maybe_auto_subscribe`, and no
   `"_maybe_auto_subscribe failed"` WARNING line appears anywhere in
   `agent.log` around this swarm's creation time, which argues against
   a raised exception but doesn't rule out a silent no-op).

**What was NOT investigated, for a future session to pick up:**

- Whether this reproduces reliably (create a fresh swarm right now,
  check `kanban_notify_subs` immediately after creation before any
  other activity, to rule out a later removal versus a write that never
  happened) versus this specific instance was one-off.
- Whether `INSERT OR IGNORE INTO kanban_notify_subs` could have hit a
  primary-key conflict for a reason not yet identified (the primary key
  is `(task_id, platform, chat_id, thread_id)`; `task_id` here is a
  freshly-created, definitely-unique id, so a conflict seems unlikely
  but wasn't directly ruled out by inspecting `thread_id`/`chat_id`
  coercion behavior in `_maybe_auto_subscribe` versus what actually
  landed, if anything, in the table).
- Whether `kanban.auto_subscribe_on_create` in `config.yaml` was
  perhaps toggled or raced by something else mid-session (checked: not
  present in `config.yaml`, so it defaults to `True` -- but a runtime
  config reload race wasn't checked).

## Practical impact

Without a working notify-subscription for the synthesizer, even a
swarm that completes perfectly end-to-end (as `v6` eventually did,
after PR #98's fix and a manual assist past the completion-loop) still
never reaches the user automatically over Telegram -- the exact
symptom this whole multi-day investigation (`SWARM-CLAUDE-GROK-LANE-
TIMEOUT-RECURRENCE-001` through `SWARM-PARTIAL-QUORUM-001`) set out to
fix, now potentially reintroduced by a different, unrelated mechanism.
This is the single highest-priority item left in this investigation
chain -- everything else fixed so far (timeouts, Tirith, blackboard
instructions, quorum, the completion-loop) is necessary but not
sufficient if the final delivery step itself has a silent gap.

## Process note

Consistent with this whole investigation's recurring lesson: verify
every "it worked" signal (a tool's own return value, a worker's
self-report, a bot's narration) against the actual database state
before trusting it. The `subscribed: true` return value here looked
exactly as trustworthy as any other successful tool result, and was
still wrong.
