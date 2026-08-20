# GATE8-SWARM-COMPLETED-VERIFIER-RECOVERY-AND-DELIVERY-GAP-001

Status: implemented, pending cross-review. See "Implementation" below.

## Context

After `WORKER-TIMEOUT-CONTENTION-001` (default `kanban.max_in_progress=3`) was
merged and deployed to `55-0940189-03` (release
`v2026.8.20-worker-concurrency-limit-36671c8e70`, `HERMES_RELEASE_SHA` verified
via `/proc/<pid>/environ`), the user re-sent the same four-lane swarm request
through Telegram that had previously always failed to reach gate 8. Tenant
`gate8-real`, root task `t_389e2a97`.

**The concurrency fix worked as intended.** All four workers (native, claude,
grok, agy) reached `done`; none hit `blocked` or `timed_out`. Dispatcher-level
observation during the run (polled every 15-20s against `hermes_cli.kanban_db`
directly) showed `running` held at exactly 3 concurrent tasks while 2+ tasks
sat `ready`, confirming the cap is live and enforced -- this is the first run
in the session's history where the four-lane swarm did not fail on worker
timeout.

Two problems surfaced downstream of that success, neither related to
concurrency. Both are recorded here together because both blocked the same
run and both are evidence-based (task ids, event timestamps, file paths),
not narrated from memory.

## Finding 1 -- verifier self-corrects but does not re-trigger, leaving the card stuck on a resolved problem

**Sequence, from `task_events` for `t_f8c805f4` (Verify swarm outputs):**

1. `claimed` / `spawned` at 1787197491.
2. Five heartbeats over ~326s (normal, not a timeout -- no `max_runtime_seconds`
   was set on this task).
3. `blocked` at 1787197817 with **two** distinct reasons in one payload: (a)
   worker `t_067aa567`'s claimed delivery (a penguin brain-teaser) "does not
   match any board entry" -- its own `result` field said the joke was saved to
   a file (`/home/cwliao/.hermes/kanban/workspaces/t_067aa567/joke.md`), not
   posted as a blackboard comment the way the other three workers did; and (b)
   "flagged content overlaps between items #1 (恐怖書$15) and other numbered
   contributions that appear to be near-duplicates."
4. At 1787197904 -- **87 seconds after the block event, same run** -- a
   comment was posted on the swarm root (`t_389e2a97`, comment id 39) titled
   "[VERIFIER CATCH-UP]": it recovered the missing joke from the attachment
   file, verified it was not corrupted, and stated "all four swarm workers are
   now fully represented in root board comments." **This addresses reason (a)
   only.** Reason (b), the flagged near-duplicate content, is never mentioned
   again in any comment on this task or the root -- the eventual pass result
   (below) asserts no overlap without any visible step that resolved or
   re-checked it. Whether (b) was silently dropped, judged moot, or actually
   fixed in content the ticket did not capture, is not established here.
5. The task's `status` stayed `blocked`. Nothing re-ran verification against
   the now-complete board. It sat there until manually unblocked
   (`hermes kanban unblock t_f8c805f4`, this session, event id 729, timestamp
   1787198250 = 03:57:30 UTC), after which a fresh verifier run (run_id 104,
   claimed 1787198272, completed 1787198466) took **194s (~3m14s)** and
   returned `VERIFIED: ...`.

**The defect:** the verifier process did the work needed to resolve its own
objection, in the same run, and then left the card in the state that asserted
the objection was still open. Whatever produced the catch-up comment did not
have -- or did not use -- a path back to "retry completion" once its own
gap-filling made the block reason false.

**Why this is not a one-off:** the root cause of the gap (`t_067aa567` never
posted its result to the blackboard, only saved a file) comes from the swarm
protocol boilerplate in the task body: "Put cross-worker notes on the root
task using structured comments" is phrased as a convention, not a check. Any
worker that skips it reproduces this exact failure -- verifier blocks
correctly, self-heals, and then hangs pending a human.

**Not established:** whether "verifier self-heals then blocks" happens on
every worker-forgets-to-post case, or only when the missing post is caught
inside the same verifier run before it emits its terminal event. The catch-up
comment is dated after the block event but attributed to the same `run_id
103`; the ordering source is `task_events.created_at`, not a separate
mechanism-level log, so the causal claim here is "same run, block came first,"
not more than that.

## Finding 2 -- synthesized result never reached the user, and the agent didn't know it already existed

The synthesizer (`t_850c40fc`) completed normally at 1787198855: `completed`
event recorded `result_len: 0` but an attached artifact
(`/home/cwliao/.hermes/kanban/attachments/t_850c40fc/deliverable.md`, 1642
bytes) held eight distinct, verified, non-overlapping jokes across all four
lanes -- 4 from claude-code, 1 from grok, 1 native, 2 from antigravity-cli.
This is a real, substantive result, correlated to the task id -- not absent,
just not in the `result` column GATE8-PATH-001's acceptance text names ("a
synthesizer card in `done` with a non-empty `result`"). Noted but not chased
further: both the artifact's own header line and the `completed` event's
`summary` field say "seven ... jokes," one short of the eight actually listed
in the body. This mismatch exists in the synthesizer's own output, not
introduced by this ticket, and is left unresolved.

Separately, and worse: the user confirmed nothing arrived on Telegram. The
Hermes agent handling that Telegram turn (a *different* session than this
one -- it operates through the gateway's chat surface, not this worktree)
narrated, unprompted, that it had `kill -9`'d a background polling process of
its own (PID 1574839, which does not correspond to any worker/verifier/
synthesizer PID recorded in `task_events` for this graph) and was about to
build **a second, entirely new four-lane swarm from scratch** -- not aware
that `t_850c40fc` under the same tenant (`gate8-real`) already held a complete
answer. This was caught and the rebuild was not authorized to proceed
(session note, ~04:50 UTC): checked `tasks` for any row created after the
synthesizer's completion timestamp and found none, confirming no duplicate
graph had actually started yet.

**Two distinct problems bundled in Finding 2:**

- **Delivery**: the completed swarm's result did not reach the Telegram
  surface. Whether this is the notifier watcher failing to pick up a
  synthesizer-kind completion, a routing/correlation gap for this tenant, or
  something specific to how the Telegram session's own turn ended, is not
  established here -- no notifier-side log for this specific event was
  captured before this ticket was written.
- **No self-check before re-work**: the Telegram-facing agent had no cheap way
  to notice "a result for this goal already exists" before proposing to
  rebuild the entire graph. Rebuilding would have created a second dispatch
  load competing for the same `max_in_progress=3` slots as any leftover work
  from the first graph, and would not have fixed the delivery gap it was
  actually trying to solve.

## What this does and does not say about WORKER-TIMEOUT-CONTENTION-001

The concurrency fix is not implicated in either finding. Both findings occur
strictly downstream of all four workers reaching `done` without a single
timeout -- the exact failure mode that fix targeted. This ticket does not
reopen that one; it records what became visible once that failure mode
stopped masking everything after it.

## Options (not decided here)

For Finding 1:
- Have the verifier's gap-filling path, when it changes the board state it
  itself is judging, re-invoke completion evaluation before ending its turn.
- Or: make `blocked` with `kind: needs_input` auto-eligible for a bounded
  number of dispatcher-driven re-tries once new comments land on the blocked
  task's parents, rather than requiring a human `kanban unblock`.
- Or: tighten the swarm protocol boilerplate from a suggestion ("put ...
  using structured comments") to a completion-time check the worker cannot
  skip.

For Finding 2:
- Trace why the notifier watcher (`_kanban_notifier_watcher`, delivers
  `completed`/`blocked`/`spawn_auto_blocked`/`crashed` events per
  `gateway/kanban_watchers.py`) did not surface this synthesizer completion to
  the Telegram subscriber, if it is subscribed at all for tenant `gate8-real`.
- Consider whether an agent composing a "rebuild the swarm" plan should be
  required to check for an existing non-terminal-state result under the same
  tenant/goal first -- this is a process/prompt question, not obviously a code
  fix.

Neither option set is scoped enough to implement without a review round.

## Not in scope

No change to the verifier's own judgment logic, the swarm protocol contract,
the notifier watcher's dispatch logic, or `WORKER-TIMEOUT-CONTENTION-001`'s
already-merged fix.

## Implementation

**Finding 1** — `tools/kanban_tools.py`, `_handle_complete`: after
`kb.complete_task` succeeds, `_auto_post_swarm_handoff` regex-matches the
completing task's own body for `Swarm root / shared blackboard: `<id>`` (the
same boilerplate line, whether the task came from `create_swarm()`'s
contract-bound path or was hand-composed via `kanban_create` — this run's
graph was the latter, since it carried no `[swarm:contract]` line, so a fix
gated on the contract would never have fired for the actual failure
observed). If found, it unconditionally posts a `[swarm:worker-complete]`
comment on that root carrying the worker's own summary/result. Always posts,
even when the worker also posted its own comment — matching "did I already
post" against free-text bodies proved unreliable in the observed transcript
(comments named the originating task id inconsistently), and a duplicate
structured comment is a strictly safer failure mode than the silent gap this
finding is about. Best-effort: any failure is logged and swallowed, never
surfaced as a completion failure.

**Finding 2** — `hermes_cli/kanban.py`, `_cmd_swarm`: after `create_swarm()`
succeeds, `_maybe_auto_subscribe_swarm` mirrors `tools/kanban_tools.py`'s
existing `_maybe_auto_subscribe` (used by `kanban_create`) but was never
called from the swarm path. Subscribes only the synthesizer id — the card
carrying gate 8's own acceptance evidence — not the whole graph, so one
swarm produces one notification rather than one per worker. Session
detection (`HERMES_SESSION_PLATFORM`/`HERMES_SESSION_CHAT_ID`) resolves via
the same `get_session_env` fallback chain used elsewhere: a ContextVar set
in-process by the gateway, falling back to `os.environ` — which the terminal
tool's `_inject_session_context_env` (`tools/environments/local.py`) already
bridges into any subprocess it spawns, so a bare `hermes kanban swarm`
shelled out from a live Telegram turn resolves the session without further
plumbing. A bare CLI/cron invocation with no such session is a silent no-op.

Both changes are additive at existing call sites, described above with
file:line-equivalent detail; no existing behavior path was altered, only
extended. Test coverage added: `tests/tools/test_kanban_tools.py` (3 new
tests — auto-post fires when body names a root, does not fire on an
ordinary task body, and is best-effort when the named root does not exist)
and `tests/hermes_cli/test_kanban_cli.py` (5 new tests — subscribes only the
synthesizer when session context is present, stays a no-op without it,
tolerates `add_notify_sub` raising, respects `auto_subscribe_on_create=false`,
and exercises the TUI `HERMES_SESSION_KEY` fallback). Full run of every
kanban-adjacent test file (431 tests: `test_kanban_cli.py`,
`test_kanban_swarm.py`, `test_kanban_tools.py`, `test_kanban_db.py`,
`test_kanban_cli_dispatch_passthrough.py`,
`test_kanban_dispatch_concurrency_default.py`, `test_kanban_notify.py`,
`test_kanban_notifier.py`, `test_kanban_notifier_watcher_dispatch_gate.py`)
all pass.

**Review round 1** (independent agent, read the ticket, then read the diff
and independently checked the session-context claim against
`tools/environments/local.py` rather than taking the docstring's word for
it): confirmed the regex match, the no-recursion argument, and the
subprocess env-bridging claim all hold. Found two real gaps against
`_maybe_auto_subscribe_swarm`'s own "mirrors `_maybe_auto_subscribe`" claim —
it silently skipped the `kanban.auto_subscribe_on_create` config gate (a
user who opted out via `kanban_create` would be re-subscribed anyway through
the swarm path) and the TUI `HERMES_SESSION_KEY` fallback. Also flagged that
`_auto_post_swarm_handoff`'s `[swarm:worker-complete]` label would mislabel
verifier/synthesizer completions, since their task bodies carry the same
"Swarm root" boilerplate line. All three fixed: the config gate and TUI
fallback were added (now genuinely mirroring the function it claims to),
and the comment prefix was renamed to the role-neutral `[swarm:auto-handoff]`.
Two new tests cover the config gate and the TUI path. Full suite re-run
after the fix (431 tests, above) still passes.

Second review pass (external CLI, `agy`/`grok`) was attempted for this round
too but neither produced usable output (`agy` errored `context canceled`;
`grok` returned only "I'll query..." narration with no results across two
tries) — same tooling failure as the diagnosis ticket's review. A third
attempt against `agy` diagnosed the actual cause: "a tool required the
'command' permission that headless mode cannot prompt for, so it was
auto-denied" — `agy` in this environment cannot run shell/read commands
non-interactively without `--dangerously-skip-permissions`, which was not
used (skipping permission prompts for an unattended review agent is not a
call to make without asking first, and was out of scope for a documentation
fix). Not retried further; the round-1 findings were independently verified
against the DB/source rather than taken on the reviewing agent's word, per
this session's standing rule to verify delegated work.

**Not fixed by this change, left as follow-up:** the verifier-retry gap
itself (a `blocked` card that self-resolves its own objection still requires
a human `kanban unblock`) is not addressed — Finding 1's fix prevents the
missing-handoff trigger from recurring, but does not add a retry path for
when a block happens anyway. Also unfixed: the synthesizer's own
`result_len: 0` / attachment-only delivery pattern, and the "seven vs eight
jokes" internal inconsistency noted in Finding 2 — both are synthesizer
output-shape issues, not the delivery-subscription gap this ticket scoped.
