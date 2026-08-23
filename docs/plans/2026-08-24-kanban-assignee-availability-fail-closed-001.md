# KANBAN-ASSIGNEE-AVAILABILITY-FAIL-CLOSED-001

Status: IMPLEMENTED_PENDING_COMMIT_AND_DEPLOY
Date: 2026-08-24
Type: ticket
Target repo: hermes-agent
Priority: P1

## Incident

The four-lane Kanban request created verifier task `t_883b4d38` with
`assignee=verifier-default`. That name is neither an installed Hermes profile
nor a registered external terminal watcher, so the card remained `ready`
without a claim or spawn. A second reported synthesizer ID did not exist in
the board; the model nevertheless described both cards as successfully
created.

The dispatcher was alive and ticking. It correctly skipped the non-spawnable
ready cards, but the creation boundary had already allowed an impossible
assignment into the board.

## Design

1. Validate every model-facing assignee before a task or swarm graph is
   created.
2. A real Hermes profile is spawnable when `profile_exists()` is true.
3. An external terminal lane is valid only with a fresh, explicit Kanban
   watcher lease and heartbeat. A process name, PATH entry, or historical
   lane name is not evidence of availability.
4. An explicitly invalid assignee fails closed with a structured error. It is
   not silently rewritten to `default`; fallback is only used when the caller
   omitted the assignee.
5. Swarm validation covers all worker profiles plus verifier and synthesizer
   assignees before the root card is written. No partial graph is allowed on
   validation failure.

## Acceptance criteria

- [x] `kanban_create` rejects an unknown profile with no live watcher lease.
- [x] `kanban_swarm` rejects invalid worker, verifier, and synthesizer
  assignees before creating any card.
- [x] A fresh external watcher lease makes its assignee valid; an expired
  lease does not.
- [x] Existing lane routing remains explicit: `lane_id` carries the external
  skill, while the task assignee remains a real Hermes profile or registered
  watcher.
- [x] Existing direct DB/CLI test fixtures remain compatible without making
  arbitrary non-profile model assignments appear valid.
- [x] Tests cover zero-card-on-rejection and lease expiry.
- [x] Cross-review records the DB, tool, CLI, and dispatcher implications.
- [ ] Deployed release and a real Telegram retry are required before closure.

## Implementation notes

The durable watcher lease belongs in the per-board Kanban DB. It must expose
register, heartbeat, unregister, and availability operations for external
terminal runners; no API key or terminal credential is stored in the board.

## Implementation

- `hermes_cli/kanban_db.py`: added per-board `kanban_assignee_watchers`
  leases with register/heartbeat/unregister/list/availability operations.
- `hermes_cli/kanban.py`: added the external runner CLI surface:
  `hermes kanban watcher register|heartbeat|unregister|list`.
- `tools/kanban_tools.py`: preflights every model-facing child and swarm
  assignee, including verifier and synthesizer, before graph creation.
- `tests/tools/test_kanban_tools.py`: covers unavailable assignees, zero-card
  rejection, watcher expiry, and existing lane behavior.

## Review record

Local review A: **PASS** — DB lease expiry is checked at creation time;
  profile and current-profile routes remain valid; swarm validation occurs
  before the atomic graph builder is entered.

Local review B: **PASS** — external lane names are not treated as Hermes
  profiles, explicit invalid verifier/synthesizer names do not silently
  fallback, and the dispatcher remains responsible only for real profiles.

External reviewers: **BLOCKED_BY_RUNTIME_POLICY** — Codex nested app-server,
  Claude session persistence, Grok session creation, and AGY log/socket setup
  were all blocked by this execution environment's read-only filesystem or
  socket policy. No external PASS is claimed.
