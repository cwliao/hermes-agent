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

Working hypothesis (not verified against clawo's closed/compiled source,
`node_modules/@enderfga/claw-orchestrator/dist/...`): Claude specifically is
prone to behaving as though a real tool/MCP connection *should* exist —
its training strongly biases it toward native tool-use semantics whenever a
system prompt describes an `<available_tools>` block in terms that read like
a tool/function schema, even when told point-blank "you do NOT have tools."
When no native tool-use or MCP channel actually exists, it appears to
conclude a real MCP server "isn't connected" rather than reliably falling
back to emitting the requested `<tool_calls>` text tags — i.e. a
model-specific prompt-following gap in this particular tool-emulation
protocol, not a Hermes kanban bug and not (as the worker itself guessed) a
literal broken connection.

**Not verified this session** (would require clawo's source or a live
transcript capture, neither available in this pass):

- Whether the model ever attempted `<tool_calls>` tags at all, or gave up
  before emitting any.
- Whether `clawo session-start` mode (its actual "Claude Code session"
  integration, listed in `clawo --help` alongside Codex/Gemini/Cursor) gives
  Claude real native tool-use / MCP access instead of the `-p --tools ""`
  stub mode, which would sidestep this entirely — the custom-provider path
  Hermes currently uses appears to be the more generic, chat-completions-style
  integration, not that mode.

## Suggested follow-up (not done in this session)

1. Confirm with a live transcript (e.g. capture clawo-serve's raw request/
   response for one `orchestrator-claude` kanban run) whether the model is
   emitting malformed/absent `<tool_calls>` tags, to convert the hypothesis
   above into a confirmed diagnosis.
2. If confirmed, the durable fix likely belongs in how Hermes' kanban
   dispatcher invokes the `orchestrator-claude` profile — either route it
   through clawo's native Claude Code session mode (real tool-use/MCP)
   instead of the generic custom-provider stub, or harden the injected
   system prompt specifically for the Claude backend (e.g. more forceful,
   repeated instruction, or a one-shot example turn) to suppress the
   tool-use assumption. Either change lives outside `@enderfga/claw-
   orchestrator`'s compiled dist (a third-party dependency — do not patch
   its `node_modules` output directly; any real fix belongs in how Hermes
   or agentpool invokes it, or as an upstream request to that package).
3. Until fixed, avoid depending on `orchestrator-claude` for automated kanban
   tasks that require a terminal kanban call; `agy`/`grok`/`default`
   (native_hermes) lanes are confirmed working for this purpose as of this
   session.

Also saved to Claude Code auto-memory
(`project_hermes_kanban_orchestrator_claude_lane_tool_protocol.md`),
AgentMemory, and instamem for cross-session/cross-agent recall.
