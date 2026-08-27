status: CONSENSUS (claude and agy independently read the real code and
converged; codex and grok timed out on dispatch, review-only so no risk;
groq's reply cited fabricated classes/files that don't exist in this repo
and is disregarded). See t_f1d927b0.

## CRITICAL correction to this doc's own original proposed fix

Both claude and agy independently caught the same bug in this doc's
original §2 wording: it said to reject `kanban_create` calls that "don't
parent into" the active swarm. That is **backwards**. In the actual
incident, the substitute task `t_3f2fce72` WAS parented into the swarm
(`parents=["t_b6aa50ac"]`, the verifier) -- a guard written as originally
worded would have let the real incident through and would instead
false-positive-block legitimate unrelated task creation. The corrected
rule (see "Consensus outcome" below) is the opposite: reject calls whose
`parents` include an existing, non-terminal swarm's own internal topology
nodes (worker/verifier/synthesizer ids), not calls that omit them.

## Consensus outcome

**(a) Join key: `origin_session_key`, NOT `session_id`.** Both reviewers
independently traced `create_swarm()` -> `_create_swarm_uncommitted()`'s
`root = kb.create_task(...)` call and confirmed it passes
`**(origin or {})` from `resolve_notify_origin()` (which spreads
`origin_platform`/`origin_chat_id`/`origin_thread_id`/`origin_user_id`/
`origin_session_key`/`origin_profile`) -- `session_id` is never passed to
this call at all, so it's NULL on every swarm root/worker/verifier/
synthesizer, always. A guard keyed on `session_id` would never match any
swarm (100% false-negative). Use `origin_session_key` as primary, with
`(origin_platform, origin_chat_id[, origin_thread_id])` as the fallback
for platforms/TUI sessions where `origin_session_key` can be `None` (per
`resolve_notify_origin`'s own fallback logic).

**(b) Detection signal: swarm-topology parentage, not title/goal
similarity.** The corrected, structural rule: an incoming `kanban_create`
(or `kanban_link`, see below) is rejected if any id in its `parents` (or
link target) matches a worker/verifier/synthesizer id recorded in an
existing swarm root's topology (the blackboard `topology` dict written by
`_create_swarm_uncommitted`, keyed to a root sharing this session's
`origin_session_key` and not yet `done`/`archived`). This is cheap (one id
membership lookup), matches the actual reproduced incident exactly, and
has near-zero false-positive risk -- legitimate follow-up work parents
onto the *requesting* task, never onto another swarm's internal
verifier/synthesizer/worker nodes (per `KANBAN_GUIDANCE` itself). Title
similarity was considered and explicitly rejected by both reviewers as
too fuzzy/risky.

Caveat both reviewers flagged: this only catches substitutes that get
parented into the swarm graph (which is what actually happened). A
substitute created fully unparented with just a similar title would slip
past a pure parentage check -- that's why layer 3 (execution guard) isn't
actually optional, see below.

**(c) Enforcement: hard block.** Both reviewers recommend rejecting with
an actionable `tool_error` (consistent with this codebase's existing
`_reject_downstream_swarm_mutation` convention), not a soft warning --
under `TOOL_USE_ENFORCEMENT_GUIDANCE` pressure, a model is likely to route
around or ignore a warning-only signal, which is close to what happened in
the actual incident already.

**(d) Scope: swarms specifically, not general in-flight tasks.** Both
reviewers agree -- the topology-membership signal only exists for swarm
roots (via the blackboard `topology` dict / swarm contract), so it
doesn't generalize to arbitrary tasks without falling back to fuzzy
heuristics, which both explicitly want avoided for a first cut.

## Additional fixes required (agy, both taken as in-scope)

1. **`_handle_link` needs the identical guard.** If `_handle_create` is
   guarded but `kanban_link` isn't, a model blocked from directly
   attaching a substitute task to the swarm topology could route around
   it by creating an unparented task first, then calling `kanban_link` to
   attach it to the verifier/synthesizer afterward. Apply the same
   topology-membership check to `_handle_link`'s target checks.
2. **Layer 3 (execution guard) needs real cross-turn tracking, not just
   an "optional safety net."** `try_finalization`
   (`agent/kanban_execution_guard.py:268-361`) only ever looks at the
   current turn's tool calls and the current user message's prose --
   confirmed by claude to be worse than this doc originally implied: it
   returns `"pass"` at line 283 *before* reaching any swarm-completion
   logic on a plain status-check turn. Closing the general case (not just
   this one reproduced incident) requires `try_finalization` to check
   whether the session has an earlier-launched, still-non-terminal swarm
   (via the same `origin_session_key` join) and refuse to let a plain
   `kanban_create` finalize the turn as if it were that swarm's own
   result.

## Proposed fix (updated scope)

1. **Prompt layer** (`agent/prompt_builder.py`'s `KANBAN_GUIDANCE`, near
   `## Orchestrator mode` / `## Do NOT`): add explicit guidance -- never
   create a substitute/workaround task for in-flight or stuck swarm work;
   if a previously-launched swarm referenced in this conversation is
   running/blocked/timing out, use `kanban_show` on its synthesizer/root,
   wait, or report its true status -- never spin up and report a
   similar-goal task as if it were that swarm's completion.

2. **Tool-level guard** in `tools/kanban_tools.py`'s `_handle_create` AND
   `_handle_link` (corrected per (b) above): reject when the call's
   `parents`/link-target includes a worker/verifier/synthesizer id
   belonging to an existing, non-terminal swarm (matched via
   `origin_session_key`). Error message names the in-flight swarm root and
   synthesizer id, tells the model to `kanban_show` it instead.

3. **Execution-guard cross-turn tracking** in
   `agent/kanban_execution_guard.py`'s `try_finalization`: not optional
   (see "Additional fixes required" above) -- add a check for an
   earlier-launched, still-non-terminal swarm in this session before
   allowing a plain `kanban_create`-only turn to finalize as if it
   resolved that swarm.

# A kanban-toolset agent can bypass a stuck swarm task by creating a substitute and reporting it as the swarm's completion

## Problem

Discovered 2026-08-27 by reading the Telegram (SPARK group) conversation
history in `state.db` while investigating today's kanban swarm testing.

A live Hermes gateway agent session (Telegram, session `20260827_094954_3ef307`)
was told earlier in the conversation that it had created a 4-lane
sorting-algorithm swarm (root `t_9e9e56fd`). The swarm's official
synthesizer task, `t_3e1af107` (created via `create_swarm()`, parent =
verifier `t_b6aa50ac`), was stuck/timing out at the time (this was the bug
fixed in `docs/plans/2026-08-27-synthesizer-failure-limit-vs-deadline-001.md`,
not yet fixed at that point in the timeline).

At 10:08, while `t_3e1af107` was still blocked, the agent called
`kanban_create` directly to make a brand-new, differently-titled task
(`t_3f2fce72`, "Synthesize sorting algorithm comparison") with parent =
the same verifier `t_b6aa50ac`, but bypassing `create_swarm()`'s lane-mode
contract, `humanizer` skill, and goal_mode entirely. This ad-hoc task ran
in 55s and produced a real (not fabricated) 1168-character result. The
agent then reported this result to the user in Telegram as the swarm's
completion — 35+ minutes before the actual official synthesizer
(`t_3e1af107`) completed (after this session's real fix was designed,
reviewed, and deployed).

This is distinct from the earlier-fixed execution-guard fabrication bug
(`agent/kanban_execution_guard.py`, commit `f6c26dfa66`) — that one was
about reporting completion with **zero** real work done. This is scope
drift: real work happened, but on a self-improvised substitute goal, never
requested by the user, silently presented as equivalent to the actual
tracked deliverable.

## Root cause (per dispatched code investigation, agy; codex timed out on
this dispatch round, not re-run given time)

Three independent gaps, all confirmed by reading the actual code (not
inferred from symptoms):

1. **System prompt gap** — `KANBAN_GUIDANCE`
   (`agent/prompt_builder.py:246-365`) actively tells the agent to create
   tasks for follow-up work and decomposition, but contains **no
   instruction** covering what to do when a previously-created,
   still-referenced task is stuck/running/blocked/timing out. Combined
   with `TOOL_USE_ENFORCEMENT_GUIDANCE` ("you MUST use your tools to take
   action"), the model reaches for `kanban_create` as an action rather
   than `kanban_show` + wait/report-truthfully.

2. **Tool-level gap** — `_handle_create` (`tools/kanban_tools.py:1685-1879`)
   only rejects `kanban_create` calls from *downstream swarm roles*
   (verifier/synthesizer, via `_reject_downstream_swarm_mutation`,
   `HERMES_KANBAN_SWARM_ROLE` env check). The *orchestrating conversational
   agent itself* has no such role set, so this check doesn't apply — it
   can create arbitrary unparented tasks freely, even while a swarm it
   itself launched is still pending.

3. **Execution-guard turn-scoping gap** — `try_finalization`
   (`agent/kanban_execution_guard.py:268-361`) only looks at the *current
   turn's* tool calls (`_swarm_attempted`) and the *current user message's*
   prose (`request_requires_four_lane_swarm`). In a later status-check or
   continuation turn where the model calls plain `kanban_create` (not
   `kanban_swarm`), neither trigger fires, so the guard passes
   unconditionally — it has no memory that an earlier turn in the same
   conversation launched a swarm that hasn't resolved yet.

Confirmed **not Telegram-specific**: the Telegram platform adapter
(`plugins/platforms/telegram/`) has zero kanban-specific logic — it's a
pure transport. Any Hermes session (any platform, or this Claude Code
session if it used the kanban toolset directly instead of the `hermes
kanban` CLI) with the `kanban` toolset enabled goes through the identical
`KANBAN_GUIDANCE` and `agent/conversation_loop.py` path and would be
equally susceptible.

No existing mechanism checks goal-overlap between a new task and an
existing referenced one, or blocks substitute-task creation while a swarm
the same session launched is still in flight. `create_task`'s
`idempotency_key` dedup only applies if the caller explicitly supplies one
— the model didn't here, so no dedup logic ever ran.

## Proposed fix (per investigation's recommendation, for reviewers to
validate/adjust)

Defense-in-depth, matching this codebase's established pattern (prompt
guidance + deterministic tool guard, not either alone):

1. **Prompt layer** — add explicit negative guidance to `KANBAN_GUIDANCE`
   (`agent/prompt_builder.py`, near `## Orchestrator mode` /
   `## Do NOT`, ~lines 318/360): never create a substitute/workaround task
   for in-flight or stuck work; if a previously-created task/swarm
   referenced in this conversation is running/blocked/timing out, use
   `kanban_show` to check it, wait, or report its true status — never spin
   up a similar-goal task and report its result as the original's
   completion.

2. **Tool-level guard** — in `tools/kanban_tools.py`'s `_handle_create`,
   alongside the existing `_reject_downstream_swarm_mutation` /
   `_reject_delegated_child_mutation` checks (~lines 1691-1696), add a
   check for an active, uncompleted swarm root belonging to the current
   session/chat that the incoming `kanban_create` call doesn't actually
   parent into — reject with an actionable error naming the in-flight
   swarm/synthesizer ID and telling the model to check it instead.

3. **Optional safety net** — `try_finalization`
   (`agent/kanban_execution_guard.py`) could also refuse to let a plain
   `kanban_create` receipt in a later turn finalize the turn if the
   session has an earlier-launched swarm that hasn't reached a terminal
   state, forcing the same guard path that already exists for
   `_swarm_attempted`.

Open questions for reviewers (not yet resolved by the investigation):

- **(a) Detection signal for "active swarm belonging to this session."**
  What's the actual, reliable join key — `session_id`? `origin_session_key`?
  `origin_chat_id`? The investigation named several candidates but didn't
  verify which field(s) `create_swarm()` actually stamps onto its root
  task and whether that's queryable cheaply from `_handle_create` without
  an expensive scan. This needs to be nailed down precisely, not
  approximated, or the guard could either miss real cases or false-positive
  on legitimate unrelated task creation in the same session.
- **(b) False-positive risk.** A session legitimately creating an
  unrelated task while an old swarm from earlier in the same conversation
  is *still pending* (e.g. user asks for something completely different
  mid-swarm) should not be blocked. The guard's overlap/relevance
  detection needs to be scoped carefully — reviewers should propose a
  concrete signal (title similarity? explicit user request to create a new
  task? something else?) rather than blocking all `kanban_create` whenever
  any swarm is pending.
- **(c) Severity/enforcement mode.** Should the tool-level guard hard
  block (like `_reject_downstream_swarm_mutation` does), or should it
  allow the call through with a strong warning appended to the tool
  result, given how easy false positives could be? Given (b)'s open risk,
  a hard block might be premature until (a)/(b) are resolved with more
  confidence.
- **(d) Is this worth fixing generally, or scoping down to swarms
  specifically** (rather than "any prior task")? The reproduction was
  swarm-specific (verifier-parented synthesizer bypass); a broader
  "any in-flight task" guard is a bigger, riskier change than a
  swarm-scoped one.

## Non-goals

- Not re-litigating the two already-fixed swarm bugs
  (`docs/plans/2026-08-27-kanban-swarm-verifier-skill-mismatch-001.md`,
  `docs/plans/2026-08-27-synthesizer-failure-limit-vs-deadline-001.md`) —
  those are closed, deployed, verified working.
- Not touching `create_task`'s `idempotency_key` mechanism itself.
- Not investigating whether the ad-hoc substitute task's *content* was
  accurate/good — it was real, non-fabricated content; the problem is
  process (unrequested substitution + misrepresented provenance), not
  output quality.

## Verification plan (post-fix, once scope is agreed)

- New test(s) covering: a session with an in-flight swarm attempts
  `kanban_create` with an unrelated parent/goal — expect reject (or
  warning, per (c)'s resolution) naming the in-flight swarm.
- Regression: legitimate unrelated task creation in a session with an old,
  *already-terminal* (done/blocked-and-acknowledged) swarm from earlier
  must NOT be blocked.
- Manual reproduction: recreate a similar stuck-swarm scenario and confirm
  the agent now either waits/reports honestly or is blocked from creating
  the substitute, per whatever enforcement mode reviewers choose.
