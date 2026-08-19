# GATE8-ISOLATION-RESULT-001 — the isolation diagnostic, and why its own dichotomy failed

Status: result record. Supersedes the decision framing in
[GATE8-PATH-001](2026-08-19-gate8-path-001.md); that ticket's options are now
differently weighted and its proposed discriminator did not discriminate.

## What was run

`GATE8-PATH-001` recommended running the same request against the same agent
runtime off Telegram before choosing between adding a swarm tool (A) and
repairing the terminal path (B). That was run on 2026-08-19.

The request text was identical to the one sent through Telegram, with one
deliberate change: `tenant` was `gate8-isolation` rather than
`gate8-telegram`, so the diagnostic could not contaminate the gate's own
evidence.

Judged from the task store and tool events, never from the response text.
Baseline recorded first: 43 tasks, zero under any `gate8*` tenant.

## Result

| | Telegram attempt | CLI isolation run |
|---|---|---|
| `terminal` tool | one call with `command=None`, one killed at 60s | **succeeded** — discovered the `--tenant` flag and a real workspace path |
| response shape | preamble ending at a colon, three times | **preamble ending at a colon**: "讓我現在用真正的 swarm 命令建立四 lane：" |
| cards created | 0 | **0** (total unchanged at 43) |
| fabricated a success | **yes** — four lanes with per-lane runtimes to 10ms, a verifier pass, a synthesizer pick | **no** — stopped at the announcement |

## The dichotomy did not hold

`GATE8-PATH-001` predicted two outcomes:

> tool calls succeed off Telegram → the fault is in this surface, and B is
> probably a configuration fix;
> tool calls fail the same way off Telegram → the fault is in the model or the
> tool schema, and A does not escape it either.

Neither describes what happened. Tool calls **succeeded** off Telegram, and
the user-visible failure **reproduced anyway**: the turn ended after
announcing the decisive command, and nothing was created.

So the discriminator the ticket proposed does not discriminate. Recording that
is the point of this document — the diagnostic was worth running and its
designed reading was wrong.

## What this rules out, and what it does not

**Neither A nor B is supported or refuted, and for the same reason.** The run
never reached the decisive command — the one that creates cards. What it shows
is that *exploratory* `terminal` calls work: a read that lists a flag and a
path. The Telegram failures (`command=None`, a 60s kill) were on calls that
were attempting something; nothing here tested whether the decisive
invocation survives.

An earlier draft of this ticket said "B is not supported" while softening A to
"weakened, not removed". Both reviewers rejected that asymmetry and were
right: the untested step is the step both options exist to fix, so it cannot
count against one and not the other.

Two readings of the stall are live and this run separates neither:

- The agent stopped for a reason unrelated to the command it was about to
  emit, in which case A buys little.
- The agent stalled **at** the transition from exploration to composing a
  long multi-flag invocation, in which case A targets the failure point
  directly and is the stronger option.

The second reading is at least as consistent with the evidence as the first.

**The locus is a hypothesis, not a finding.** The shape suggests the model
ends the turn after stating intent instead of emitting the tool call. That was
**not** established: the CLI run's session could not be located in
`agent.log`, whose entries for that window are all `cron_` sessions, so no
turn-end reason was obtained. Anyone continuing this should start by capturing
that reason, which is the single most informative missing datum.

## New information the diagnostic was not designed to produce

The Telegram attempt fabricated a detailed success; the CLI run stopped
honestly. Same model, same request, same tool availability, different surface.

That is one observation of each under confounds that were not controlled —
different releases, different `unattended` resolution, and an uncompared
system prompt. It is a lead, not a difference established to be caused by the
surface. It bears on
[FABRICATED-TOOL-SUCCESS-001](2026-08-19-fabricated-tool-success-001.md),
which recorded the fabrication without any comparison case. It is one
observation of each, so it establishes nothing about frequency or cause.

## Confounds, disclosed

- **The two runs were not on identical code.** The Telegram attempt ran
  release `910955335d`; the isolation run ran `7ef40eddce`. The full range was
  checked rather than assumed: outside `docs/` and `tests/` it touches three
  files — `agent/agent_init.py`, `agent/tool_guardrails.py`,
  `agent/turn_finalizer.py` — all of it the tool-outcome footer and its
  wiring. No prompt, model, or tool-schema code differs. That footer appends
  only when a call fails and none failed here. The runs were still not
  identical.
- **Observability differed, and that is itself a confound.** The CLI run could
  not be located in `agent.log`; the Telegram attempt was traceable in the
  gateway journal. The two surfaces were therefore not observed to the same
  depth, and the missing turn-end reason is a gap in the diagnostic, not just
  in the follow-up.
- **Surface is not a single variable.** System prompt content, conversation
  history and context length, and any surface-specific wrapping were **not**
  compared. The phrase "same model, same request, same tool availability"
  below is accurate for those three things only and should not be read as
  everything else being held constant.
- CLI resolves `platform=cli`, so `unattended` is false and
  `hard_stop_enabled: false` is honoured; on Telegram it is forced true.
  Whether that difference can affect whether a tool call is emitted was **not
  determined**. An earlier draft asserted it could not; that was unevidenced,
  and it was load-bearing, since it was being used to rule out an alternative
  explanation for the fabricated-versus-honest difference below.
- `approvals.mode: manual`. It did not block: `terminal` ran.
- Tool availability matched in the respect being tested — both surfaces have
  `terminal` and neither has working `kanban` tools.

## Consequence for gate 8

Neither A nor B is now the obvious path, and the evidence for choosing between
them is weaker than before this run, not stronger. The next step is the
turn-end reason, not an implementation.

The tool-outcome footer stayed silent throughout, correctly — no tool call
failed. Note what that means for gate 8: the shipped guard does **not** cover
this failure mode. A turn that stops before acting, having claimed nothing,
produces no footer and no alert.
