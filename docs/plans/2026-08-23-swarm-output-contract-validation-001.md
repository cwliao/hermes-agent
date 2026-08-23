---
title: "Kanban swarm output contract and narrow formatting validation"
status: IMPLEMENTATION_REVIEW_PASS_WITH_FOLLOWUPS
date: 2026-08-23
type: ticket-design
ticket: SWARM-OUTPUT-CONTRACT-VALIDATION-001
target_repo: hermes-agent
priority: P2
related_tickets:
  - KANBAN-WORKER-CONTEXT-COMPRESSION-001
  - MODEL-REPLAY-RELIABILITY-001
---

# SWARM-OUTPUT-CONTRACT-VALIDATION-001

## Objective

Prevent malformed internal annotations and obvious punctuation artefacts from
reaching the user at the Kanban swarm output boundary, without pretending that
post-processing can repair weak model creativity or factual reliability.

## Production evidence

The four-lane autumn-homophone run eventually delivered:

1. `native_hermes` — a joke ending with the internal-looking marker `（30字）`;
2. `claude` — a weak/unclear pun;
3. `grok` — a weak/unclear pun;
4. `agy` — a sentence ending with an unmatched extra `」`.

The first and fourth are bounded formatting/contract defects. The weak pun
quality is a known limitation of the `drafter-active` model and is not claimed
as fixable by this ticket.

Current `kanban_swarm.validate_completion()` validates role/lifecycle metadata
and non-empty synthesizer output, but does not validate the user-visible text's
format, hidden length annotations, or punctuation balance. A global Telegram
sanitizer would be unsafe because ordinary user content can legitimately use
parentheses and quotation marks.

## Scope

1. Define a swarm-specific result contract with explicit user-visible `text`,
   measured `char_count`, and validation status/reason, while retaining a
   human-readable result for existing consumers.
2. Have verifier/synthesizer validation reject or request bounded rework for
   malformed lane outputs; preserve the original lane text for audit rather
   than silently rewriting it everywhere.
3. Detect only explicitly scoped hidden length markers such as `（30字）` or
   `(30字)` when the task contract says the length instruction is internal.
4. Detect an unmatched trailing quote/bracket at the swarm boundary and apply
   a deterministic, narrowly scoped correction or bounded rework decision.
5. Add tests for the observed malformed examples and for valid ordinary
   parentheses/quotes that must remain untouched.

## Non-goals

- Do not globally sanitize Telegram messages, session transcripts, or model
  output outside the swarm contract.
- Do not rewrite a model's joke into a different joke merely to improve style.
- Do not claim to solve the model's creativity, reasoning, or reliability
  limitations.
- Do not use heuristic text cleanup as a substitute for structured verifier
  evidence.
- Do not create an unbounded verifier/rework loop.

## Design constraints and open decisions

- The contract must distinguish internal instructions from user-requested
  literal text. Marker removal is permitted only for the former.
- Prefer reject-and-rework, with a small attempt budget, over silently changing
  arbitrary punctuation. If a deterministic cleanup is retained, it must be
  limited to a proven suffix pattern and recorded in validation metadata.
- `char_count` must define whether spaces and punctuation count, and the same
  definition must be used by the verifier and final synthesizer.
- Existing downstream delivery must continue to receive a plain text result;
  structured metadata is additive and must not leak internal review fields.

## Acceptance criteria for implementation

1. The swarm result contract is documented in code and tested for lane and
   synthesizer roles.
2. The exact observed `（30字）`/`(30字)` leak is rejected or removed only
   under an internal-length-marker contract; legitimate parentheses remain.
3. The exact observed unmatched trailing `」` is rejected or corrected by the
   swarm boundary; balanced quotes and normal Chinese punctuation remain
   unchanged.
4. Invalid output triggers at most a bounded rework/fallback path and cannot
   create an infinite Kanban loop or falsely mark malformed output as valid.
5. The original lane text and validation reason remain inspectable in
   metadata/audit output without being sent as hidden text to Telegram.
6. Existing Kanban, Telegram delivery, and gateway tests remain green.
7. One synthetic four-lane end-to-end run demonstrates that formatting
   violations are either corrected or blocked with an explicit reason, while
   model-quality limitations are reported separately.

The raw lane text must remain immutable for audit. A lane result that fails the
contract must not be passed to the synthesizer as if it were valid; it must be
reworked within a bounded budget or explicitly excluded/blocked. Any
deterministic cleanup must be limited to the exact contract-scoped pattern and
recorded in validation metadata; there is no permission to strip arbitrary
trailing quotes from all Telegram output.

## Cross-review questions

- Is the proposed validation narrow enough not to corrupt ordinary user text?
- Does the verifier preserve exact lane text instead of introducing another
  paraphrase/game-of-telephone failure?
- Are marker removal and quote handling contract-driven and auditable?
- What happens when all lanes are malformed or the rework budget is exhausted?
- Does the result remain backward-compatible with current Kanban watchers and
  Telegram delivery?

## Review record

This is a design ticket only. It authorizes neither implementation nor
deployment. The known model-quality limitation is explicitly retained as an
out-of-scope operational/model issue. Upstream remains download-only.

External review results will be appended after the exact ticket text is sent
to available read-only reviewers. A missing external response is recorded as
`BLOCKED`/`UNAVAILABLE`, never treated as `PASS`.

### Design cross-review disposition

Two independent repo-local review passes were performed against this exact
ticket text: (A) output-contract/backward compatibility and (B) safety/audit
boundaries. Both agreed that model creativity must remain out of scope and
that this must not become a global Telegram sanitizer. The correction set,
now incorporated above, is:

- preserve immutable raw lane text and keep validation metadata separate;
- do not let malformed output reach the synthesizer as validated input;
- make reject/rework the default, with a bounded attempt count;
- require any exact-marker cleanup or punctuation correction to be
  contract-scoped and auditable;
- test all-failed-lane behavior and backward compatibility with existing
  watchers/delivery.

External reviewer status: Claude `BLOCKED/UNAVAILABLE` (CLI not logged in);
AGY `BLOCKED/UNAVAILABLE` (the permission layer rejected sending repo content
to that adapter). No external PASS is claimed. Local consensus:
`DESIGN_REVIEW_PASS_WITH_FOLLOWUPS`; implementation remains separately
authorized work.

### Implementation and cross-review

Implemented locally in:

- `hermes_cli/kanban_swarm.py`: contract-scoped output policy, narrow marker
  detection, balanced quote/bracket validation, immutable raw-text boundary,
  and additive `char_count`/`format_valid`/reason metadata.
- `tools/kanban_tools.py` and `hermes_cli/kanban.py`: both completion paths
  apply the same validator and persist validation metadata; malformed worker
  summaries and synthesizer results are rejected rather than silently edited.

Implementation review used two independent local passes:

- Output-contract/backward-compatibility pass: **PASS after correction**. It
  required policy fields to be explicit in the swarm contract and required
  metadata-only worker completion to remain valid.
- Safety/audit pass: **PASS after correction**. It required preserving raw
  lane text, avoiding a global Telegram sanitizer, and adding task-scoped
  toolset handling to the separate P1 ticket; all are recorded in code/tests.

Validation:

- output/context targeted suite: `51 passed`;
- Kanban/compression integration sweep: `268 passed, 1 skipped`;
- `compileall`: passed;
- `git diff --check`: passed.

The live four-lane production-style synthetic run and deployment are still
follow-up gates; this ticket is not marked deployed. Model creativity remains
an explicit out-of-scope limitation.
