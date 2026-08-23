---
title: "Model stale-answer replay reliability"
status: IMPLEMENTED_CROSS_REVIEW_PASS_PENDING_DEPLOYMENT
date: 2026-08-23
type: reliability-ticket
ticket: MODEL-REPLAY-RELIABILITY-001
target_repo: hermes-agent
---

# MODEL-REPLAY-RELIABILITY-001

## Problem boundary

This ticket is intentionally separate from
[`SESSION-TRANSCRIPT-REPLAY-001`](2026-08-23-session-transcript-replay-idempotency-001.md).
The latter fixes database-level duplicate rows and durable `_row_id` identity.
This ticket covers the remaining production symptom: with a clean context and
no duplicate SQL rows, the selected model can return the previous turn's final
answer byte-for-byte instead of executing the current task again.

## Evidence

- A real Telegram Webboard retest used the affected session
  `20260821_163406_895ea0` and the same trigger as the original incident.
- The returned answer was identical to the pre-fix answer, including the
  embedded timestamp `08:03:29`.
- The result indicates that the model did not emit the corresponding tool
  call and copied the prior answer instead.
- The independent verification of SESSION-TRANSCRIPT-REPLAY-001 found zero
  duplicate non-empty assistant rows in `state.db` from approximately 11:20
  until the verification, so this reproduction is not explained by the fixed
  SQL-row duplication mechanism.
- The active local vLLM route is `drafter-active`; its reliability is already
  being tracked separately. This ticket may add a runtime guard or fallback,
  but must not claim that a database fix changes model capability.

## Objective

Prevent an unchanged prior answer from being accepted as a verified result of
the current action-oriented turn when the model emitted no tool call. Preserve
legitimate repeated answers for genuinely informational requests, and make a
remaining model failure explicit rather than silently reporting stale work as
completed.

## Non-goals

- Do not alter `_row_id` persistence or Telegram ingress dedupe.
- Do not compare all assistant text globally and reject every repeated phrase.
- Do not use text similarity alone to infer that a tool should have run.
- Do not mutate or delete historical production transcript rows.
- Do not hide the problem by blindly retrying the same model indefinitely.
- Do not claim that a model-level guard is a replacement for selecting a more
  reliable model; `drafter-active` model evaluation remains an operational
  follow-up.

## Candidate solution to review

Implement a narrow, bounded finalization guard with these gates:

1. The current turn has no executed tool call.
   "No executed tool call" is eligible only when the turn execution telemetry
   is complete and explicitly records zero calls. Missing, partial, or unknown
   telemetry makes the guard ineligible; it must not be interpreted as proof
   that no call happened.
2. The current user request and the immediately preceding user request have
   the same deterministic, versioned normalized action identity. The identity
   may use only existing explicit execution signals (for example the
   configured tool-use/execution-guidance mode plus existing tool-workflow
   metadata); this ticket must not add a new semantic intent classifier. If
   the identity fields are missing, the normalization version differs, or
   parsing fails, the guard is ineligible and performs no automatic retry.
3. The current visible answer is byte-identical after removing only transport
   noise to the immediately preceding assistant answer.
4. The previous turn had ground-truth tool execution metadata, and every
   previous tool is classified by existing tool guardrail metadata as
   read-only or explicitly idempotent. If any previous tool is mutating,
   side-effect status is unknown, or execution telemetry is missing, no
   automatic re-execution is allowed.

When all gates hold, transition through this bounded state machine:

`eligible` → `nudged` → `executed`/`recovered`, or
`fallback_applied`, or `unverified`/`blocked`.

The `nudged` message must be an internal synthetic recovery pair that preserves
role alternation, is excluded from provider-independent durable transcript
rows, and does not alter the stable system prompt or cache prefix. It tells
the model to re-evaluate the current request, execute the required read-only
or idempotent tool, and not copy the previous answer. It may happen once per
logical turn. A recovery is accepted only when ground-truth execution metadata
shows a new invocation ID; a different answer string alone is insufficient.

If the same model still returns the same answer without a new invocation, use
the configured model/provider fallback once when available. The fallback must
start a distinct provider invocation and inherit the same side-effect safety
fence. If no fallback exists or the fallback also cannot produce a new
verified invocation, return an explicit `unverified`/`blocked` result and log
only redacted provider/model/status metadata.

The implementation contract is versioned and explicit:

- The execution evidence schema is `tool_execution_v1`. It must identify the
  logical turn, session/branch, invocation ID, tool name, registry version,
  completion state, and side-effect classification. Missing fields, delayed
  events, duplicate/reused invocation IDs, or an unsupported schema version
  make the retry ineligible.
- `tool_execution_v1.complete=true` is authoritative only after the turn
  coordinator has frozen the expected worker set at turn start, observed a
  terminal event from every expected worker, joined them, and durably emitted
  the turn-end cutoff sequence. `calls=[]` plus that matching cutoff proves
  zero calls; absence of the cutoff, a timeout before the join, or any late
  event makes the turn ineligible rather than proving zero. Events after the
  cutoff are rejected by sequence/turn fencing. Each worker terminal event is
  first durably recorded with a unique worker/event sequence; duplicate events
  are ignored. Only after the closed-world set is complete does one atomic
  transaction commit the join marker and cutoff sequence, so no cutoff can
  precede a worker terminal record.
- The trusted closure record is an immutable `closure_v1` event written by the
  turn coordinator, not by the model or provider response parser. It contains
  the frozen worker-set digest, expected/observed terminal counts, generation,
  cutoff sequence, and `zero_calls_proven` boolean. A unique constraint on
  `(logical_turn_key, generation, closure_kind)` permits exactly one closure;
  the guard accepts zero calls only from this committed record.
- The action identity schema is `action_identity_v1`. Its canonical form must
  use RFC 8785 JSON Canonicalization Scheme over a frozen snapshot of existing
  structured execution metadata, reject duplicate JSON keys and non-finite
  numbers, and record the metadata provenance plus schema version. It must not
  introduce a semantic LLM classifier or persist raw prompt text.
- Read-only/idempotent status comes from a versioned existing tool registry.
  Unknown tools, stale registry versions, and missing side-effect metadata
  never enter `eligible`.
- A stable logical-turn key is recorded in an atomic attempt ledger with
  states and compare-and-set transitions. The ledger uses a durable SQLite
  transaction with a unique logical-turn key and a claim token: a claimant
  atomically reserves `nudged` or `fallback_applied`, and a second claimant
  must observe the committed state and cannot reserve the same attempt. Crash
  recovery marks an interrupted claim `unknown_dispatch`; lease expiry cannot
  reopen it or increase either counter. It permits at most one `nudged` attempt and
  one `fallback_applied` attempt across crash, timeout, duplicate delivery,
  and concurrent processes. It is bookkeeping, not a transcript row, and
  contains no raw user content.
- A fallback invocation without a fresh `tool_execution_v1` receipt is
  terminal `unverified`/`blocked`; a changed answer, provider success status,
  or reused invocation ID is never sufficient evidence of recovery.
- Every receipt must bind to the same session ID, branch ID, logical-turn key,
  action-identity version/value, tool-registry version, and synthetic lineage.
  It must also bind the attempt's claim token, lease/generation fence, and
  synthetic lineage. An invocation ID is generated before the atomic claim,
  globally unique for the runtime (UUID plus process epoch is acceptable), and
  unseen before the attempt. The claim stores the ID, claim token, generation,
  and lineage before dispatch. A receipt is `fresh` only when all of those
  values match the claimed attempt, its invocation ID is new, and its event
  sequence is after the claim commit and before the terminal cutoff. A crash
  after claim but before dispatch/receipt is `unknown_dispatch` and is terminal
  `unverified`/`blocked`; it forbids fallback and lease expiry cannot reopen
  the flow, because dispatch may have happened. Late or duplicate receipts
  after a valid receipt are ignored without changing the valid outcome. Late
  receipts without a valid receipt, cross-branch receipts, stale registry
  receipts, reused IDs, stale generations, and mismatched claim tokens are
  rejected and produce the terminal unverified/blocked state.
- The nudge is a structured internal control event, not an ordinary user
  message. Its role-alternation representation must be stripped before
  durable transcript projection and excluded from action adjacency and
  identity calculations.
- Normalization is applied in this exact order: strip ANSI control sequences,
  using the approved CSI/OSC escape grammar; convert CRLF to LF, apply Unicode
  NFC, then trim only outer whitespace. The exact grammar and order are fixed
  in the implementation contract and tested as `normalization_v1`.
  Fallback is a separate provider invocation carrying a fresh attempt claim
  and the same receipt/fence requirements; provider success without a bound
  fresh receipt is terminal `unverified`/`blocked`.

The adjacent-turn and transition contracts are fixed as follows:

- `adjacent_v1` selects the latest non-synthetic user message in the same
  session/branch and the next assistant completion before any later user,
  branch fork, compression truncation, or replay boundary. Synthetic control
  events are excluded from both adjacency and identity. The nudge/fallback
  lineage is a ledger identifier, not transcript content.
- `eligible` can claim `nudge` only when all gates and the atomic ledger CAS
  succeed. `nudged` can become `executed/recovered` only with a fresh bound
  receipt. If the nudge provider invocation reaches a confirmed terminal
  cutoff with complete telemetry but no receipt, it may claim the single
  `fallback` transition. A claim/dispatch crash, timeout before the cutoff,
  or incomplete telemetry becomes `unknown_dispatch` and permanently forbids
  fallback. `fallback_applied` can become `executed/recovered` only with its
  own fresh bound receipt; otherwise it becomes terminal `unverified/blocked`.
  `recovered`, `unknown_dispatch`, `unverified`, and `blocked` are terminal
  and cannot transition back into retry. Counters never increase during lease
  expiry or recovery.
- The attempt bound is per logical-turn key: at most one nudge claim and at
  most one fallback claim for that turn, regardless of provider invocation,
  process, retry, or lease count. A fallback claim is impossible from
  `unknown_dispatch`.
- The tool registry is immutable for a registry version and carries a digest;
  a receipt must match both the version and digest captured by the claim.

The only permitted attempt transitions are:

`eligible → nudge_claimed → nudge_dispatched → nudge_terminal_no_receipt →
fallback_claimed → fallback_dispatched → recovered|unverified|blocked`.

At any claim or dispatch boundary, a crash, timeout, missing closure, or
incomplete telemetry instead transitions to the absorbing
`unknown_dispatch → unverified|blocked` path; `unknown_dispatch` has no other
legal exit and cannot claim fallback. `nudge_dispatched → recovered` requires
a fresh receipt. A confirmed `closure_v1` with zero receipts may enter
`nudge_terminal_no_receipt` and claim the one fallback.
`fallback_dispatched → recovered` likewise requires a fresh receipt; otherwise
it terminates in `unverified|blocked`. `recovered`, `unverified`, and `blocked`
are terminal. No other transition is valid, and the ledger CAS rejects it.

The fallback claim is an atomic guard in the same SQLite transaction that reads
the closure: it succeeds only when the closure is finalized, its
`zero_calls_proven` flag is true, no valid receipt exists for the nudge, the
ledger is exactly `nudge_terminal_no_receipt`, and no post-cutoff event is
present. The transaction also reserves the fallback claim token before any
provider dispatch; a later event cannot retroactively make an invalid claim
valid.

The cutoff transaction must verify, under the same SQLite write lock, that the
frozen expected-worker set exists, every expected worker has exactly one
terminal event matching the turn generation, no worker has a duplicate event
sequence, and no cutoff row already exists. A unique constraint on
`(logical_turn_key, generation, worker_id, event_sequence)` plus a unique
`(logical_turn_key, generation, cutoff_kind)` constraint makes the join/cutoff
commit idempotent and race-safe.

The implementation must define how the previous completed user/assistant pair
is selected across tool results, compression, cold resume, and Telegram
delivery. "Immediately preceding" means the same session and branch, with
system/developer/tool messages and known synthetic recovery messages excluded;
it must never cross a session/branch boundary or a context truncation boundary.
It must not treat a legitimate repeated factual answer as stale merely because
the text matches.

The guard must not use a new broad semantic classifier merely to make the
trigger appear robust. It should reuse an existing explicit action/intent
signal and a deterministic normalized action identity. If that identity is
not available, the guard must fail open for this ticket and record an
unverified diagnostic rather than guessing. Transport normalization is
versioned and limited to ANSI removal, CRLF→LF conversion, Unicode NFC, and
outer whitespace trimming; it must not collapse internal whitespace, change
case, or use fuzzy similarity. Exact normalized answer identity is safer than
semantic matching, and fuzzy matches must never cause automatic execution.

Automatic re-execution is unsafe for non-idempotent side effects. The existing
tool guardrail classification is the source of truth: only its read-only or
idempotent set can enter `eligible`. A new invocation ID and, when supported
by the tool, an operation/idempotency key must be observable for every
permitted retry. Missing telemetry is fail-closed for the retry, not evidence
that no side effect occurred. The same fence applies to fallback: a fallback
must not select a mutating/unknown-side-effect operation or proceed with
incomplete telemetry.

## Required cross-review questions

1. Are the four gates sufficient to avoid false positives for repeated factual
   questions, confirmations, acknowledgements, and idempotent commands?
2. Is the internal nudge compatible with Hermes's role alternation and prompt
   caching rules, and is one retry enough to avoid a retry loop?
3. Should fallback happen before or after the bounded same-model retry, and
   how should the result be marked when both models repeat the answer?
4. Can the guard operate without persisting raw user text, secrets, or a
   linkable unkeyed digest in logs or durable transcript metadata?
5. Which existing action/intent signal can be reused instead of introducing a
   speculative classifier?
6. What real end-to-end test proves that a tool is executed again, rather than
   merely producing a different answer string?

## Acceptance criteria

- A deterministic regression reproduces a model returning the previous answer
  without a tool call and proves that Hermes performs at most one bounded
  re-execution attempt.
- The re-execution path proves a tool call is emitted and executed when the
  model recovers; the stale answer is not accepted as the current result.
- A repeated informational question with the same answer is not retried or
  blocked when no action/tool intent is present.
- A repeated idempotent command with a legitimate no-op result is not falsely
  classified as stale.
- Same-model retry and configured fallback both have bounded, observable
  outcomes; no indefinite loop is possible.
- The complete state machine enforces at most one nudge and one fallback
  invocation per logical turn, even when the answer text changes without a
  new ground-truth invocation.
- Failure after the bound is surfaced as `unverified`/`blocked`, not as a
  fabricated successful completion.
- No raw prompt, credential, or user content is added to durable diagnostics.
- Existing persistence, Telegram, replay, tool-loop, and model-routing tests
  remain green.
- Tests prove that missing/partial telemetry, mutating or unknown-side-effect
  tools, session/branch mismatch, context truncation, normalization-version
  mismatch, and stable system/cache-prefix mutation all prevent retry.
- Tests prove stale/reused invocation IDs, cross-session/branch receipts,
  fallback terminal-without-receipt, registry-version mismatch, crash/timeout,
  duplicate/concurrent delivery, and the atomic one-nudge/one-fallback ledger
  bound.
- A real Telegram Webboard retest of session
  `20260821_163406_895ea0` or a fresh equivalent proves a new tool invocation
  and a result with a new runtime timestamp/trace, or records a model-level
  failure with the explicit unverified status.
- Independent cross-review passes before merge, deployment, or closure.

## Cross-review disposition

The first external design cross-review returned **CHANGES_REQUIRED**. It
required a canonical action identity rather than vague semantic equivalence,
ground-truth execution metadata rather than model text, explicit handling of
non-idempotent or partially completed side effects, a bounded retry/fallback
state machine, and E2E evidence of a new tool invocation ID.

The second external design cross-review independently returned
**CHANGES_REQUIRED**. It confirmed the same risks and additionally required
that internal nudges preserve role alternation and prompt-cache boundaries,
remain invisible/non-durable, and expose states such as `executed`, `nudged`,
`fallback_applied`, `unverified`, and `blocked` for production diagnosis.

The design is revised with the consensus constraints: complete versioned
ground-truth telemetry, deterministic versioned action identity, explicit
session/branch boundaries, narrowly defined transport normalization,
versioned tool-registry fencing, a stable-turn atomic attempt ledger,
synthetic structured non-durable control events, explicit invocation limits,
and invocation-ID-based E2E proof. Final Codex cross-review returned
**PASS** after these corrections; AGY's final review also returned **PASS**.
The design is internally consistent and approved for implementation, subject
to preserving the stated durability and fail-closed fences in code.

## Implementation and independent review evidence

Implemented locally in the Hermes development repository:

- `agent/model_replay_guard.py`: `normalization_v1`, `action_identity_v1`,
  explicit execution-mode gate, adjacent-turn candidate selection, and
  idempotent-only eligibility.
- `hermes_state_common.py` / `hermes_state.py`: durable
  `model_replay_attempts` ledger with `BEGIN IMMEDIATE` CAS transitions,
  `closure_v1`/cutoff fields, registry version/digest fencing, claim tokens,
  pre-generated invocation IDs, and an atomic fresh-receipt commit that rejects
  duplicate, late, cross-branch, or mismatched receipts.
- `agent/conversation_loop.py`: one synthetic nudge, one fallback at most,
  mutating/unknown recovery calls blocked before dispatch, and explicit
  `unverified`/`blocked` delivery when no fresh receipt is proven.
- Persistence/compression/finalizer filters now exclude the replay guard's
  synthetic pair from durable transcript rows.

Validation completed locally:

- replay-guard unit and SQLite ledger tests: **5 passed**;
- vLLM step3p5 regression tests: **12 passed**;
- existing SessionDB write-lock tests: **6 passed**;
- existing turn-finalization regression tests: **1 passed**;
- Python compilation and `git diff --check`: passed.

The final independent Codex design cross-review returned **PASS** after the
receipt-binding, registry-fence, closure/cutoff, and side-effect-blocking
corrections. AGY was attempted for a second live implementation review but its
headless command permission was unavailable, so no AGY implementation verdict
is claimed here.

## Current disposition

Ticket opened from the failed independent E2E verification of
SESSION-TRANSCRIPT-REPLAY-001. The implementation is cross-reviewed locally
and pending commit, deployment, and the real Webboard acceptance test. This
ticket does not claim production deployment or closure until those checks pass.

## Production retest finding after `3b8765a825`

The first implementation was deployed as release `3b8765a825` and the same
Telegram trigger was retested on `2026-08-23` using the affected session
`20260821_163406_895ea0`. The retest still returned the prior Webboard answer,
including timestamp `08:03:29` and leaked `AGENTS.md` content. Runtime showed
`tool_turns=2`, but there was no tool executor call for that turn.

The guard was present and its finalization call site was reached in the
deployed release, but the real path silently returned `pass` for two reasons:

1. `drafter-active` used `tool_use_enforcement=auto` and
   `execution_guidance=auto`, so the original explicit-action metadata gate was
   absent even though the runtime had a concrete tool-capable surface.
2. Compression rotated the turn into
   `20260823_192418_3bd23f`. The child transcript retained duplicate
   `Webboard` user/assistant projections. The successful `terminal` report was
   earlier in that child transcript, while the immediately preceding duplicate
   answer had no tool call. The adjacency-only scan therefore found no
   candidate. A failed phantom `webboard` call was also present beside the
   real report.

The corrective implementation remains fail-closed while covering this path:

- a deterministic tool-capable surface plus the current request supplies an
  action identity when explicit execution guidance is `auto`;
- the exact normalized answer is searched through the compression-preserved
  lineage for an earlier same-action answer with a successful tool result;
- failed or duplicate tool outputs are excluded from execution evidence;
- only the exact `hermes_webboard_report.sh` terminal command is treated as
  read-only; arbitrary terminal/unknown tools and missing tool results become
  `blocked`, never automatic re-execution;
- every finalization decision emits a redacted `model_replay_guard` audit log,
  including `invoked`, `pass` reasons, `nudge`, `fallback`, and `blocked`.

The corrected code was reproduced against the actual rows in
`20260823_192418_3bd23f`: it finds the earlier terminal receipt and marks it
`recovery_safe=True`. Six replay-guard tests, 12 vLLM regression tests, and
nine compression/session tests pass. The fix still requires deployment and a
fresh same-session Telegram retest showing the runtime audit decision and a
new tool invocation before this ticket can close.

## Baseline-less lineage finding and corrective design

The next production retest used the rotated lineage
`20260821_163406_895ea0` → `20260823_192418_3bd23f` →
`20260823_195406_62658f`. The deployed guard was invoked, but correctly logged
`decision=pass reason=no_exact_tool_backed_candidate`: no message in that
lineage was a clean, tool-backed baseline. The final response was nevertheless
the unchanged `08:03:29` Webboard answer containing leaked `AGENTS.md` text.

The three contaminated IDs were drained and deleted through the official
session CLI, then the gateway was restarted. Deleting all three was necessary
because deleting only a root or only the latest session leaves compression
children available for recovery. Verification found no rows for those IDs.

The corrective design adds an explicit `FRESHNESS_REQUIRED_ACTIONS` registry,
currently containing only the exact normalized Telegram action `Webboard`.
When that action has a terminal tool surface but no tool-backed baseline, the
guard claims one bounded fresh-execution nudge with reason
`baseline_missing_exact_live_report_action`. A non-tool answer after the nudge
is blocked; a receipt is required. This is an exact action rule, not a prose,
timestamp, or statistics heuristic, and it does not apply to arbitrary text or
non-Telegram platforms.

The exact read-only Webboard terminal command is now checked by the same safety
predicate at both candidate detection and fresh-receipt validation. This avoids
classifying the command as safe in one layer while rejecting its actual receipt
in the next layer. Ten replay-guard tests and the combined replay/vLLM/
compression/session regression set (35 tests) pass locally. Production deploy
and a new real Telegram Webboard acceptance test remain pending; the ticket
must not be closed until logs show the baseline-less nudge or blocked decision
and the final response is demonstrably fresh.

## Production acceptance after `a780bd4772`

The immutable release `v2026.8.23-model-replay-a780bd4772` was activated with
gateway PID `1595835` and full release SHA
`a780bd4772d36ba1971907d9407efb0e785d3abf`. A real Telegram Webboard message
was accepted at `2026-08-23 20:18:37 CST` in the same chat. The resulting clean
turn used session `20260823_195406_62658f`, executed the exact read-only command
`bash ~/.hermes/scripts/hermes_webboard_report.sh`, and received a tool result
timestamped `2026-08-23 20:18:47 CST`.

The final 422-character Telegram response was delivered successfully and did
not contain the stale `08:03:29` timestamp or `AGENTS.md` content. Runtime
reported `tool_turns=1`. The guard emitted `decision=invoked` followed by
`decision=pass reason=no_exact_tool_backed_candidate`; this pass is correct for
this turn because a real tool receipt was already present, so the guard did not
need to nudge. The state.db transcript contains the user message, terminal
call, terminal result, and fresh assistant answer with no old replay rows.

This completes the requested real Telegram acceptance for the deployed fix.
The guard remains intentionally fail-closed for a future baseline-less exact
Webboard turn: if the model returns no tool call, the new freshness candidate
path must nudge or block instead of accepting the answer.
