# A `kanban_create` call issued just before `kanban_swarm` in the same turn leaves an orphan task that duplicates swarm work

## Problem

Discovered live during the regression test of
`docs/plans/2026-08-27-verifier-completion-metadata-gate-001.md`'s fix
(commit `dcc827a3d5`, deployed 2026-08-27). In coordinator session
`20260827_194622_dbfcef1a`, the user asked (single Telegram message) for a
4-lane `kanban_swarm` comparing four sorting algorithms. The model's tool
sequence for that one user turn was:

1. `kanban_show(task_id=null)` -> error (`task_id is required`)
2. `kanban_create(title="Sorting algorithms complexity comparison and demo",
   body="Parent task to coordinate creation of four child tasks...")` ->
   creates `t_1d9d6029`, status `ready`, **no parent**
3. `kanban_swarm(goal="Compare time and space complexities of...")` ->
   succeeds, creates the real swarm (root `t_a68efd71`, 4 workers incl.
   `t_6f377701` "Quicksort analysis and demo", verifier `t_2b690ae5`,
   synthesizer `t_cf6842a2`)

Step 2's task was never referenced again by the model and never
cancelled/linked into the swarm it structurally intended to describe. It
sat as an ordinary `ready` task with no parent, so the dispatcher picked it
up independently (`hermes_cli/kanban_db.py`'s `dispatch_once()`) and routed
it to its own worker session (`20260827_194841_ec76c5`, ~30s after
creation). That worker read the body ("Parent task to coordinate creation
of four child tasks...") and, following it literally, called
`kanban_create(title="Quicksort analysis and demo", parents=["t_1d9d6029"])`
-> `t_b61a4cc7`. That title and topic exactly duplicates the real swarm's
own `t_6f377701` lane, which was already running/completing concurrently
under an entirely separate task graph. Both ran to completion
independently -- wasted worker/GPU time on the shared host, and two
divergent "Quicksort analysis and demo" outputs existed on the board with
no relationship to each other.

Confirmed via direct read of `/home/cwliao/.hermes/state.db`'s `messages`
table (session ids and timestamps above) -- not a guess.

## Root cause

This is a **model tool-selection / cleanup gap**, not a dispatcher or
decompose-subsystem defect (contrast with today's fix #6, which was a real
code defect in the triage/auto-decompose path). The coordinator model:

- Attempted to manually stage a "swarm-shaped" task via plain
  `kanban_create` before discovering/using the actual `kanban_swarm` tool
  for the same request.
- Never cancelled, blocked, or linked the stray task after the real
  `kanban_swarm` call succeeded moments later in the *same turn*.

Nothing in the current guidance or guard layer catches this shape:

- `docs/plans/2026-08-27-duplicate-swarm-creation-guard-001.md`'s guard
  (commit `90bf701e8f`) only fires on a **second** `kanban_swarm` call when
  a non-terminal swarm already exists for the session -- here there is only
  one `kanban_swarm` call; the extra artifact is a plain `kanban_create`
  that *precedes* it, a different shape entirely.
- `agent/prompt_builder.py`'s `KANBAN_GUIDANCE` has soft language against
  "workaround tasks" but nothing tool-schema-level discourages using
  `kanban_create` to hand-roll swarm coordination, and nothing detects or
  cleans up an orphan afterward.
- The dispatcher (`dispatch_once()`) has no concept of "this ready task was
  created in the same turn/session as a swarm call moments later" -- it
  just dispatches anything `ready` with satisfied dependencies.

## Proposed fix (two independent, additive layers -- for reviewer sign-off)

**Layer A -- tool-description guidance (cheap, low-risk, prompt-level).**
Strengthen the `kanban_create` and/or `kanban_swarm` tool schema
descriptions (`tools/kanban_tools.py`'s `KANBAN_CREATE_SCHEMA` /
`KANBAN_SWARM_SCHEMA`, wherever the actual description strings live) with
an explicit negative instruction: do not hand-build a "coordinator" task
via `kanban_create` in order to manually orchestrate multiple child tasks
that compare/parallelize the same kind of work -- call `kanban_swarm`
directly for that. This does not guarantee compliance (it's a soft
guidance layer, same category as the existing `KANBAN_GUIDANCE` text) but
costs nothing and directly targets the observed failure mode.

**Layer B -- post-swarm orphan sweep (hard guard, needs design care).**
When `kanban_swarm`'s handler (`_handle_swarm` in `tools/kanban_tools.py`,
calling `hermes_cli.kanban_swarm.create_swarm()`) succeeds, look back at
tasks created by the *same session* within a short trailing window (e.g.
the current turn, or a fixed lookback such as 120s -- reviewers should
pick the bound) that are:
  - not part of the swarm just created (not the root/worker/verifier/
    synthesizer ids, no `parents` overlapping them),
  - still in a non-terminal, undispatched-or-early state (`ready` is the
    concrete case seen; consider whether `todo` also applies),
  - have no children yet (to avoid interfering with a task that's already
    legitimately in progress).

For each match, do **not** delete or silently cancel it (a task the model
created could still be a legitimate, unrelated request bundled into the
same turn -- deletion risks destroying real user-requested work). Instead
call `kanban_block` on it with a clear, human-readable reason (mirrors the
existing `record_swarm_stall_diagnostic` pattern from
`docs/plans/2026-08-27-verifier-completion-metadata-gate-001.md`, commit
`dcc827a3d5`) such as: "auto-blocked: a kanban_swarm call succeeded in the
same session shortly after this task was created; if this task is
unrelated to the swarm, unblock it explicitly." This surfaces the
situation as a visible, human-reviewable `blocked` task instead of letting
the dispatcher silently run it to duplicate/wasted completion.

Reviewers: please weigh in on (1) the lookback window and exact match
criteria for Layer B -- want to avoid false positives blocking genuinely
unrelated legitimate tasks the user bundled in the same message; (2)
whether Layer B belongs inside `_handle_swarm` itself vs. as a best-effort
post-commit hook so a failure in the sweep can never abort a successful
swarm creation; (3) whether Layer A alone might be judged sufficient for
V1 given Layer B's higher false-positive risk, deferring Layer B to a
follow-up if reviewers are not confident in the match criteria.

## Non-goals

- Not touching the dispatcher's core `dispatch_once()` ready-task pickup
  logic -- the orphan task genuinely was `ready` with satisfied
  dependencies; dispatching it was correct given the (wrong) state it was
  left in. The fix belongs at creation-time/swarm-time, not dispatch-time.
- Not building retroactive detection for already-existing orphan tasks on
  the board today -- this is a forward-looking guard. (`t_1d9d6029` /
  `t_b61a4cc7` from the incident have already run to completion; no cleanup
  needed there.)
- Not a fuzzy title/goal-similarity duplicate detector across the whole
  board -- scoped narrowly to "created in the same session, shortly before
  a kanban_swarm call that just succeeded," which is a structural, not
  semantic, signal.

## Verification plan (post-fix)

- New test: a plain `kanban_create` immediately followed by a successful
  `kanban_swarm` call in the same session results in the plain task being
  auto-blocked with the expected reason, and is excluded from dispatcher
  pickup while blocked.
- New test: a plain `kanban_create` for a genuinely unrelated task,
  followed by `kanban_swarm`, if within the match window, is also blocked
  -- document this as an accepted false-positive tradeoff (human can
  unblock) unless reviewers propose a tighter signal.
- Regression test: `kanban_swarm` called with no other tasks created
  nearby behaves identically to today (no behavior change for the common
  case).
- Regression test: existing duplicate-swarm guard
  (`docs/plans/2026-08-27-duplicate-swarm-creation-guard-001.md`) test
  suite still passes unmodified.
- Live regression: re-run a Telegram-driven 4-lane swarm test and confirm
  no stray orphan task appears on the board afterward.
