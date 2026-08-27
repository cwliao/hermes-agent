status: CONSENSUS (claude and agy independently read the real code and
converged; codex and grok timed out on dispatch, review-only so no risk;
groq's reply cited entirely fabricated files/classes -- SQLAlchemy-style
`agents/kanban_swarm.py`, `SwarmSynthesizer`, `db.scoped_session()` -- none
of which exist in this repo, sqlite3-based -- and is disregarded). See
t_103fe12f.

## Consensus outcome

All of the investigation's factual claims (handler location, origin
resolution, root-vs-synthesizer distinction, the tools-vs-agent connection
concern) were independently re-verified by both real reviewers by reading
the cited code directly -- confirmed accurate.

**(1) V1 scope: confirmed correct, with one addition.** Hard-block
`kanban_swarm` only, scoped to session, as proposed. **New finding
(claude): also scope by `board`.** `_find_active_swarms_for_session()`
today has no board filter at all -- queries only by `origin_session_key`
or `platform+chat_id`. Since `_handle_swarm` supports a per-call `board`
override specifically so a Telegram-side agent can target a non-default
board, an active swarm on board A would wrongly block a legitimate new
swarm on board B in the same chat. The new shared helper (see (5)) must
thread the resolved `board` through and filter by it, not just by
session. Also: spec the rejection message to name the existing root/
synthesizer id and status and explicitly tell the model not to retry with
a workaround shape (mirrors the phrasing already used by commit
`8a291e8b03`'s guards).

**(2) Root-vs-synthesizer distinction: confirmed correct and necessary**
by both reviewers reading `create_swarm()`/`_activate_root_inline`
directly -- the root is flipped to `done` inside the same `write_txn`
that starts the swarm, so a root-status check would be a 100%
false-negative in practice, not just theoretically wrong.

**(3) Race condition: CLOSE IT IN V1, do not defer.** Both reviewers
independently reject deferring this. Cost is cheap -- SQLite's `write_txn`
already uses `BEGIN IMMEDIATE` (serializes writers), so re-running the
active-swarm check inside `create_swarm()`'s own write transaction, right
before inserting the root, costs one extra SELECT inside an already-open
transaction. Claude adds an important correction to the doc's framing:
the "two turns at the same instant" risk is not a contrived edge case --
this architecture already runs worker subprocesses concurrently under a
shared session/origin, so a live gateway turn and a dispatcher-spawned
worker deciding to call `kanban_swarm` around the same moment is a real
concurrent-process scenario already present in this codebase, not
speculative. Implement: preflight check in `_handle_swarm` (fast-path
rejection with a clear error) PLUS an atomic re-check inside
`create_swarm()`'s write transaction (correctness guarantee) -- defense
in depth, not either alone.

**(4) Reject model-fillable `allow_concurrent`: confirmed.** Both agree a
model-supplied flag proves nothing about user intent and could be set by
the same failure mode that caused this incident. If a real dual-swarm
need ever surfaces, the only trustworthy signal would be derived from the
raw current-turn user message content (the same pattern
`_current_turn_four_lane_goal()` already uses to bind a swarm goal to the
literal current turn rather than trusting the model's own argument) --
not a schema field. Not needed for V1.

**(5) Shared helper: confirmed real, not a style nitpick.** Both traced
the actual `kb.connect()` call inside `_find_active_swarms_for_session()`
(no board param) versus `_handle_swarm`'s `_connect(board=board)` --
calling the agent-side helper directly from `tools/kanban_tools.py` would
silently connect to the wrong board whenever the override is in play.
Factor a shared helper (in `hermes_cli/kanban_swarm.py` or
`hermes_cli/kanban_db.py`) that takes the caller's existing `conn` and the
resolved `origin`/`board`, used by both `tools/kanban_tools.py` and
`agent/kanban_execution_guard.py` (refactor the latter's existing
function to delegate to the new shared helper rather than duplicating the
query).

**(6) Additional findings taken as in-scope:**
- (agy) The existing pattern in both current call sites
  (`_reject_in_flight_swarm_topology_mutation` and
  `_find_active_swarms_for_session`) has a latent bug: if a synthesizer
  row is ever missing (`synth_row is None` -- deleted/corrupted graph),
  the current code does NOT treat it as terminal, meaning that swarm is
  considered "active forever" -- a permanent false-positive/deadlock risk
  already present in the deployed code, not new. Fix this in the new
  shared helper: treat a missing synthesizer row as non-active (log a
  warning), not as perpetually active.
- (claude) Spec the rejection message content explicitly in the
  implementation (see (1)).

## Updated verification plan

In addition to the original plan: test the board-scoping fix (an active
swarm on board A must not block a new swarm on board B in the same
session); test the `synth_row is None` handling doesn't perpetually block;
add a concurrency test for the atomic re-check inside `create_swarm()`'s
transaction (two near-simultaneous `kanban_swarm` calls under the same
session/board must result in exactly one swarm created and one rejected).

# `kanban_swarm` can create a fully independent duplicate swarm while one is already in flight

## Problem

Discovered live during the regression test of
`docs/plans/2026-08-27-telegram-agent-task-bypass-001.md`'s fix (commit
`8a291e8b03`, deployed 2026-08-27). While the original swarm's synthesizer
(root `t_a721d234`, synthesizer `t_2e233b34`) was in its second, legitimate,
in-budget retry attempt (not stuck -- genuinely slow, ~25 tok/s
single-stream generation on this shared GPU host), the live Telegram
gateway agent independently called `kanban_swarm` again with the identical
goal, creating an entirely separate, fully independent swarm (root
`t_e7cf6360`) -- its own fresh 4 workers, verifier, and synthesizer, with
no parent/child relationship to the original swarm's topology at all. The
user confirmed they did not send a second request.

Manually stopped: reclaimed and archived root `t_e7cf6360` and all 7 of
its children. Confirmed no orphaned worker processes remained. Real GPU
compute was wasted (4 duplicate workers ran to completion before being
stopped) on an already resource-constrained shared vLLM endpoint,
concurrently with the original swarm's own resource-constrained synthesis
attempt.

## Why the just-deployed fix (8a291e8b03) doesn't cover this

That fix's three layers all key off a new task's `parents`/link-target
referencing an *existing* swarm's topology node ids (worker/verifier/
synthesizer). A fresh `kanban_swarm` call creates its own brand-new root
with no such reference -- there is no structural id-overlap signal at all
for two independent swarm topologies. Confirmed via code investigation
(codex):

- `tools/kanban_tools.py`'s `_reject_in_flight_swarm_topology_mutation` is
  only called from `_handle_create` (line 1867) and `_handle_link` (line
  2421) -- never from `_handle_swarm` (line 2182), which is the actual
  handler for the `kanban_swarm` tool and calls
  `hermes_cli.kanban_swarm.create_swarm()` at line 2381.
- `agent/kanban_execution_guard.py`'s `_swarm_topology_mutation_attempted`
  has the identical blind spot -- it only inspects `kanban_create`/
  `kanban_link` tool call arguments, never `kanban_swarm`.
- `agent/prompt_builder.py`'s `KANBAN_GUIDANCE` addition ("never create a
  substitute or workaround task... never spin up a similar-goal task") is
  soft guidance only, arguably covers this in spirit, but has no hard
  enforcement -- and the model did it anyway.

## Key correctness detail (codex)

Detecting "is there a non-terminal swarm for this session" must NOT check
the swarm *root*'s status -- `create_swarm()` immediately marks the
planning root `done` so its workers can start (this is intentional,
documented behavior, not a bug). The actual "is this swarm still in
flight" signal is the **synthesizer's** status: read the root's topology
(`latest_blackboard(conn, root_id).get("topology")`), get
`synthesizer_id`, and treat the swarm as active unless the synthesizer's
status is in `TERMINAL_STATUSES = ("done", "archived")`
(`hermes_cli/kanban_db.py:12897`). This exactly matches the pattern
already used by the two guards from the prior fix -- reuse the *algorithm*,
but see the next section for why not the exact same function call.

## Origin resolution -- confirmed available in the handler

Traced the full path: `build_turn_context()` sets the current-turn
ContextVar before tool dispatch (`agent/turn_context.py:694`) ->
`resolve_notify_origin()` reads it (`gateway/session_context.py:524`) ->
`_handle_swarm` already resolves `origin` at line 2366, before calling
`create_swarm()` at line 2381 (which writes it onto the root,
`hermes_cli/kanban_swarm.py:890`; descendants inherit it via
`create_task()`, `hermes_cli/kanban_db.py:3722`). So the Telegram/TUI path
has origin available with no gap. A pure CLI/cron path with no session
context gets `origin = {}` and can't safely session-scope a block -- same
limitation the prior fix already has, not new.

Implementation note: don't call the agent-side `_find_active_swarms_for_session()`
directly from `tools/kanban_tools.py` -- it does its own `kb.connect()`,
which could ignore `_handle_swarm`'s per-call `board` override. Factor a
shared tools-side helper (or a small shared module both call into) that
accepts the handler's existing `conn` instead.

Also noted: `origin_session_key` has no dedicated DB index today (only
`idx_tasks_session_id` exists, and `session_id` is NULL on every swarm
node per the prior fix's investigation) -- the lookup is cheap logically
but not indexed. Not blocking for now given typical board size, but worth
a follow-up if this ever shows up as a real latency problem.

## Enforcement recommendation (codex)

**V1: hard-block `kanban_swarm` outright whenever any non-terminal swarm
already exists for this session** (via the synthesizer-status check
above) -- coarse, but matches this codebase's established preference
(from the prior fix's own design review) for hard blocks over soft
warnings, and scoped narrowly to `kanban_swarm` specifically, not general
`kanban_create` (ordinary unrelated task creation during an active swarm
must remain allowed -- already covered by an existing test,
`tests/tools/test_kanban_tools.py:2266`).

Explicitly reject an `allow_concurrent`-style boolean field the model
could set on the `kanban_swarm` call itself as "authorization" -- a
model-supplied flag proves nothing about user intent; the model could
set it to route around the guard exactly the way this incident happened
in the first place. Confirmed there's no existing signal anywhere
(`KANBAN_SWARM_SCHEMA`, `KANBAN_GUIDANCE`, `request_requires_four_lane_swarm`)
that reliably distinguishes "the user explicitly asked for a second,
genuinely-parallel swarm" from "the model decided on its own to retry."

Real dual-swarm use cases may exist (a user genuinely wanting two
independent research swarms at once), so "always block" isn't absolutely
correct in principle -- but this is expensive, multi-worker, shared-GPU
work with no existing concurrency contract in this codebase for multiple
swarms per session. V1 hard block is judged a reasonable risk tradeoff;
a real dual-swarm need can use a different session, or a future explicit
user-level opt-in (not a model-fillable field) if this ever becomes a
real requirement.

**Race condition flagged**: a plain "SELECT for active swarm, then call
create_swarm() if none found" check-then-create in the handler is not
atomic -- two turns in the same session entering concurrently could both
see zero and both proceed. If the invariant must be airtight, the
admission check needs to happen inside `create_swarm()`'s own write
transaction, not as a separate handler-level preflight. Reviewers should
weigh in on whether V1 needs this atomicity guarantee or whether the
practical risk (two turns in the same session calling kanban_swarm at
literally the same moment) is low enough to defer.

## Non-goals

- Not touching `_reject_in_flight_swarm_topology_mutation` or
  `_swarm_topology_mutation_attempted` (already correct for the case they
  cover -- the create/link-into-existing-topology case from
  `docs/plans/2026-08-27-telegram-agent-task-bypass-001.md`).
- Not building a goal-similarity/fuzzy-matching detector -- there is no
  structural signal for two independent swarms, and title/goal similarity
  was already explicitly rejected in the prior fix's review as too
  fuzzy/risky.
- Not inventing a model-fillable "allow concurrent swarms" authorization
  field -- rejected per the reasoning above.

## Verification plan (post-fix)

- New test(s): `kanban_swarm` is rejected when a non-terminal swarm
  (synthesizer not done/archived) already exists for the session; is
  allowed when the only prior swarm's synthesizer is done/archived; an
  ordinary unrelated `kanban_create` during an active swarm still passes
  (regression guard against re-broadening scope, mirrors
  `tests/tools/test_kanban_tools.py:2266`'s existing case).
- Confirm the root-vs-synthesizer terminal-status distinction is actually
  tested (a test using a swarm where the root is `done` but the
  synthesizer isn't, verifying it's still detected as active).
- If the race-condition question above resolves toward needing atomicity,
  add a concurrency test; otherwise document the accepted risk explicitly
  in the code comment.
