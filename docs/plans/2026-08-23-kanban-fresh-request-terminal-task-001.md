---
title: "Kanban fresh-request isolation from terminal task history"
status: IMPLEMENTED_PENDING_DEPLOY_AND_TELEGRAM_E2E
date: 2026-08-23
type: ticket
ticket: KANBAN-FRESH-REQUEST-TERMINAL-TASK-001
target_repo: hermes-agent
priority: P1
related_tickets:
  - MODEL-REPLAY-RELIABILITY-001
  - KANBAN-WORKER-CONTEXT-COMPRESSION-001
  - SWARM-OUTPUT-CONTRACT-VALIDATION-001
---

# KANBAN-FRESH-REQUEST-TERMINAL-TASK-001

## Incident

After `275c379ca7` was deployed, the four-lane autumn-joke prompt no longer
hit the worker context limit or protocol-violation path. A new Telegram turn
did, however, call `kanban_list`, find the old completed task
`t_d4611a2a`, call `kanban_block` on that `done` card, and then tell the user
that no action was required. No new swarm or lane execution was created.

This is not the exact-answer replay handled by
`MODEL-REPLAY-RELIABILITY-001`: the model did not copy a prior answer and the
missing operation is a mutating `kanban_swarm` call. Automatically re-running
that mutation from the replay guard would be unsafe and could create duplicate
work. The correction therefore belongs at the Kanban orchestration boundary.

## Design

1. Keep the human CLI's complete task-history behavior unchanged.
2. Make model-facing `kanban_list` default to active tasks only; `done` and
   `archived` history requires an explicit status filter.
3. Return scope metadata and an explicit instruction that a new user request
   requires a new task or swarm.
4. If `kanban_block` is mistakenly called for a terminal task, fail closed with
   a structured, actionable error directing the model to create a fresh task or
   swarm. Never mutate the terminal card and never report the new request as
   already complete.
5. Add orchestrator guidance stating that similar titles and old terminal cards
   are not proof that the current request is finished.

## Safety boundary

The replay guard remains responsible for exact stale answer text and fresh
tool receipts. It is deliberately not expanded to auto-retry `kanban_swarm`,
because that operation creates side effects. This ticket uses deterministic
tool visibility and terminal-state errors instead of a semantic classifier.

## Acceptance criteria

- Default model-facing `kanban_list` never returns `done`/`archived` tasks.
- Explicit `status=done` or `status=archived` still supports historical
  inspection.
- A terminal `kanban_block` call does not mutate the card and tells the model
  to create a new task/swarm for a new request.
- CLI listing and explicit status filters remain backward compatible.
- Existing Kanban, swarm, replay, compression, and gateway tests remain green.
- The same autumn four-lane prompt is sent through Telegram after deployment;
  evidence must show a fresh swarm root/task IDs and four actual lane runs.
- The deployed release marker, process cwd, venv, and `HERMES_RELEASE_SHA`
  match the implementation commit before the Telegram test.

## Implementation

Implemented locally:

- `hermes_cli/kanban_db.py`: added a reusable `exclude_statuses` filter without
  changing CLI defaults.
- `tools/kanban_tools.py`: active-only default model listing, explicit history
  scope metadata, and terminal-card fail-closed guidance.
- `agent/prompt_builder.py`: fresh-request isolation guidance for orchestrators.
- `tests/tools/test_kanban_tools.py`: regression coverage for terminal hiding,
  explicit history inspection, and terminal-card mutation rejection.

## Cross-review record

Local review A (Kanban compatibility): **PASS**. The CLI remains unchanged;
explicit `status=done`/`status=archived` history queries still work, and
`include_archived=true` still exposes archived rows while the default hides
done rows.

Local review B (replay/idempotency safety): **PASS**. The terminal-card path
does not mutate the card, and the fix deliberately does not teach
`MODEL-REPLAY-RELIABILITY-001` to replay the mutating `kanban_swarm` operation.

External Claude/AGY review: **BLOCKED_BY_SECURITY_POLICY**. The local
permission layer rejected transmitting this uncommitted private diff to either
external adapter; no external PASS is claimed. Upstream remains
download-only.

Validation before commit:

- Kanban tool/swarm/CLI/board/lifecycle regression set: **139 passed**.
- `compileall`: passed.
- `git diff --check`: passed.

## Current disposition

Code is implemented locally. Deployment and the real Telegram acceptance test
are required before closure. The model's creative joke quality remains outside
this ticket.
