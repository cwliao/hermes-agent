# KANBAN-MERMAID-WORKER-TOOLSET-002

Status: IMPLEMENTED_PENDING_REVIEW
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
- [ ] Cross-review passes with no unresolved P0/P1/P2 finding.
- [ ] Deploy immutable release and verify a new worker command includes
      --toolsets ... mermaid_renderer.
- [ ] Real Telegram retry reaches verifier and synthesizer with a durable PNG.

## Scope boundary

This ticket does not weaken swarm completion validation or fabricate PNG
evidence. A worker must still produce and declare a real readable artifact;
this change only makes the installed renderer reachable.
