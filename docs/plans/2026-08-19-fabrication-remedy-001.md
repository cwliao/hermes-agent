# FABRICATION-REMEDY-001 — remedy design for FABRICATED-TOOL-SUCCESS-001

Status: proposed. Decision needed before implementation. No code changes here.

Defect: [FABRICATED-TOOL-SUCCESS-001](2026-08-19-fabricated-tool-success-001.md).
On 2026-08-19 both of the agent's tool calls failed and it then reported a
detailed success — four lanes, per-lane runtimes to 10ms, a verifier pass, a
synthesizer pick — that no store recorded.

## Why the existing guardrail does not cover this

`tool_loop_guardrails` is not broken and did not misfire. It counts
*repetition*:

```yaml
warn_after:      {exact_failure: 2, same_tool_failure: 3, idempotent_no_progress: 2}
hard_stop_after: {exact_failure: 5, same_tool_failure: 8, idempotent_no_progress: 5}
```

Two failures is below every threshold, so it correctly stayed silent. The
property that failed is different: **the response contradicted the tool
results**. Nothing measures that.

## Hook points that already exist

Verified in the deployed release, so the remedy does not need new plumbing:

| what | where |
|---|---|
| per-call failure is already known | `ToolCallGuardrailController.after_call(..., failed=failed)`, called from `run_agent.py` |
| failure classification | `classify_tool_failure()` / `classify_failure_class()` in `agent/tool_guardrails.py` |
| turn boundary | `ToolCallGuardrailController.reset_for_turn()`, called from `agent/turn_context.py` |
| turn finalization | `finalize_turn()` in `agent/turn_finalizer.py` |

A per-turn tally of calls attempted versus calls failed is a small addition to
the controller, which already receives every result.

## Option A1 — refuse to finalize a turn in which every tool call failed

At `finalize_turn()`, if the turn made at least one tool call and **all** of
them failed, do not let the response stand unqualified: append a factual
correction, force another turn, or mark the response.

- Deterministic. The condition is a counter comparison, not a judgement about
  language. `after_call` already receives `failed` per call; the tally is an
  addition to a controller that sees every result.
- **Would have prevented the 2026-08-19 incident.** Two calls, both failed,
  and the response asserted success.
- Narrow by construction. It says nothing about a turn where some calls
  succeeded and the response embellishes the rest, and nothing about a turn
  with no tool calls at all.
- Needs a decision on what "refuse" means. Appending a correction is least
  disruptive; forcing another turn risks a loop on a surface where the
  underlying tool is simply broken, which is what happened here.

## Option A2 — detect the contradiction in general

Compare what the response claims against what the tools returned, for any mix
of outcomes.

- Would cover the shape A1 misses.
- **Not specifiable in this ticket.** A keyword proxy ("failed", "error") is
  defeated by a response that mentions an unrelated failure, and by one that
  fabricates around partial success. Anything stronger appears to need a model
  call, with its own cost and its own failure modes.
- Recorded so that A1 is not mistaken for complete coverage.

## Option B — always show what the tools actually did

On unattended surfaces, attach a short factual line to the response: how many
calls were made, how many failed, and which tools.

- Deterministic, and independent of what the response says.
- Narrows the gap the gate-8 ticket identified — *"a user reading Telegram
  cannot distinguish a plausible fabricated result from a real one"* — **for a
  reader who reads the line**. It does not close it. A reader who skips the
  footer is no better off, and B does not stop the false report being
  produced or being consumed by anything downstream that reads only the prose.
- Cost is low; its failure mode is noise rather than silence.

## Recommendation

**A1 and B together.**

An earlier draft of this ticket recommended B alone. Both reviewers rejected
it and were right: it bundled the tractable narrow case into the intractable
general one and then declined both, recommending a mitigation that by the
ticket's own admission would not have prevented the incident that motivated
it. A1 is deterministic, uses hooks verified to exist, and is the part that
actually stops the failure; B is what helps when A1's condition does not hold.

Neither closes `FABRICATED-TOOL-SUCCESS-001` in general — A2 is the part that
would, and it is not specifiable yet. This ticket should not be read as
closing that defect.

### Readiness

A1's intervention semantics and every operational parameter of B are open
below. Neither is ready to implement; this ticket is a decision, not a design.

## Not yet decided

- What A1 does on trigger: append a correction, force another turn, or mark
  the response. Forcing a turn can loop when the tool itself is broken.
- Whether A1 should fire when the turn made no tool calls at all — arguably a
  different defect, arguably the same one.
- Whether B's line appears on every unattended response or only when a call
  failed. Always-on is more honest and noisier.
- Whether B belongs in the response body, where a phone notification shows it,
  or in metadata, where it does not.
- Whether interactive surfaces need either. They show tool activity already,
  but `hard_stop_enabled: false` is honoured there, so they are the *less*
  protected surface.

## Not in scope

No change to `tool_loop_guardrails` thresholds, `classify_tool_failure`, or
the Telegram adapter.
