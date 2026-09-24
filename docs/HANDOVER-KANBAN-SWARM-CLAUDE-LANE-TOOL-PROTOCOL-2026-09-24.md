# Handover: Hermes Kanban Swarm — `orchestrator-claude` Lane Cannot Call Kanban Tools

- Date: 2026-09-24 (Asia/Taipei)
- Repository: `/home/cwliao/.hermes/hermes-agent`
- Service: `hermes-gateway.service`, `clawo-serve.service`
- Working language: Traditional Chinese for user-facing output
- Related: `HANDOVER-KANBAN-SWARM-AGY-LANE-WORKSPACE-SANDBOX-2026-09-24.md` (same
  swarm run, different lane, different root cause — do not conflate the two)

## Executive status

In the same 3-lane cross-model review swarm as the AGY workspace-sandbox
handover, `orchestrator-claude` (task `t_7d3163d4`, root `t_8eae5233`) never
completed. It was retried 4 times (runs `894`, `897`, `898`, `900`), each run
taking ~60s and ending identically: the worker exits cleanly (`rc=0`) without
ever calling `kanban_complete`/`kanban_block`/`kanban_request_review`,
self-reporting things like:

```
Resolving this requires a human/operator to check why the kanban MCP tool
server isn't connected for this worker session.
```

```
due to a broken kanban tool connection — this needs to be fixed by an
operator outside the session, not by repeating the same call.
```

The dispatcher correctly flags each as a `protocol_violation` (a run without
a terminal kanban call is always a failure regardless of what it did), and
after 4 violations (limit 3) the task went `sticky: true` — no more automatic
retries. This is **structural, not transient**: identical failure across 4
independent runs.

**Workaround applied**: `hermes kanban unlink t_7d3163d4 t_0dbeae2f` (removed
it as a required parent of the verifier), so the swarm could complete on the
AGY + Grok reviews alone instead of deadlocking forever (see the AGY handover
for why this swarm type has no `--worker-quorum` escape hatch). The task
itself was left in place with an explanatory comment, not deleted.

## Root cause (traced this session)

Hermes' kanban dispatcher routes the `orchestrator-claude` profile through
the generic custom OpenAI-compatible provider plugin
(`plugins/model-providers/custom/__init__.py`), which talks to the
`clawo-serve.service` (`node /home/cwliao/project/agentpool/node_modules/.bin/clawo serve`,
a thin wrapper around the third-party npm package
`@enderfga/claw-orchestrator`). For this lane, the live spawned process was:

```
claude -p --input-format stream-json --output-format stream-json \
  --replay-user-messages --verbose --include-partial-messages \
  --permission-mode bypassPermissions --model claude-sonnet-5 \
  --tools "" \
  --system-prompt "You are a helpful AI assistant acting as a pure LLM
  behind an API proxy. You do NOT have access to any tools such as Bash,
  Read, Write, Edit, Glob, Grep, or any other built-in tools. Do NOT attempt
  to call any tools or execute any commands. When you need to perform an
  action, use ONLY the tools defined in the <available_tools> block below,
  and respond with <tool_calls> tags as instructed there. ..."
```

i.e. Claude Code's own native tools AND MCP are fully disabled
(`--tools ""`), and the kanban tools (`kanban_complete`, `kanban_attach`,
etc.) are instead offered purely as **prompt text** describing a bespoke
`<tool_calls>[...]</tool_calls>` JSON-in-text convention that clawo's harness
parses back out of the model's plain-text response. This is the same
lowest-common-denominator mechanism presumably used for the `agy`/`grok`
backends (which apparently honor it reliably enough for their kanban tasks
to complete in this same swarm run).

## Root cause — CONFIRMED (update: same session, after further investigation)

The hypothesis above (a pure model-training quirk) was **wrong as stated**. The
actual mechanism was confirmed with a direct, reproducible test:

1. First ruled out session staleness: `clawo session-list` showed the
   `orchestrator-claude` backend was pinned to one persistent session
   (`openai-sys-d9a1589d7528`) that had accumulated **29 turns** across the 4
   dispatcher retries (session key = hash of model+systemPrompt, so identical
   retries reuse the same conversation instead of starting fresh — a real but
   secondary issue). Stopped it (`clawo session-stop openai-sys-d9a1589d7528`)
   and re-tested on a brand-new isolated kanban task (`t_2aac20e8`, trivial
   goal, guaranteed-fresh session). **Failed identically within 60s** — this
   ruled out session staleness as the cause.
2. Called clawo's OpenAI-compat endpoint directly
   (`POST http://127.0.0.1:18796/v1/chat/completions`, token from
   `~/.openclaw/server-token`) with a minimal one-tool request. The model's
   actual reply: *"The `kanban_complete` tool isn't actually available in
   this session despite being listed — the call failed with 'No such tool
   available.'"* — i.e. Claude **did** attempt the tool call, and got a real
   rejection from Claude Code's own tool-execution harness, not a made-up
   self-diagnosis.
3. `claude --help` documents `--tools ""` precisely: it disables the
   **built-in** tool set (Bash, Read, Edit, ...) and "ignores user, project
   and local settings files... **add `--strict-mcp-config` to skip MCP
   servers too**." clawo's compat shim passes `--tools ""` but **not**
   `--strict-mcp-config`.
4. `orchestrator-claude`'s own profile `config.yaml` has a `mcp_servers:`
   block wiring in real MCP servers (`sqlite-tasks`, `sqlite-kb`,
   `sqlite-session`, `klib`, `notion`, `agentmemory`) — confirmed by asking a
   fresh session (no `tools` in the request at all) to list its available
   tools: it listed `Agent, Bash, Edit, ListAgents, Read, ReportFindings,
   ScheduleWakeup, ShareOnboardingGuide, Skill, ToolSearch, Workflow, Write`
   — genuine native tools, present despite the compat layer's intent to
   suppress all of them.

**Confirmed root cause**: `--tools ""` does not disable MCP-provided tools,
only the built-in set. `orchestrator-claude`'s profile has real MCP servers
configured (for its legitimate non-kanban orchestrator role). Those MCP
tools stay live in every session spawned for this profile, including
kanban-lane compat-bridge sessions. Because genuine native tools **are**
present, Claude does not fully trust the "you have zero tools, use the text
protocol instead" system-prompt framing, attempts `kanban_complete` as a
real native tool call, and gets a genuine "No such tool available" rejection
from Claude Code's own harness — which it then (accurately) reports as a
broken tool connection.

## Working fix applied this session

Rather than patch `@enderfga/claw-orchestrator`'s compiled dist (third-party,
fragile) or strip `orchestrator-claude`'s MCP servers (would degrade its
other, legitimate orchestrator use), used the **already-proven-correct**
dispatch mechanism instead: `agentpool`'s `dispatch.js`, which drives Claude
via `clawo session-start` (a real Claude Code session with native tools
genuinely wired, not the openai-compat `-p --tools ""` stub) — the same
mechanism [[reference_agent_dispatch_skill_and_fanout]] already documents for
cross-model code review. One call
(`node dispatch.js` with `{"capability":"review","prompt":"...","cwd":"/home/cwliao/project/klib","preferred_engine":"claude"}`)
produced a real, substantive REVISE verdict with two concrete, previously
unnoticed findings, in place of the four failed kanban-lane attempts.

## Durable fix — DONE (2026-09-24, same day, follow-up)

Implemented option 2 below. `hermes profile create worker-claude
--clone-from orchestrator-claude` (copies `config.yaml`/`.env`/`SOUL.md`/
skills), then removed the `mcp_servers:` block from
`~/.hermes/profiles/worker-claude/config.yaml` (the block that was leaking
real native tools into `orchestrator-claude`'s kanban sessions in the first
place — see root cause above). `orchestrator-claude` itself was left
completely untouched for its other, non-kanban role.

Verified with a fresh diagnostic kanban task
(`kanban create ... --assignee worker-claude`, ask it to call
`kanban_comment` then `kanban_complete` immediately): **completed on the
first attempt**, single run, 53s, zero protocol violations — the exact
task shape that failed 4/4 times on `orchestrator-claude`.

**Going forward**: assign kanban tasks/swarm workers needing the Claude
lane to `worker-claude`, not `orchestrator-claude`. Option 1 below (route
through `clawo session-start` at the dispatcher level) remains the more
structurally correct fix if anyone wants to invest in it later, but is no
longer necessary to unblock kanban-side Claude usage.

## Original suggested durable fix (superseded by the section above)

The kanban swarm's `orchestrator-claude` lane itself is still broken for any
future use. Real options, all outside this session's scope:

1. Give the kanban dispatcher a way to route the `claude` lane through
   `clawo session-start`/`session-send` instead of the openai-compat stub
   (mirrors what `dispatch.js` already does correctly) — the most correct
   fix, but requires editing Hermes' kanban dispatcher / lane-selection code,
   not just config.
2. Create a **separate** hermes profile (e.g. `worker-claude`, mirroring
   `worker-agy`/`worker-grok`'s naming) cloned from `orchestrator-claude` but
   with `mcp_servers:` removed, and point the kanban swarm's claude lane at
   that instead — leaves `orchestrator-claude`'s other (non-kanban) use
   untouched. **Done — see "Durable fix — DONE" above.**
3. Until either is done: for anything needing a real Claude review or
   kanban-tool-calling verdict, use `agentpool/dispatch.js`
   (`preferred_engine: "claude"`) directly, as done here, rather than
   assigning a kanban task to `orchestrator-claude`. Still valid advice for
   one-off reviews outside a kanban swarm; `worker-claude` is for kanban
   tasks specifically.

Also saved to Claude Code auto-memory
(`project_hermes_kanban_orchestrator_claude_lane_tool_protocol.md`),
AgentMemory, and instamem for cross-session/cross-agent recall.
