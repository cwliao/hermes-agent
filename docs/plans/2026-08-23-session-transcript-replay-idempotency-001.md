---
title: "Session transcript replay and Telegram inbound idempotency"
status: IMPLEMENTATION_REVIEW_PASS
date: 2026-08-23
type: implementation-ticket
ticket: SESSION-TRANSCRIPT-REPLAY-001
target_repo: hermes-agent
---

# SESSION-TRANSCRIPT-REPLAY-001

## Objective

Prevent Hermes from re-inserting an already persisted conversation into the
active session after compression, cold resume, cache eviction, or a repeated
native Telegram delivery. The failure currently makes the model repeat stale
tool results and produce answers that look hallucinated.

## Evidence

- Production session `20260821_163406_895ea0` received one Telegram `Webboard`
  update at 2026-08-23 08:10:10 (`update_id=714524425`).
- Rows 7910–7916 in `state.db` duplicated rows 7902–7908 byte-for-byte,
  including the original timestamps. Row 7918 then repeated the old dashboard
  result without running the current query.
- The current persistence guard relies primarily on the in-memory
  `_db_persisted` marker and Python object identity. The durable `_row_id`
  already carried by gateway-loaded histories is not used by
  `run_agent.py::_flush_messages_to_session_db`.
- The native Telegram text path builds a stable `message_id` but has no bounded
  normal-path inbound seen-set. Relay has its own dedupe, but native Telegram
  does not use that path.
- Session compression also reported a configured `groq` provider without an
  available API key; this is a contributing context-growth failure, not a
  substitute for transcript idempotency.

## Scope

1. Make the agent flush recognize durable `_row_id` identities so a cold or
   copied history cannot be appended as fresh rows.
2. Preserve legitimate in-place rewrites: when an already persisted live dict
   is intentionally changed, its rewrite path must explicitly clear the
   durable row identity before appending the replacement row. `_row_id` is a
   per-message identity guard, not a high-water mark or an update ledger.
3. Add bounded native Telegram inbound dedupe keyed by platform/chat/message
   identity, including deterministic tests for duplicate polling delivery and
   held-event redispatch.
4. Add regression tests reproducing the stale-tool-result replay shape.
5. Document the missing compression-provider credential as an operational
   follow-up; this ticket does not silently alter the user's fallback policy or
   runtime secrets.

## Out of scope

- No force push, upstream push, or production gateway restart in this ticket.
- No deletion of existing `state.db` rows. Any contaminated session cleanup
  requires a separate backup-and-reset operation.
- No automatic removal of `groq` from `config.yaml`.
- No outbound Telegram send ledger, global high-water mark, or text-content
  dedupe. Those are different correctness problems and would risk suppressing
  legitimate replies or split-message chunks.

## Acceptance criteria

- A history loaded from SQLite, copied into a fresh list/dict structure, and
  flushed again creates zero duplicate active rows.
- A newly generated user/assistant/tool tail still persists exactly once.
- A deliberate persisted-row rewrite remains durable: its explicit rewrite path
  clears `_row_id`, so the replacement row is appended and is not dropped by
  the identity guard.
- Repeated native Telegram delivery of one `(chat_id, message_id)` reaches the
  gateway once; two different message IDs remain independent turns.
- Existing persistence, compression, Telegram batching, and delivery tests
  remain green.
- A second independent review records no unresolved high-severity correctness
  or data-loss issue before merge/deployment.

## Review notes

GitHub Issues are disabled on `cwliao/hermes-agent`; this committed plan is the
repository's local ticket record. Upstream remains download-only and is not
used as a write target.

### Independent cross-review disposition

The independent reviewer returned **No-Go** against an earlier, broader
interpretation that treated `_row_id` as a durable event high-water mark and
asked for an outbound Telegram send ledger. That objection is valid for that
design, but it does not apply to this narrower fix: `_row_id` is used only to
recognize an already-loaded SQLite message during transcript append, while
known rewrite sites explicitly remove it before persisting a replacement.
Native Telegram dedupe is ingress-only and bounded by `(chat_id, message_id)`;
it does not dedupe text, batching continuations, or outbound sends. The
implementation must retain this boundary and add regression tests for both
the cold/copy-history case and the rewrite case.

The follow-up cross-review found one additional P1: the normal gateway
`load_transcript()` path was still dropping `_row_id` before the agent flush,
so the first implementation only protected the lease-wait reload path. The
implementation now requests row IDs on the gateway agent/recovery loads and
preserves them through `_build_replay_entry`; provider serialization still
strips them. The reviewer also found two synthetic Telegram fixtures that
reused one message ID; those fixtures now use distinct IDs for distinct
chunks/photos. Targeted validation after these corrections passed.

### Implementation and final cross-review

Implemented in the working tree:

- Durable `_row_id` propagation through normal gateway cold-resume/recovery
  loads, with provider-bound serialization stripping the bookkeeping field.
- Explicit `_row_id` invalidation for intentional rewrites, compaction copies,
  micro-compaction defrag, final empty-content replacement, and max-iteration
  summaries.
- In-place compaction rollback now restores the pre-compression durable
  snapshot when `archive_and_compact()` rolls back. It does not clear ids from
  retained rows and accidentally append them again; if the archive committed
  and only later bookkeeping failed, committed ids remain intact.
- Bounded native Telegram ingress dedupe keyed by `(chat_id, message_id)`;
  distinct message ids, long-message batching, held-event redispatch, and
  outbound sends remain independent.

Final independent implementation review: **PASS** — no actionable correctness
or data-loss issue found. The reviewer ran the relevant targeted suite and a
gateway-suite pass reached 1,197 passed and 1 skipped before the environment's
test run was interrupted.

Current local validation after the final rollback correction:

- 352 passed, 6 warnings across replay, Telegram batching/media, persistence,
  compaction, and agent regression tests.
- `git diff --check` passes.
- The warnings are existing/dependency or test-double warnings; they are not
  failures from this change.

No commit, push, production deployment, or gateway restart was performed.
