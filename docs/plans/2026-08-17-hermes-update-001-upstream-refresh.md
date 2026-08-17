---
title: "HERMES-UPDATE-001 upstream refresh: selective security and feature port"
status: IMPLEMENTATION_REVIEW_BLOCKED
date: 2026-08-17
type: operations/reliability
ticket: HERMES-UPDATE-001
target: Linux DGX Spark deployment
---

# Decision

The upstream candidate pinned by the original plan is stale. Upstream moved
from `45af7a71fcd420b4422d2c074b1ce58b9ce0d048` to
`3b9a963b8e5cdb804a422755bed9a60fcd778273`, `candidate_delta_ahead=300` and
`candidate_delta_behind=0` relative to the prior candidate. These are not the
private/upstream divergence counts below. The delta includes Telegram, gateway, agent-state, configuration, and
security-related changes. `candidate_delta_ahead` and `candidate_delta_behind`
measure only the prior-candidate to new-candidate delta; `private_only_commits`,
`upstream_only_commits`, and `changed_paths` measure the full private/upstream
fork divergence. The metadata-only inventory against private
`origin/main` `39265b589f98bfbc7f16621916131412b35161f8` reports 7,720
upstream-only commits and 7,223 changed paths; this is evidence against a
full-tree replacement, not a reason to abandon selective porting.

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
  `9975180a59`; MCP OAuth activation is classified
  `DEFERRED_NOT_IN_THIS_RELEASE` because the DGX capability is not verified.
  It is not part of the current P0 implementation set.
- Reject NUL-bearing cron paths and unsafe directory handling:
  `40586082e5`, `795e035f60`, and `9ac1e65b0a`.
- Reject cloud-placeholder cron reads at the shared file-read choke point:
  `42a96e7543`, `b2cc4ceef0`.
- Preserve profile-scoped provider credential resolution:
  `8ff5f13c09`, `275f8d41bc`, and `b560c0d241`.
- Validate explicit xAI API-key precedence, profile secret scope, and base-URL
  origin before xAI search/TTS use: `32170dd1a2`, `3b9a963b8e`.

### P1 — user-visible Telegram and gateway behavior

- Keep supported Telegram synthetic sends in the active DM topic:
  `25fabcf8eb`; the absent `/loop` base feature is not ported by this ticket.
- Enforce `group_allowed_chats` during early auth under multiplex profiles:
  `9ca11399c0`.
- Preserve applicable streamed/already-sent response handling:
  `2f6bbfbcbc`; the dependent `/loop` tick patch `fccf2b718e` was excluded.
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
| xAI key precedence and origin validation | `32170dd1a2`, `3b9a963b8e` | **CONDITIONAL SECURITY PORT** | Port only when xAI search/TTS is enabled; preserve profile secret scope, explicit-key precedence, base-URL validation, and OAuth fallback. |
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
- MCP OAuth DCR handling for this release; its DGX capability is
  `DEFERRED_NOT_IN_THIS_RELEASE` and must be re-evaluated by a separate
  capability check before any future selection.
- Blind cherry-picking of the 300-commit upstream range.
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

## Lane mapping evidence

The following is the current metadata-only mapping. The upstream SHAs are
candidate anchors inside `UPSTREAM_TARGET_SHA`; they are not cherry-pick or
fast-forward commands. `xai-shared-resolver` happens to equal the target SHA
because that change is part of the target tip, not because it is a separate
operation.

| Selected lane | Private anchor | Upstream seam/anchor | Required tests | Rollback impact |
|---|---|---|---|---|
| Tool-hook fail-closed approval | `hermes_cli/plugins.py`, `agent/shell_hooks.py`, `model_tools.py` | `20fcc11a0c`, shared hook dispatch/approval seam | hook approval/block/modify regression suite; role/cache invariants | code-only release rollback; no user-state migration |
| Masked verification rejection | `agent/verification_evidence.py`, `run_agent.py` | `c6dfdcbf8d`, verification-result acceptance seam | `tests/agent/test_verification_evidence.py` and runtime guardrail tests | code-only release rollback; no user-state migration |
| Terminal masked-success safety | `tools/terminal_tool.py`, `tools/terminal_hints.py` | `8ad055414b`, terminal result annotation seam | `tests/tools/test_terminal_hints.py` plus terminal E2E | code-only release rollback; no state mutation |
| Cron path safety | `cron/lifecycle_guard.py` | `40586082e5`, `795e035f60`, `9ac1e65b0a`, `42a96e7543`, `b2cc4ceef0` | NUL, directory, and cloud-placeholder no-open tests | restore prior release; preserve cron definitions/output |
| Telegram topic/auth/delivery loop | `plugins/platforms/telegram/adapter.py`, `gateway/run.py` | `25fabcf8eb`, `9ca11399c0`, `2f6bbfbcbc`, `fccf2b718e` | `tests/gateway/test_loop_command.py`, Telegram auth/reply tests, authorized delivery checks | restore prior Telegram release/drop-in; preserve pairing/state |
| Gateway failure visibility | `gateway/run.py`, `gateway/session_state.py` | `9219cd3944`, `b7936c892d`, shared post-turn failure seam | gateway restart/background failure tests; metadata-only error visibility | one prior gateway release/drop-in rollback; preserve sessions |
| Routed profile model consistency | `gateway/config.py`, `gateway/run.py` | `faaf2ae093`, `095d25c612`, routed/persisted model seam | profile routing and persisted-model config tests | restore prior gateway release; preserve config/state |
| Profile-scoped credential resolution | `hermes_cli/auth.py`, `gateway/config.py` | `8ff5f13c09`, `275f8d41bc`, `b560c0d241` | profile-scope/env fallback tests; no secret output | restore prior release; preserve credential files byte-for-byte |
| xAI key precedence and origin validation | `tools/x_search_tool.py`, `tools/tts_tool.py`, `tools/xai_http.py`, `hermes_cli/auth.py` | `32170dd1a2`; `3b9a963b8e` is the target-tip shared resolver, not a separate cherry-pick | `tests/tools/test_xai_http_credentials.py`, x-search, and xAI TTS tests | code-only release rollback; no credential file mutation; the entire lane is eligible only when the conditional check is `ENABLED` |
| Compression tail/state continuity | `agent/conversation_compression.py`, `hermes_state.py`, `run_agent.py` | `21d3e63702`, `652f5c2ebb`, `406c5daf04`, `06b9141109`, `79b7d969d3` | watermark, WAL/schema, prompt-cache, message-order, recovery tests | no live migration; rollback before touching state; verify DB/WAL manifest |

Conditional lane checks are explicit: xAI lane selection requires an isolated
profile/config resolution test proving whether xAI search/TTS is enabled;
Codex context selection requires the DGX model-profile inventory; LiteLLM cache
selection requires a provider-path test. The port implementer owns these
checks and must record `ENABLED` or `DISABLED` for the selected port plan; the
current plan status is `DISABLED` for xAI search/TTS, Codex OAuth context, and
LiteLLM Claude cache. These are selection statuses, not claims about secret
values or unread runtime configuration; if any live check becomes `ENABLED`,
implementation is blocked until a follow-up lane-mapping cycle adds private
paths, tests, and rollback impact.

### Cross-lane collision checklist

These shared files are intentional integration boundaries and must be reviewed
as combined seams before rollback rehearsal:

- `run_agent.py`: masked-verification and state-continuity lanes.
- `gateway/run.py`: Telegram-gateway, gateway-failure-visibility, and
  routed-profile-model lanes.
- `hermes_cli/auth.py`: profile-auth and conditional xAI-security lanes.
- `gateway/config.py`: profile-auth and routed-profile-model lanes.

The dependency review must record `PASS` for these four collision groups
before `ROLLBACK_VERIFICATION_GATE`; an unresolved collision blocks merge and
release.
The pre-implementation metadata result is `PASS`: selected anchors have no
unexpected overlap with excluded Bot Mode or desktop-only paths; the four
listed shared-file overlaps are intentional combined seams with no unresolved
collision at plan scope.

## Required gates

### Review correction set applied

- **MCP lane:** explicitly deferred from this release; no assumption is made
  about whether MCP OAuth is enabled on the DGX profile.
- **Dependency isolation:** path-level metadata review of the selected anchor
  commits found no overlap with the excluded Bot Mode commit files; semantic
  dependency review remains required during implementation.
- **Rollback:** bounded rollback verification is a standalone gate before
  merge/release, separate from recording rollback impact in the inventory.
- **Preserve contract:** the selective-port scope preserves every listed user,
  credential, state, identity, release-marker, cache, role, Telegram, and
  fail-closed item at plan scope; live runtime preservation remains a later
  implementation/release gate.

1. Recompute the compatibility inventory against the new upstream SHA; do not
   reuse the matrix pinned to `45af7a71`. Current result is private
   `39265b589f98bfbc7f16621916131412b35161f8` versus upstream
   `3b9a963b8e5cdb804a422755bed9a60fcd778273`: 206 private-only commits,
   7,720 upstream-only commits, and 7,223 changed paths.
2. Use the lane mapping above and explicitly verify that selected P0/P1/P2
   commits do not require excluded Bot Mode or desktop-only commits. Drop a
   lane if it has no concrete private contract or DGX benefit.
3. Pass the `STATIC_PATH_ISOLATION_GATE`: record the pre-implementation
   metadata path-overlap result, including the four shared-file collision
   groups, before independent review. Then review the identical
   metadata-only correction packet with exactly one authenticated Claude and
   one authenticated AGY (DGX Spark first; WSL is the bounded fallback if a
   DGX headless invocation cannot run). Reconcile any correction set before
   implementation.
4. Implement only approved lanes in an isolated clean worktree, then run
   focused tests and the full required CI gates.
5. Pass the `POST_IMPLEMENTATION_SEMANTIC_DEPENDENCY_GATE`: review the actual
   implementation diff and record `PASS` for all four collision groups and
   their dependency isolation. An unresolved collision blocks rollback,
   merge, and release.
6. Before merge/release, execute a bounded rollback verification in the
   isolated candidate/release rehearsal and record it separately from
   rollback-impact mapping.
7. Keep merge, immutable release, DGX restart, runtime health, Telegram
   inbound, and Telegram user-visible delivery as separate authorizations and
   evidence gates.

## Independent review evidence

- Result: **PASS / consensus** for the metadata-only port plan; no corrective
  set remains.
- Packet SHA-256:
  `e729ace3f9b6d53e868699b37b1e9784e54fe4f160987b41b6fd15ddd27eeec5`.
- Claude: authenticated DGX Spark host `55-0940189-03`, model
  `claude-sonnet-5`, CLI `2.1.229`, verdict `PASS`.
- AGY: authenticated WSL host `55-0940189-91`, AGY `1.1.13`, verdict
  `PASS`; DGX headless AGY was unavailable because its command permission
  prompt could not run in bounded mode, so the documented WSL fallback was
  used. No permission bypass was used.
- Both reviewers consumed the identical packet SHA. The review covered only
  metadata; no implementation, merge, deploy, restart, credential inspection,
  or message-content access occurred.

Current decision: **UPDATE THE PORT PLAN; RETAIN THE CURRENT DGX RELEASE.**

## Implementation evidence and review status

- Isolated implementation head: `3ecbc6329` on
  `ticket/hermes-update-001-dgx-upstream`; no push, merge, deploy, restart, or
  DGX mutation was performed.
- The upstream streamed `/loop` patch was found to depend on absent base commit
  `f79440e0f` (`hermes_cli/loops.py` and its cross-surface feature set). It was
  reverted by `4e4a9d646`; the unsupported `/loop` test file was removed, and a
  static impact check found no remaining `LoopManager`/`hermes_cli.loops`
  references in retained implementation or tests. Private `/goal` remains.
- Applicable canonical focused suite on the Windows shim: `128/128 PASS`
  across terminal hints, verification evidence, Telegram auth, and Telegram
  reply mode. The cron suite reached 44 passing tests before a Windows-only
  `C:\...` path tokenization failure; this is not Linux/DGX evidence.
- WSL target-Linux setup is `BLOCKED`: available Python is `3.14.4`, while the
  package requires `>=3.11,<3.14`. No Linux cron result is claimed.
- DGX Claude implementation review: `BLOCKED`; the reviewer would not attest
  to implementation correctness from metadata-only evidence without an
  inspectable diff.
- WSL AGY re-review of the same updated packet: `BLOCKED`; requested supported
  Linux test evidence, CI, impact analysis, and later DGX runtime/delivery
  evidence remain unavailable.

Current implementation decision: **BLOCKED pending supported-Linux test/CI
evidence and the separate DGX runtime/delivery gates.** Do not merge, deploy,
or modify DGX until these gates are separately authorized and pass.
