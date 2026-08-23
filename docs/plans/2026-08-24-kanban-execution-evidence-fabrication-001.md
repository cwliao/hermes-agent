# KANBAN-EXECUTION-EVIDENCE-FABRICATION-001

Status: deployed; Telegram acceptance pending

## Incident

After `136c096b9c` was deployed, the four-lane autumn-joke request no longer
called `kanban_block` on the old terminal task, but the model still returned a
fabricated execution plan. The response named `t_l3n98rub` and `t_f6ry2vln`,
although neither task existed and the current turn contained no
`kanban_swarm`, `kanban_create`, or successful mutation receipt.

The persisted assistant row was inspected directly in `state.db`. The role
names were stored as the literal two-character escape `\\0` followed by a name;
the row contained zero actual NUL bytes. No matching role names or `\\0` source
were found in the repository, release snapshot, or runtime config. This is
treated as model-generated malformed prose, not as evidence of a binary data
corruption path.

The same turn first hit a separate provider-budget failure:

```text
2026-08-24 03:44:38
HTTP 400: maximum context length 65536; requested 65536 output tokens;
prompt contains 119370 characters
```

Compression then rotated the session and the model produced the fabricated
890-character answer. This context-budget issue remains a separate follow-up
runtime item and must remain visible during end-to-end verification.

## Root cause

`model_replay_guard` is intentionally scoped to exact stale Webboard answers
with a tool-backed baseline. A new Kanban request has no prior baseline to
compare, so `no_exact_tool_backed_candidate` correctly passed. Nothing at the
finalization boundary required a real Kanban mutation receipt before allowing
task IDs and lane results into the user-visible answer.

## Implemented design

`agent/kanban_execution_guard.py` adds a narrow deterministic gate for the
explicit four-lane request shape:

1. The current user turn must mention the four known lanes, independent output,
   and a verifier/synthesizer or Kanban stage.
2. A successful current-turn `kanban_swarm` receipt must contain `ok=true`, a
   root ID, at least four worker IDs, a verifier ID, and a synthesizer ID.
3. With no mutation call, the model gets one internal execution nudge.
4. A failed/incomplete mutation receipt, a second no-receipt answer, or an
   unavailable tool fails closed with a blocked message. The guard never
   retries an uncertain mutation.
5. Actual NUL bytes and the incident's literal `\\0`-identifier escape are
   rejected at the user-visible boundary.

The synthetic nudge is registered with the existing compression and finalizer
scaffolding filters so it cannot become future evidence or context pollution.

## Acceptance criteria

- [x] Guard is called from the real conversation finalization path.
- [x] Fake task IDs without a tool receipt cannot be delivered.
- [x] Old-turn receipts cannot satisfy a new request.
- [x] Failed mutation receipts fail closed without a duplicate mutation retry.
- [x] Successful four-lane `kanban_swarm` receipts are accepted.
- [x] Literal `\\0` and actual NUL output are not delivered.
- [x] Focused Kanban/replay tests pass.
- [x] Commit and push to `origin/main` (`6684cf86ec`).
- [x] Deploy an immutable release and verify the release SHA/systemd drop-in.
- [ ] Repeat the exact prompt in Telegram.
- [ ] Verify `kanban list` contains the newly created root, four worker tasks,
  verifier, and synthesizer; verify the four worker runs have real run/event
  records before marking this ticket complete.
- [ ] Track or fix the separate 65536-output-token local vLLM budget issue
  before declaring the runtime fully healthy.

## Review record

Local review A: pending final diff.

Local review B: pending final diff.

External Claude/AGY review: not used for the uncommitted diff because the
security policy previously blocked transmitting private repository content.
