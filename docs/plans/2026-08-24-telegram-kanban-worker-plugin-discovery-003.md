# TELEGRAM-KANBAN-WORKER-PLUGIN-DISCOVERY-003

Status: FOLLOWUP_REVIEW_APPROVED_PENDING_DEPLOY
Date: 2026-08-24
Type: ticket
Target repo: hermes-agent
Priority: P1

## Incident

Two Telegram submissions did reach the gateway and created two four-lane
swarms in chat `-1004391006048`, but the user saw no timely final response.
The first run contained a native worker timeout and both runs accumulated
running/ready workers under the DGX memory-pressure gate. The decisive worker
log finding was:

`Warning: Unknown toolsets: mermaid_renderer`

The gateway passed `mermaid_renderer` in the worker command, but the
long-lived gateway resolved worker toolsets before plugin discovery completed.
The child therefore received an explicit toolset name that its registry did
not know and could not call `render_mermaid`, recreating the missing-PNG path.

Observed evidence:

- Telegram inbound accepted at 14:06:05, correlation `ab609fa52cff4143`.
- Second Telegram inbound accepted at 14:13:56, correlation `87ef7b50d32d439c`.
- Swarm roots created: `t_2493eb5b` and `t_0306200b`.
- First root's native lane `t_b7ccae99` timed out after 302 seconds.
- Worker logs for `t_27fef090`, `t_eba4366a`, `t_b7ccae99`, `t_594c8bee`, and
  `t_22956ba5` all reported the unknown plugin toolset.
- The gateway itself remained active and Telegram polling remained healthy.

## Fix

In `_resolve_worker_cli_toolsets`, call the public `discover_plugins()` path after
switching to the worker's effective `HERMES_HOME` and before resolving
`platform_toolsets.cli`. This keeps the bounded worker tool surface while
making plugin-provided toolsets visible to the resolver. It does not bypass
profile filtering or add unrestricted tools.

## Acceptance criteria

- [x] Worker resolver performs plugin discovery before filtering requested
      toolsets.
- [x] Live resolver returns `mermaid_renderer` for the deployed Hermes home.
- [x] Regression test covers the discovery-before-filtering ordering.
- [x] Cross-review found and resolved the background-discovery race; final review
      has no unresolved P0/P1/P2 finding.
- [ ] Immutable release deployed and worker command no longer logs
      `Unknown toolsets: mermaid_renderer`.
- [ ] Fresh Telegram E2E reaches verifier and synthesizer and produces a real
      durable PNG attachment.
- [ ] Duplicate in-flight Telegram submissions are observable and do not leave
      the user without a progress/final-state message.

## Scope boundary

Do not weaken the memory-pressure guard or fabricate artifacts. A second
submission may legitimately create a separate request, but the user-visible
workflow must expose its queued/running state and terminal outcome. Model
endpoint availability remains a separate vLLM configuration decision.

## Implementation evidence

- Source change: `hermes_cli/kanban_db.py`
- Regression change: `tests/hermes_cli/test_kanban_worker_spawn_toolsets.py`
- Targeted tests: worker toolset test suite 7 passed.
- Live resolver check: returned `['file', 'kanban', 'skills', 'terminal',
  'web', 'mermaid_renderer']`.

## Review evidence

- Codex CLI cross-review initially found a P1 background-discovery race in the
  first implementation.
- Resolved by switching from `_ensure_plugins_discovered()` to public
  `discover_plugins()`, which joins the in-flight discovery thread before
  `_get_platform_tools()` reads plugin keys.
- Related tests: 82 passed.

## Follow-up live E2E finding

After the first immutable release was deployed, Telegram transport recovered:
the inbound update was accepted, four workers were spawned, progress and final
messages were delivered, and the gateway stayed healthy. However, all four
fresh worker logs still reported:

`Warning: Unknown toolsets: mermaid_renderer`

The remaining root cause was in the child CLI: `HermesCLI` validated the
explicit bounded `--toolsets` list before the child plugin registry had run
discovery. The gateway resolver was therefore correct, but a cold-started
worker still rejected the plugin-backed toolset.

## Follow-up fix

`cli.py` now runs the public synchronous `discover_plugins()` path before
toolset validation for Kanban workers. The `HERMES_DEFER_AGENT_STARTUP=1`
interactive Termux path remains deferred unless the Kanban task marker is
present, so ordinary prompt-first startup semantics are preserved.

Regression coverage uses a real temporary Hermes home with the Mermaid plugin
enabled and also verifies that deferred non-Kanban startup does not discover
eagerly.

Follow-up validation: 134 passed, 1 skipped; final cross-review found no
actionable defect in the patch. A fresh post-deploy Telegram E2E remains the
release acceptance gate.
