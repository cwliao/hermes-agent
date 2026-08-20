# GATE8-SWARM-CREATION-TOOL-001 — register a lane-bound swarm-creation tool (GATE8-PATH-001 Option A)

Status: implemented, two rounds of cross-review complete, corrections applied.

## Why this, now

[GATE8-PATH-001](2026-08-19-gate8-path-001.md) named this "Option A" and left it
deliberately unscoped, recommending an isolation test first. That test has
since run twice
([GATE8-ISOLATION-RESULT-001](2026-08-19-gate8-isolation-result-001.md),
[GATE8-RERUN-RESULT-001](2026-08-20-gate8-rerun-result-001.md)) and the swarm
itself has since completed successfully from the CLI and, as of today, from
Telegram — twice, both times only after several minutes of the agent hand-
composing and re-composing a `hermes kanban swarm ...` shell invocation,
narrating fixes to itself between turns ("lane names must map to the real
CLI — `claude-code` not `claude`; `antigravity-cli` not `agy`", "preflight
skill ID validation", "grok lane needs an xAI key"), and at least once
abandoning a partial one-worker graph mid-composition when its turn ended.

This is the same failure shape `GATE8-RERUN-RESULT-001` recorded from the
CLI-isolation run: wrong skill for every lane (`"HUMANIZER"`), worker
runtime silently defaulted instead of the requested value, a partial graph
left behind and dispatched. It has now also been observed live, twice, on
the surface gate 8 actually requires (Telegram). The isolation ticket's own
dichotomy ("if tool calls succeed off Telegram, B is a config fix; if they
fail the same way, A doesn't escape it either") did not hold — tool calls
succeed, composition itself is what's unreliable, and only A addresses that
directly: a typed interface can reject an invalid lane id or a missing skill
before any card exists, which no amount of terminal-path repair can do,
because the fragility is in an LLM assembling a multi-flag shell command
from memory each time, not in the shell path itself.

**Not claimed:** that A alone makes gate 8 reliable. The verifier/notifier
gaps found and fixed in
[GATE8-SWARM-COMPLETED-VERIFIER-RECOVERY-AND-DELIVERY-GAP-001](2026-08-20-gate8-swarm-completed-verifier-recovery-and-delivery-gap-001.md)
and the concurrency fix in `WORKER-TIMEOUT-CONTENTION-001` are both already
merged and are independent of this. This ticket is about the one remaining
failure mode neither of those touches: the agent's own construction of the
swarm request.

## What exists today (read from code, not assumed)

- `create_swarm()` (`hermes_cli/kanban_swarm.py:198-438`) is the only
  entry point that can set lane-contract fields (`expected_lane_id`,
  `preflight_skill_id`, `expected_lane_count`) that `validate_completion()`
  enforces at worker-completion time. `kanban_create` (the only tool
  currently registered for this) cannot set any of them.
- The only caller of `create_swarm()` today is `hermes_cli/kanban.py::
  _cmd_swarm` (1483-1525), reached exclusively through a shell command the
  agent must compose via the `terminal` tool. It parses `--worker
  PROFILE:TITLE[:SKILL,SKILL]` (repeatable) and a **separate, parallel**
  `--worker-lane` list that must match `--worker` in count and order —
  itself a footgun distinct from the skill-mismatch one, since nothing
  ties a given `--worker-lane` value to the intended `--worker` entry
  except position.
- `SwarmWorkerSpec.preflight_skill_id` is **never set** by the CLI parser
  (`parse_worker_arg`, `kanban_swarm.py:485-496`). The only place it gets a
  value is `create_swarm`'s own inference: `spec.preflight_skill_id or
  (spec.skills[0] if len(spec.skills) == 1 else "")` — silently `""`
  (fatal for any non-`native_hermes` lane) the moment a worker is given zero
  or more-than-one skill.
- **No lane→skill mapping exists anywhere in the codebase.** The correct
  skill name per external lane (`claude-code` for `claude`, `grok` for
  `grok`, `antigravity-cli` for `agy`) is documented only in prose inside a
  results ticket. Nothing constrains what string an agent puts in `skills`
  against the lane it claims.
- Validation of a lane-bound swarm's workers now happens *before* any card
  is created (`create_swarm`, ~257-283 — this was `SWARM-E2E-DEFECTS-001`
  Defect 1's fix), so a single malformed worker rejects the whole call
  rather than leaving a partial graph from *that one `create_swarm()`
  invocation*. It does **not** protect against an agent abandoning the
  *shell command itself* mid-composition before ever calling
  `create_swarm` — which is what actually produced the one-worker orphan
  graph observed today (`t_b36bebe5` → `t_3cd7e202`, no verifier/synthesizer)
  and, per `GATE8-RERUN-RESULT-001`, before.
- Tool registration precedent (`tools/kanban_tools.py`): every tool is a
  `_handle_*` function + a JSON-schema `parameters` dict + a
  `registry.register(..., check_fn=...)` call. `_check_kanban_mode` gates
  worker-lifecycle tools (available to dispatcher workers too);
  `_check_kanban_orchestrator_mode` gates board-routing tools and is used
  today only by `kanban_list` and `kanban_unblock`.

## Proposal

Register `kanban_swarm` (name open to revision) as a model-facing tool that
wraps `create_swarm()` directly — no new scheduler, no new store, matching
the boundary GATE8-PATH-001 already set ("a tool over an existing function
is none of" the forbidden things).

### Answering GATE8-PATH-001's three open questions

**1. Argument shape: one list of lane-bound worker objects, not two parallel
lists.**

```json
{
  "goal": "四 lane 笑話 brainstorm",
  "workers": [
    {"lane_id": "native_hermes", "title": "native joke", "body": "..."},
    {"lane_id": "claude", "title": "claude joke", "body": "..."},
    {"lane_id": "grok", "title": "grok joke", "body": "..."},
    {"lane_id": "agy", "title": "agy joke", "body": "..."}
  ],
  "verifier_assignee": "...",
  "synthesizer_assignee": "...",
  "worker_max_runtime_seconds": 300
}
```

`lane_id` should carry a JSON-schema `enum` over `MULTI_AGENT_LANE_IDS` as a
model-facing hint, but that hint is not a real enforcement boundary in this
codebase: `tools/registry.py::dispatch` (614+) passes `args` straight to
the handler with no schema-validation step, so whether an out-of-enum value
is ever rejected before the handler runs depends on the model provider's own
tool-call constraint enforcement, not on anything here. The actual
enforcement has to be the handler calling `create_swarm()`, which already
validates every `lane_id` against `MULTI_AGENT_LANE_IDS` and rejects the
whole call before any card exists (`kanban_swarm.py:233-283`) — this is true
today, for the CLI path, and the new tool inherits it for free by calling
the same function. What the single-list shape still fixes on its own,
independent of any schema enforcement: the CLI's `--worker`/`--worker-lane`
positional-pairing footgun (nothing ties a `--worker-lane` value to its
`--worker` entry except list position) has no equivalent when each worker
object carries its own `lane_id` field.

**2. Gating: orchestrator-only** (`_check_kanban_orchestrator_mode`),
matching `kanban_list`/`kanban_unblock`, not the broader `_check_kanban_mode`
`kanban_create` uses. A four-lane graph with runtime/skill/lane invariants
is board-routing work for an orchestrator-facing profile; a dispatcher-
spawned single-task worker has no legitimate reason to spawn a new swarm
mid-task, and the narrower gate keeps the blast radius of a malformed call
where `kanban_create`'s does not need to be.

**3. Preflight skill: refuse at tool-call time, which requires adding the
lane→skill mapping that does not exist today.** The rerun ticket already
priced the alternative: build-and-fail-closed produced six blocked cards
after consuming a full dispatcher cycle, for an error (`"HUMANIZER"` on
every lane) a static `lane_id → required_skill` table would have caught
before any card existed. This ticket would need to introduce that table
(a small, explicit dict — `{"claude": "claude-code", "grok": "grok",
"agy": "antigravity-cli"}` — is a plausible shape, but pinning it down is
part of the follow-up implementation review, not this ticket) and validate
each worker's `skills`/`preflight_skill_id` against it before calling
`create_swarm()`. The existing quorum rule (`native_hermes` required, ≥2 of
3 external lanes) is unaffected and continues to live in `create_swarm`
itself.

### What this does not change

- `create_swarm()`'s own validation, the lane quorum rule, the dispatcher,
  the notifier, or the verifier — all out of scope, matching
  GATE8-PATH-001's original boundary.
- The CLI (`hermes kanban swarm`) stays as-is for human/operator use; this
  adds a second, tool-shaped entry point to the same function, it does not
  replace the first.

## A prerequisite this ticket does not itself resolve

GATE8-PATH-001 flagged that Option A "Depends on
`TELEGRAM-SWARM-UNREACHABLE-001` Defect A being resolved, since the kanban
toolset is gated off on this surface today." That defect is that
`tools/kanban_tools.py::_profile_has_kanban_toolset` reads the top-level
`toolsets` config key, not `platform_toolsets`, so a config declaring
`platform_toolsets.telegram: [kanban, ...]` has no effect on the gate.

**Checked against the live `~/.hermes/config.yaml` on this host while
writing this ticket, not assumed:** `platform_toolsets.telegram` does list
`kanban`; top-level `toolsets` is `[hermes-cli]` and does not. Defect A is
still live, on the exact surface and host this ticket is about. Every
observed hand-composed swarm attempt today went through the `terminal` tool
shelling out to `hermes kanban swarm`, not through any `kanban_*` tool call
— consistent with the Telegram agent having no kanban tools in its schema
at all, this new one included.

**Registering `kanban_swarm` does nothing on Telegram until Defect A is also
fixed or the top-level `toolsets` list is changed to include `kanban`.**
The latter is a one-line config change but broadens what every profile using
that top-level list can reach, not just Telegram; the former is scoped in
`TELEGRAM-SWARM-UNREACHABLE-001` itself as "make the gate consult
`platform_toolsets` for the active platform" vs. "keep the top-level list
authoritative and reject a `platform_toolsets` entry the gate can't see" —
neither resolved. This ticket takes no position on which; it only records
that shipping the tool proposed here delivers nothing on the motivating
surface without one of those two also landing.

## Not yet decided (deliberately left open for review)

- Exact tool name and whether `worker.body` is required or can default from
  `title` (the CLI currently requires a body but the observed hand-composed
  graphs sometimes reuse the title verbatim).
- Where the lane→skill mapping lives (a new small module, a constant in
  `kanban_swarm.py`, or config) and whether it should be user-overridable —
  a fixed 3-lane mapping is currently plausible only because
  `EXTERNAL_LANE_IDS` is itself a fixed 3-tuple; if that ever grows, the
  mapping needs a real extension story this ticket does not design.
- Whether refusing an unavailable preflight skill should also check that
  the *CLI itself* is installed/authenticated (e.g. the grok lane's xAI
  credential gap observed live today) or only that the skill name is
  spelled correctly — the former is a materially larger check (shelling out
  or reading `hermes doctor`-equivalent state at tool-call time) than the
  latter (a static string lookup), and conflating them risks scope creep
  into `FABRICATED-TOOL-SUCCESS-001`/credential-health territory this
  ticket does not own.
- Whether `FABRICATED-TOOL-SUCCESS-001` should land first, as GATE8-PATH-001
  originally flagged as an open ordering question. Both fixes are
  independent; this ticket takes no position on sequencing.

## Review round 1

Independent agent, instructed to verify every checkable claim against the
live code rather than take the draft's word for it. Confirmed accurate: the
`create_swarm()` validation-before-creation claim, the preflight-skill
inference behavior, the "no lane→skill mapping exists" grep, the
`--worker`/`--worker-lane` pairing description, and the orchestrator-gating
precedent. Found two real issues, both corrected above: the JSON-schema
`enum` claim in Q1 overstated what this codebase actually enforces (fixed by
grounding the argument in `create_swarm()`'s own validation instead of
provider-side schema enforcement), and the ticket had no mention of
`TELEGRAM-SWARM-UNREACHABLE-001` Defect A as a live prerequisite — verified
directly against this host's running `~/.hermes/config.yaml` (not assumed)
and added as its own section above.

## Implementation

`tools/kanban_tools.py::_handle_swarm` registers as `kanban_swarm`,
orchestrator-only (`_check_kanban_orchestrator_mode` + the
`_require_orchestrator_tool` runtime guard, matching `kanban_unblock`).
Answers to the three questions, as implemented:

- **Worker shape**: a single `workers` array, each entry `{lane_id, title,
  body, profile, skills, max_runtime_seconds}`. `lane_id` carries a
  JSON-schema `enum` as a model-facing hint; the real enforcement is
  `_handle_swarm`'s own `lane_id not in MULTI_AGENT_LANE_IDS` check,
  independently of whether any provider enforces the schema — this
  correction from round-1 review is preserved through to the code.
- **Gating**: orchestrator-only, as proposed.
- **Preflight skill**: `hermes_cli/kanban_swarm.py` gained
  `LANE_SKILL_IDS = {"claude": "claude-code", "grok": "grok", "agy":
  "antigravity-cli"}`. A lane worker's `skills`/`preflight_skill_id` are
  filled in from this table; an explicit `skills` that conflicts with the
  lane's requirement is rejected with the expected value named, before
  `create_swarm()` is called — refusal, not build-and-fail-closed.

Also fills `profile` from the calling session's own `HERMES_PROFILE` (or
`"default"`) when a worker omits it, rather than requiring the model to
invent a per-lane profile name — this closes the other live-observed defect
this ticket didn't originally name as a question: an `assignee` naming a
Hermes profile that doesn't exist is never dispatched, with no error
surfaced anywhere (observed directly: three workers stuck in `ready` for
10+ minutes with nothing past a `created` event).

Calls the existing `_maybe_auto_subscribe(conn, created.synthesizer_id)` —
already used by `kanban_create` — so a swarm built through this tool gets
the same notification delivery `GATE8-SWARM-COMPLETED-VERIFIER-RECOVERY-
AND-DELIVERY-GAP-001` added for the CLI path, for free.

**Three static tool-name lists needed updating** for the new tool to
actually reach an agent's schema, none of which are the registry itself:
`toolsets.py`'s flat `hermes-cli` composite list, the `"kanban"` toolset's
own `"tools"` list (also in `toolsets.py`), and
`agent/transports/hermes_tools_mcp_server.py`'s codex-runtime tool list.
Missing any of the three silently keeps the tool out of that surface's
schema with no error — confirmed by writing the tool first and finding it
absent from `resolve_toolset("hermes-cli")`'s output despite `registry.
get_entry("kanban_swarm")` finding it registered.

Test coverage: 12 new tests in `tests/tools/test_kanban_tools.py` covering
skill/profile autofill (verified against actual DB rows, not mocks), no
partial graph on an invalid lane (row count before/after), skill-conflict
rejection, an unbound worker, missing `goal`/`workers`, auto-subscribe,
orchestrator-only gating, non-integer `max_runtime_seconds`, and a mixed
lane-bound/unbound `workers` list. Two existing schema-visibility tests
updated for the new tool name. Full run across every kanban/toolset-adjacent
test file: 488 tests pass.

## Review round 2 (implementation)

Independent agent, instructed to verify claims against the live code and
this host's actually-installed skills rather than the ticket's word.
Confirmed: `LANE_SKILL_IDS` matches real skill folder names under
`~/.hermes/skills/autonomous-ai-agents/`; all three static lists were
updated (also checked for a fourth — website docs and `acp_adapter`'s
display list don't mention `kanban_swarm` yet, a docs/display gap already
present for `kanban_list`/`kanban_unblock`, not a regression this ticket
introduced); the `enum`-is-cosmetic finding from round 1 still holds and
the real backstop (`_handle_swarm`'s own check plus `create_swarm`'s
validation-before-creation) is solid and test-covered; mixed lane-bound/
unbound workers are cleanly rejected by `create_swarm`'s own `lane_mode`
logic. Found one real gap: `workers[i].max_runtime_seconds` was coerced
with a bare `int()` outside any try/except, so a non-numeric value would
have raised an uncaught `ValueError` surfaced as a raw exception string
instead of the tool's normal `tool_error(...)` shape. Fixed, with a test.
A second gap (mixed-lane-worker path untested through the tool itself, only
inferred from `create_swarm`'s own tests) was also closed with a test.

## Not in scope

No change to `create_swarm`, `validate_completion`, the lane quorum, the
dispatcher, the notifier watcher, or the verifier's own judgment logic.
