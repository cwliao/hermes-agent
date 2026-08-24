# KANBAN-MERMAID-WORKER-TOOLSET-002

Status: DEPLOYED_PENDING_TELEGRAM_E2E
Date: 2026-08-24
Type: ticket
Target repo: hermes-agent
Priority: P1

## Incident

Telegram transport and four-lane swarm creation succeeded, but the live
Kanban DB showed native_hermes blocked on Missing PNG artifact for visual
release; verifier and synthesizer never became runnable. The worker process
was launched with file,kanban,skills,terminal,web and had no render_mermaid
tool, even though the Mermaid renderer plugin was enabled for Telegram.

## Fix

Add the bounded mermaid_renderer toolset to the Kanban worker default surface.
The dispatcher still filters defaults through the assigned profile's explicit
CLI toolset, so the plugin is available only when that profile enables it.
The live CLI platform toolset is updated to expose the already-enabled plugin.

## Acceptance criteria

- [x] Worker default toolset includes mermaid_renderer.
- [x] Cross-review APPROVE; no unresolved P0/P1/P2 finding.
- [x] Deploy immutable release v2026.8.24-kanban-mermaid-worker-toolset-bf1ba3e619;
      deployed resolver includes --toolsets ... mermaid_renderer.
- [ ] Real Telegram retry reaches verifier and synthesizer with a durable PNG.

## Scope boundary

This ticket does not weaken swarm completion validation or fabricate PNG
evidence. A worker must still produce and declare a real readable artifact;
this change only makes the installed renderer reachable.

## Delivery evidence

- Implementation commit: bf1ba3e619d5316608b0d03a9bff9f3905283f71
- Pushed to origin/main.
- Immutable release: /home/cwliao/.hermes/releases/v2026.8.24-kanban-mermaid-worker-toolset-bf1ba3e619
- Pinned venv: /home/cwliao/.hermes/venvs/gateway-bf1ba3e619
- Effective systemd drop-in: 88-kanban-mermaid-worker-toolset-bf1ba3e619.conf
- Post-deploy gateway: active/running, NRestarts=0, Telegram polling healthy.
