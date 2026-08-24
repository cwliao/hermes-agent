# KANBAN-SWARM-TERMINAL-CONTRACT-RETRY-001

Status: DEPLOYED_PENDING_TELEGRAM_E2E
Date: 2026-08-24
Type: ticket
Target repo: hermes-agent
Priority: P1

## Incident

Telegram inbound and outbound delivery succeeded for the autumn four-lane
request (`ed00c08ea68d4951`, outbound message `2339`). The Kanban result was
not complete. The root card completed, but one worker stopped with a missing
PNG handoff, three workers remained running, and verifier/synthesizer cards
never reached a runnable state. The claimed `/tmp` artifacts were absent.

## Evidence-backed RCA

The dispatcher did spawn four workers. The failure was downstream of spawn:

- worker completion calls were rejected by the fail-closed swarm contract;
  the live log shows role, root, skill, ownership, and artifact/evidence
  failures across repeated retries;
- the model created an unrelated `triage` child for missing `jokes.json`,
  which did not satisfy the lane's completion contract;
- PNG generation attempted ImageMagick/Graphviz paths, but `dot` was not
  installed and the claimed PNG was never created;
- verifier `t_a5fb8b75` and synthesizer `t_9d7616cd` had no runs. Their lack
  of execution was a correct parent gate result, not evidence that Telegram
  or the dispatcher was dead.

## Fix

`validate_completion()` still fails closed, but now aggregates all contract
defects into one actionable tool error when more than one field is wrong. A
single-defect error keeps its existing text for compatibility. This avoids
the observed one-field-per-turn retry spiral without auto-filling trusted
identity or evidence fields.

The ticket also adds a regression test for the multi-defect response and
records the live RCA so a model-facing “completed” message cannot close this
incident without durable task/artifact evidence.

## Acceptance criteria

- [x] Contract validation remains fail-closed.
- [x] Multiple metadata/output defects are returned together.
- [x] Existing single-defect error text remains compatible.
- [x] Regression test covers role, root, lane, skill, output, and verification
  defects in one response.
- [x] Run the focused test suite: 37 focused + 78 cross-review tests passed.
- [x] Deploy immutable release `v2026.8.24-kanban-swarm-contract-retry-c53d2ac43a`
      with pinned venv `gateway-c53d2ac43a` and effective systemd drop-in 87.
- [ ] Run one real Telegram retry.
- [ ] Verify task runs, verifier/synthesizer execution, durable attachments,
  and outbound Telegram delivery against the Kanban DB/logs.

## Cross-review

Local review A: **PASS** — this changes only diagnostic feedback;
 it does not relax role/root/lane/skill/result/evidence gates.

Local review B: **PASS** — one-defect callers retain the previous
 error string; multi-defect callers receive a complete correction shape and
 the task remains in-flight for retry.

Cross-review consensus: **APPROVE** — local Codex read-only review found no
P0/P1/P2 findings and requested no further implementation change. It confirmed
that aggregation only changes diagnostics: every contract defect still rejects
completion, while single-defect messages remain compatible.

Claude/Grok/AGY review: **BLOCKED_BY_PRIVATE_REPO_POLICY** — the execution
policy refused to send this private working tree to those external CLI
reviewers. No external PASS is claimed. Live runtime validation remains a
separate acceptance item.
