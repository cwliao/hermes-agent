---
title: "Kanban swarm worker context and compression budget"
status: IMPLEMENTATION_REVIEW_PASS_WITH_FOLLOWUPS
date: 2026-08-23
type: ticket-design
ticket: KANBAN-WORKER-CONTEXT-COMPRESSION-001
target_repo: hermes-agent
priority: P1
---

# KANBAN-WORKER-CONTEXT-COMPRESSION-001

## Objective

Prevent a Kanban swarm worker from entering a repeated context-compression
failure loop before it can call `kanban_complete` or `kanban_block`.
The worker must receive enough task/contract context to finish, while every
model request and compression summary request must fit the configured context
window.

## Production evidence

Live four-lane task `t_d4611a2a` ("autumn joke request") produced two
protocol-violation runs before run #172 completed:

- runs #170 and #171 exited without `kanban_complete` or `kanban_block`;
- the model repeatedly reported a 65,536-token context limit with prompt sizes
  around 131,083–138,156 characters;
- context compression failed after three attempts on each run;
- the worker log showed the subprocess loading a broad CLI surface: 24 tool
  definitions and approximately 53,226 characters of tool schema JSON;
- the live `worker_context` payloads for this task were only approximately
  1,503, 2,018, and 2,502 characters. Therefore this incident does **not**
  prove that the entire swarm blackboard was injected into this worker. The
  larger risk remains the combination of broad tool schemas, unbounded
  aggregate context, and an unbounded compression output request.

The compression path in `agent/context_compressor.py` deliberately omits
`max_tokens` from its summary call. With no dedicated `summary_model`
configured, the main `drafter-active` model is used; vLLM consequently treats
the output request as its 65,536-token default. This can make the compression
request itself impossible even when the primary output-cap parser is correct.

## Scope

1. Bound the default worker tool surface while preserving explicit,
   task-specific tool overrides.
2. Add a deterministic aggregate cap for `build_worker_context()`; preserve
   task identity, lifecycle state, parent/child contract facts, and the
   completion-call example before trimming lower-value history.
3. Give compression summary calls an explicit dynamic output budget that fits
   the remaining context window and the configured summary budget.
4. Add metadata-only diagnostics for tool-definition count/schema size,
   worker-context size, estimated request budget, compression input size, and
   chosen summary output budget. Do not log message bodies, secrets, or full
   prompts.

## Non-goals

- Do not solve `drafter-active` creativity or factual reliability.
- Do not modify the replay/idempotency guard or Telegram delivery semantics.
- Do not silently change the user's provider/fallback policy.
- Do not assume every swarm has four lanes; caps must apply to arbitrary
  topology and configured worker roles.
- Do not remove tools required by an explicit worker skill/task contract.

## Design constraints and open decisions

- A lean default profile is a candidate, not yet an approved hard-coded list.
  The implementation must first identify the tools required by the worker's
  role/skill and allow explicit opt-in for additional tools.
- The aggregate context cap must be measured in the same conservative unit the
  model adapter uses for budgeting; character count may be retained as a
  diagnostic but is not sufficient as the only token estimate.
- Compression must reserve room for the summary output. A fixed small value is
  acceptable only as a fallback after the dynamic calculation, not as the sole
  policy.
- If no feasible summary request remains, the system must use a bounded,
  deterministic fallback or fail the run with an actionable event; it must not
  retry the same impossible request three times.

## Acceptance criteria for implementation

1. A regression fixture reproduces the 65,536-context error with a large
   worker context and proves that the compression request sends a bounded
   `max_tokens` value satisfying `input_tokens + max_tokens + provider
   overhead <= context_window` (with an explicit safety margin).
2. A worker-context fixture containing many comments, attempts, parents, and
   handoffs proves the aggregate cap is enforced deterministically and that
   the completion contract and lifecycle facts survive truncation.
3. A tool-surface test proves the default worker request is materially smaller
   than the current broad CLI surface, while an explicit task override remains
   available and is recorded in metadata.
4. A failure-path test proves an impossible compression request does not repeat
   unchanged three times and does not silently turn into a protocol violation.
5. Existing Kanban, compression, output-cap, and gateway tests remain green.
6. One synthetic four-lane end-to-end run completes with no repeated compression
   error and with inspectable metadata showing the selected budgets.
7. No acceptance criterion depends on a particular model producing good jokes;
   this ticket is about execution reliability and bounded context.

The implementation must treat the completion contract, task identity,
`root_id`, role, lifecycle state, and the concrete `kanban_complete`/
`kanban_block` call shape as mandatory context. They may not be discarded by
the aggregate truncation policy. Tool reduction must be role/skill-aware and
must not be implemented as a blanket removal of tools that an explicit task
requested.

## Cross-review questions

- Does tool reduction preserve workers that explicitly need browser, web,
  memory, code execution, or other task tools?
- Can truncation ever remove the completion call shape or cause a worker to
  violate the Kanban protocol?
- Does the summary budget calculation account for both input and output rather
  than merely clamping output to an arbitrary constant?
- Are diagnostics useful without leaking prompt contents or credentials?
- Is the retry policy bounded when the provider returns a context-limit error?

## Review record

This is a design ticket only. It authorizes neither implementation nor
deployment. Upstream remains download-only; no upstream write is in scope.

External review results will be appended after the exact ticket text is sent
to available read-only reviewers. A missing external response is recorded as
`BLOCKED`/`UNAVAILABLE`, never treated as `PASS`.

### Design cross-review disposition

Two independent repo-local review passes were performed against this exact
ticket text: (A) reliability/budget accounting and (B) compatibility/data-loss
boundaries. Both agreed that the ticket is correctly separated from model
quality and output formatting, but requested the following corrections, now
incorporated above:

- `max_tokens` must be proven to fit the full input-plus-output budget, not
  merely be present or below a fixed constant;
- mandatory lifecycle/contract fields must be preserved under truncation;
- the default tool profile must be role/skill-aware and retain explicit
  overrides;
- the live end-to-end check must inspect bounded-budget metadata, not infer
  success from a good-looking model answer.

External reviewer status: Claude `BLOCKED/UNAVAILABLE` (CLI not logged in);
AGY `BLOCKED/UNAVAILABLE` (the permission layer rejected sending repo content
to that adapter). No external PASS is claimed. Local consensus:
`DESIGN_REVIEW_PASS_WITH_FOLLOWUPS`; implementation remains separately
authorized work.

### Implementation and cross-review

Implemented locally in:

- `hermes_cli/kanban_db.py`: bounded default worker toolsets, profile-level
  `kanban.worker_toolsets` override, task-scoped
  `[kanban:worker_toolsets]` override, metadata-only tool-budget logging, and
  a 24 KiB aggregate worker-context cap that regenerates/preserves the swarm
  contract and completion-call shape.
- `agent/context_compressor.py`: compression summary calls now send a dynamic
  `max_tokens` reservation bounded by estimated prompt tokens, context window,
  and a 512-token safety margin.

Implementation review used two independent local passes:

- Reliability/budget pass: **PASS after correction**. It required proving
  `input + output + margin <= context_window`, not merely adding a constant
  output cap; the regression test now covers the 65,536-window case.
- Compatibility/data-loss pass: **PASS after correction**. It caught that
  worker metadata-only completion is a supported legacy path and that a
  blanket tool reduction would break specialist workers; both are now covered
  by the validator behavior and task/profile override paths.

Validation:

- targeted implementation suite: `51 passed`;
- Kanban/compression integration sweep: `268 passed, 1 skipped`;
- `compileall`: passed;
- `git diff --check`: passed.

The live four-lane production-style synthetic run and deployment are still
follow-up gates; this ticket is not marked deployed.
