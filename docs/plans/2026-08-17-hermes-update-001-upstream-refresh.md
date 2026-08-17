---
title: "HERMES-UPDATE-001 upstream refresh: selective security and feature port"
status: PORT_SELECTION_REVIEW_PENDING
date: 2026-08-17
type: operations/reliability
ticket: HERMES-UPDATE-001
target: Linux DGX Spark deployment
---

# Decision

The upstream candidate pinned by the original plan is stale. Upstream moved
from `45af7a71fcd420b4422d2c074b1ce58b9ce0d048` to
`a36583e311a7bea351a245f703dc5e3850450f41`, 292 commits ahead with no commits
behind. The delta includes Telegram, gateway, agent-state, configuration, and
security-related changes.

The required update is **selective porting**, not a full upstream replacement.
The DGX release remains the current private ARCH-004 release until the selected
lanes pass their own implementation, review, CI, and deployment gates.

## Priority port set

These lanes are the new correction set to review and implement on a clean
isolated candidate worktree:

### P0 — security and safety fixes

- Fail-closed tool-hook approval sharing: `20fcc11a0c`.
- Reject masked verification results: `c6dfdcbf8d`.
- Detect terminal pipelines that falsely report success: `8ad055414b`.
- Preserve MCP DCR client secrets and redact token response failures:
  `9975180a59` (only where MCP OAuth is enabled on the DGX profile).
- Reject NUL-bearing cron paths and unsafe directory handling:
  `40586082e5`, `795e035f60`, and `9ac1e65b0a`.
- Preserve profile-scoped provider credential resolution:
  `8ff5f13c09`, `275f8d41bc`, and `b560c0d241`.

### P1 — user-visible Telegram and gateway behavior

- Keep `/loop` and synthetic sends in the active Telegram DM topic:
  `25fabcf8eb`.
- Enforce `group_allowed_chats` during early auth under multiplex profiles:
  `9ca11399c0`.
- Preserve streamed/already-sent response delivery and loop ticks:
  `fccf2b718e`, `2f6bbfbcbc`.
- Isolate post-turn gateway failures and surface the response path:
  `9219cd3944`, `b7936c892d`.
- Reconcile routed profile model configuration and persisted model routes:
  `faaf2ae093`, `095d25c612`.

### P2 — state continuity, only after P0/P1

- Watermark and concurrent-tail preservation during compression:
  `21d3e63702`, `652f5c2ebb`, `406c5daf04`.
- Structural SQLite corruption classification and durable ordinal safeguards:
  `06b9141109`, `79b7d969d3`.

P2 changes touch session/WAL and compression invariants and require focused
state-integrity, prompt-cache, message-order, and recovery tests before any
port decision. They are not a reason to replace the current DGX release now.

## New feature inventory

The upstream delta also contains product features. These are separated from
the security and Telegram correction set so a useful feature is not lost, but
does not silently expand the deployment scope.

| Feature | Upstream commits | DGX disposition | Reason/evidence gate |
|---|---|---|---|
| Pre-tool hook argument transformation (`modify`) | `d083b85591` | **PORT CANDIDATE** | Useful for controlled skills and plugins; must retain fail-closed approval, argument auditability, and cache stability. |
| Compression `tail_mode` (`legacy`/`lean`) | `1d5fc2bc0b` | **PORT CANDIDATE, default legacy** | User-visible context-cost/performance feature; requires compression, WAL, prompt-cache, and session-continuity tests. |
| Bot Mode teammate protocol and built-in bot plugin | `2b39e92e9e`, `366d8814b8`, `ea4310e76c`, `4e22d070f4`, `8236b41771` | **SEPARATE FEATURE TICKET** | Major multi-agent surface with prompt, plugin, routing, and state impact; do not hide it inside an upstream refresh. |
| SessionDB context-manager protocol | `ab7f48d4b0` | **SEPARATE STATE TICKET** | Changes the persistence contract; requires ARCH-style WAL/schema/recovery review. |
| Larger Codex OAuth context limits | `5229975438`, `bab7be3ca7` | **CONDITIONAL PORT** | Port only if the DGX profile uses the affected Codex OAuth models; verify provider limits and cost behavior. |
| Cua Driver 0.20 runtime contract | `a403fe6f92` | **DEFER / CONDITIONAL** | Useful only when the DGX deployment has that computer-use backend; no Telegram or gateway benefit. |
| LiteLLM Claude prompt-cache compatibility | `0038d96b78`, `1b0e953b47`, `ff4df5e54d` | **PORT CANDIDATE IF USED** | Directly protects prompt-cache behavior, but only applies to the affected LiteLLM provider path. |
| Remote gateway headers and connection-aware plugin routing | `fcef62ef72`, `b711fd0513`, `27e4f09540` | **DEFER / SEPARATE REMOTE-GATEWAY TICKET** | Primarily desktop/remote connection UX; not required for the current local DGX gateway service. |
| Model/provider in live-transcript manifest | `e3c71e052d` | **DEFER** | Metadata usefulness must be reconciled with the local-only/no-telemetry boundary before adoption. |
| Desktop MCP fleet UI, deep links, health overlays, pane resizing, and bot UI | `f3bf718a62`, `37445d6dc2`, `e22fa90769`, `56eafcff3c`, `1c3fbd21ae`, and related commits | **EXCLUDE FROM DGX PORT** | Desktop-only surface; no Linux gateway runtime contract. |

### Recommended implementation order

1. P0 security/safety fixes and P1 Telegram/gateway fixes.
2. `modify` hook and LiteLLM cache fix when their runtime paths are enabled.
3. Compression `tail_mode`, keeping `legacy` as the default until state and
   prompt-cache evidence passes.
4. Conditional Codex OAuth context support if the DGX model profile needs it.
5. Open separate tickets for Bot Mode and SessionDB context-manager support;
   do not bundle either into this update.

## Explicitly excluded

- Desktop-only UI, desktop SDK, macOS-only computer-use, and Windows-native
  wrapper behavior.
- Blind cherry-picking of the 292-commit upstream range.
- Generated artifacts, caches, virtual environments, unrelated upstream
  features, or changes without a DGX runtime contract.
- Any modification to the live DGX checkout, `~/.hermes`, systemd unit, or
  active release during this refresh.

## Preserve unchanged

The existing preserve contract remains authoritative: `~/.hermes` user state,
credentials, sessions, memories, skills, plugins, pairing, cron state, WAL/SHM,
DGX host and authenticated WSL SSH identity, immutable release markers,
rollback selection, prompt-cache stability, message-role alternation, truthful
tool/artifact results, Telegram pairing/authz, inbound polling, outbound
delivery, and fail-closed recovery behavior.

## Required gates

1. Recompute the compatibility inventory against the new upstream SHA; do not
   reuse the matrix pinned to `45af7a71`.
2. Map each P0/P1/P2 candidate to the private source anchors, current upstream
   seam, tests, and rollback impact. Drop a lane if it has no concrete private
   contract or DGX benefit.
3. Review the identical metadata-only correction packet with exactly one
   authenticated DGX Claude and one authenticated DGX AGY; reconcile any
   correction set before implementation.
4. Implement only approved lanes in an isolated clean worktree, then run
   focused tests and the full required CI gates.
5. Keep merge, immutable release, DGX restart, runtime health, Telegram
   inbound, and Telegram user-visible delivery as separate authorizations and
   evidence gates.

Current decision: **UPDATE THE PORT PLAN; RETAIN THE CURRENT DGX RELEASE.**
