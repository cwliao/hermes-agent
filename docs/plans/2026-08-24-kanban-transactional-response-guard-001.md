# KANBAN-TRANSACTIONAL-RESPONSE-GUARD-001

Status: IMPLEMENTED_PENDING_COMMIT_AND_DEPLOY
Date: 2026-08-24
Type: ticket
Target repo: hermes-agent
Priority: P1

## Incident

During a four-lane Kanban request, one `kanban_create` call succeeded and the
next failed (`title is required`). The finalization guard logged a blocked
decision because there was no successful swarm receipt, but Telegram had
already received streaming edits containing the model's claim that both
tasks were created.

The durable final message and the user-visible streaming message therefore
disagreed. The guard was logically invoked; its delivery boundary was too
late.

## Design

Kanban mutation responses are transactional from the user's perspective:

1. Buffer or disable final assistant text streaming for a request that
   requires a real Kanban mutation.
2. Release the model text only after the current turn has a successful,
   structurally valid receipt.
3. On a known tool failure or partial mutation, deliver only an explicit
   blocked/error response. Never claim IDs or results not present in the
   receipt.
4. Keep the existing no-receipt nudge bounded and never replay a mutating
   tool call automatically.
5. Preserve streaming for ordinary non-Kanban turns.

## Acceptance criteria

- [x] A failed `kanban_create`/`kanban_swarm` receipt cannot stream a success
  narrative to Telegram before finalization.
- [x] A partial mutation is reported as partial/blocked, not as full success.
- [x] A valid current-turn swarm receipt still delivers the final response.
- [x] Existing replay, empty-response, and ordinary streaming behavior is
  unchanged.
- [x] Tests cover streamed-before-finalization failure, success receipt, and
  old-turn receipt isolation.
- [x] Logs include a specific mutation-failure reason and delivery decision.
- [ ] A real Telegram repetition confirms that failed calls are not shown as
  successful; a successful swarm confirms normal delivery.

## Implementation

- `agent/kanban_execution_guard.py`: added explicit same-turn failed mutation
  receipt detection and `reason=known_mutation_failure`; successful receipts
  remain structurally validated and old-turn receipts remain excluded.
- `gateway/run.py`: classifies mutation-shaped Kanban requests before creating
  the stream consumer and disables early text/interim delivery for those turns.
  Read-only Kanban questions remain on the ordinary streaming path.
- `tests/run_agent/test_kanban_execution_guard.py`: covers mixed successful +
  failed mutation receipts.
- `tests/gateway/test_kanban_transactional_response.py`: covers the exact
  four-lane prompt, ordinary/read-only text, and ordinary task creation.

## Review record

Local review A: **PASS** — the stream gate runs before
  `GatewayStreamConsumer` construction; finalization remains the only place
  that accepts a receipt.

Local review B: **PASS** — a partial mutation cannot pass merely because one
  sibling call succeeded; no mutating tool is automatically replayed.

External reviewers: **BLOCKED_BY_RUNTIME_POLICY** — Codex nested app-server,
  Claude session persistence, Grok session creation, and AGY log/socket setup
  were all blocked by this execution environment's read-only filesystem or
  socket policy. No external PASS is claimed.
