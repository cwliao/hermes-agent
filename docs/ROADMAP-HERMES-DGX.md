# Hermes DGX Roadmap

> Snapshot: 2026-08-10 (Asia/Taipei). This is the planning source of truth for the Hermes DGX integration effort.
> Re-verify branch heads, live release marker, service status, and ticket refs before every deployment decision.

## 1. Source and status rules

- Product goals, engineering sequence, and tickets are separate layers.
- `Merged/release`: the change is reachable from the DGX release source branch.
- `Branch-only`: the change exists on a fetched branch but is not in the release source.
- `Rebuilt/unreviewed`: code exists in the isolated rebuild worktree but has not passed the delegated review loop.
- `Proposed`: a future item without an implementation commit in the current refs.
- `Live/deployed` requires runtime evidence; a commit, push, listener, or process exit code is not deployment proof.
- Deployment remains a separate approval checkpoint from implementation and review.

### Hermes ARCH mainline policy

- `main` in `cwliao/hermes-agent` is the canonical ARCH mainline.
- Every `ARCH-*` ticket must be reviewed, tested, and merged into Hermes
  `main` before it is marked complete.
- `ticket/T0127-v2026.8.3-merged` is only the current DGX release-staging
  source. A release built from it proves deployment, not mainline integration.
- After an ARCH merge to `main`, the DGX release must be re-baked from the
  merged mainline commit and runtime evidence must be refreshed.

## 2. Current repository topology

| Reference | HEAD / evidence | Meaning |
|---|---|---|
| `origin/main` | `7fa1865f7` (2026-08-04) | Public/main line; includes T0051, but is not the DGX release target. |
| `origin/ticket/T0127-v2026.8.3-merged` | `8ef05d5a3` (2026-08-08) | Current DGX release source branch used for the rebuild baseline. |
| `origin/feature/klib-orchestration-integration` | `5863e9d5e` (2026-08-08) | Integration branch; includes T0108/T0138 work but diverges from the release source. |
| live checkout | `main`, `7fa1865f7` at last read-only check | Claude-owned checkout; do not edit, reset, pull, or restart it from this workflow. |
| ARCH-001 rebuild | `agent/arch-001-dgx-release-rebuild` at `f0dd130c8` | Committed, pushed, baked, and live on DGX as `v2026.8.3-arch-001-f0dd130c8`; not yet merged to Hermes `main`. |

This clone currently has only the `origin` remote; no separate `upstream` remote is configured.

## 3. v0.20 architecture series and ticket aliases

The complete v0.20 architecture plan is recorded in the workspace planning documents below. It is broader than the ARCH branches currently visible in the fetched Hermes Git refs:

- `D:\AI\project\HERMES_V020_TICKETS_V2.md` — parent-level consensus and priority order.
- `D:\AI\project\HERMES_V020_IMPLEMENTATION_TICKETS_DRAFT.md` — detailed implementation split and dependencies.
- `D:\AI\project\HERMES_HANDOVER_2026-08-07.md` — current handover, delegated reference-branch notes, and acceptance boundaries.
- `D:\AI\project\DGXSpark\docs\hermes-cutover\UPSTREAM-ALIGNMENT-2026-08-09.md` — T01–T08 upstream cutover crosswalk and status.

The parent tickets and detailed implementation tickets are aliases, not duplicate work:

| Parent umbrella | Detailed implementation tickets | Scope |
|---|---|---|
| `ARCH-000` | `ARCH-001` through `ARCH-004` | Shared runtime-state foundation, audit/replay, redaction, and SQLite/WAL safeguards. |
| `SEC-003` | `SEC-001`, `SEC-002` | Structural shell policy plus approval state machine and denial breaker. |
| `UP-001` / `UP-002` | `RES-001`, `RES-002` | Bounded tool recovery plus asynchronous compression/state reinjection. |
| `OPS-004` | `OPS-001` | Gateway heartbeat and bounded stall watchdog. |

### v0.20 P0 implementation inventory

| Ticket | Capability and dependencies | Current evidence/status |
|---|---|---|
| `ARCH-001` | Versioned shared runtime state schema; no dependencies. | Implemented at `f0dd130c8`; 29 focused tests passed; pushed and deployed with runtime fingerprint and health evidence. It is not mainline-complete until reviewed and merged to Hermes `main`. Claude unavailable; latest AGY output cited incorrect paths and is not accepted as consensus. |
| `ARCH-002` | Append-only audit event store and read-only replay; depends on `ARCH-001`. | Not implemented or merged in current local refs. |
| `ARCH-003` | Central recursive secret redaction and safe serialization; depends on `ARCH-001`. | Handover records a delegated reference branch/commit (`dbd53d329e`) with a foundation-only implementation; consumer wiring is absent and it is not accepted as final. The reference branch is not present in current local refs. |
| `ARCH-004` | SQLite/WAL contention, corruption, disk-full classification, bounded retry, and fail-closed safeguards; depends on `ARCH-001` + `ARCH-002`. | Not implemented or merged in current local refs. |
| `SEC-001` | Structural shell policy parser and fixtures; depends on `ARCH-001` + `ARCH-003`. | Not implemented or merged in current local refs. |
| `SEC-002` | Durable approval state machine and denial circuit breaker; depends on `ARCH-001` + `ARCH-002` + `SEC-001`. | Not implemented or merged in current local refs. |
| `RES-001` | Bounded tool-recovery middleware; depends on `ARCH-002` + `ARCH-003` + `SEC-002`. | Not implemented or merged in current local refs. |
| `RES-002` | Asynchronous compression and structural state reinjection; depends on `ARCH-001` + `ARCH-003`. | Not implemented or merged in current local refs. |
| `OPS-001` | Gateway heartbeat and bounded stall watchdog; depends on `ARCH-001` + `ARCH-002` + `ARCH-004` + `SEC-002` + `RES-001` + `RES-002`. | Not implemented or merged in current local refs. |

The detailed draft's implementation defaults are part of the acceptance contract: three denials open the breaker; tools get at most two retries and ten retries per task; compression has a hard 15-second default; watchdog recovery never automatically reruns writes, deploys, pushes, or systemd operations. Every state change must be redacted, audited, schema-versioned, and tested in an isolated profile without writing to the live release.

### Deferred v0.20 items

| Ticket | Priority | Scope | Status |
|---|---:|---|---|
| `INT-005` | P1 | Signed webhook outbox with HMAC rotation, bounded retry, and DLQ. | Deferred until P0 contracts stabilize. |
| `A2A-006` | P2 | Controlled A2A adapter with bounded depth/concurrency and denied egress. | Deferred until P0 and `INT-005` contracts stabilize. |

The fetched Hermes refs currently show ARCH-001 branches/commits only. That Git visibility must not be mistaken for the full planning inventory; conversely, the handover's ARCH-003 reference metadata must not be mistaken for locally available, accepted code.

### Upstream cutover delivery tickets

The upstream release-delivery track is separate from the architecture feature track and is now included in this roadmap:

| Ticket | Role | Status |
|---|---|---|
| `T01 BASELINE` | Candidate tag/signature/peeled SHA/dependency/diff verification. | Revised; AGY and Claude review completed; candidate verification still required. |
| `T02 KLIB` | Versioned KLIB overlay, metrics, smoke, regression, state compatibility. | Planning aligned; implementation/review pending. |
| `T03 DOCU` | DocuBot/KMDaily/Drive cursor, idempotency, clone-state migration. | Planning aligned; implementation/review pending. |
| `T04 SAFE` | Safety guards, redaction, forbidden-model/tool-scope fail-closed checks. | Planning aligned; implementation/review pending. |
| `T05 INTEGRATION` | Telegram adapter and shared-file ownership/sign-off. | Planning aligned; implementation/review pending. |
| `T06 UPDATER` | Immutable release slots, manifest/state machine, receipts, rollback. | Planning aligned; implementation/review pending. |
| `T07 GMAIL` | Sanitized notification and retry/freeze contract. | Planning aligned; implementation/review pending. |
| `T08 OPS` | systemd/timer/cron migration, stabilization, rollback/cutover gate. | Planning aligned; live execution requires explicit approval. |

T01–T08 must consume the ARCH/SEC/RES/OPS contracts above; they are not a replacement for ARCH-001–004 or their downstream tickets.

## 4. Long-term product goals

| Goal | Outcome | Priority | Current state | Ticket/work items |
|---|---|---:|---|---|
| G0 Release and health correctness gate | Prove source -> baked release -> running process, detect stale/degraded services, and preserve rollback evidence. | P0 | Foundation exists but is manual; must gate all deployments. | T0135, T0138, T0140, T0142, ARCH-001 |
| G1 Private Telegram and job health | One allowlisted user, `/status`, restart recovery, terminal job states, and failure/recovery alerts. | P1 | `/kmdaily` exists; health/waiter contract still needs consolidation. | T0051, T0127, T0131; KMDaily health follow-up |
| G2 Knowledge and briefing workflow | Reliable `/klib`, `/ingest`, KMDaily trigger, background completion, and readable MCP results. | P1 | Most command features are merged on the release branch; E2E acceptance remains. | T0079, T0081, T0084, T0085, T0086, T0088, T0127, T0131, T0133, T0136; T0152 proposed |
| G3 Safe remote coding-agent workflow on Spark | Isolated worktrees, TaskRouter leases, supervised Claude/Codex/AGY runners, and explicit HITL. | P1 | ARCH-001 is deployed but not mainline-complete; independent consensus remains unresolved because Claude was unavailable and the latest AGY packet was path-invalid. | web_gate/CLI bridge, ARCH-001 |
| G4 Mobile HITL for destructive operations | Durable approval state, expiry, denial, and recovery across restarts. | P2 | Approval hooks exist; durable runtime-state wiring is in ARCH-001 rebuild. | ARCH-001; approval lifecycle tests |
| G5 Voice/file handoff | Real deployment voice/file transfer with observable completion and rollback. | P3 | No current release ticket verified. | Proposed |
| G6 Team Telegram bot | Pairing, per-user sessions, groups, and isolation. | P3 | Deferred; no current release ticket verified. | Proposed |
| G7 Durable memory and knowledge continuity | External memory provider, safe trim persistence, and correct recency/eviction. | P2 cross-cutting | Separate fetched branches; not yet release-merged. | agentmemory provider, memory consolidation, memory eviction branches |
| G8 Multi-bot collaboration | Bot-to-bot coordination with bounded authority and auditability. | P4 | Explicitly last. | Proposed |

## 5. Ticket inventory and disposition

### Release-merged / available on the current DGX release source

| Ticket | Delivered capability | Goal | Disposition |
|---|---|---|---|
| T0051 | Telegram `/kmdaily` on-demand trigger plugin | G1/G2 | Merged on `origin/main`; verify presence in the release source before deployment. |
| T0079 | Telegram `/klib` command | G2 | Merged history. |
| T0081 | `/klib` deduplication and read command | G2 | Merged history. |
| T0084 | `/klib` MarkdownV2 and semantic mode | G2 | Merged history. |
| T0085 | Telegram callback-keyboard extension | G2 | Merged history. |
| T0086 | `/klib` pagination | G2 | Merged history. |
| T0088 | MarkdownV2 double-escaping fix | G2 | Merged history. |
| T0105 | Reject bracket-wrapped final responses | G1/G2 | In release history. |
| T0110 | Clarify ingested-document context semantics | G2 | In release history. |
| T0111 | Validate terminal timeout input | G1/G3 | In release history. |
| T0127 | Telegram `/ingest` and session chat/message binding | G2 | In release source. |
| T0131 | Proactive `/ingest` completion notice and background polling | G1/G2 | In release source. |
| T0133 | Format direct KLIB MCP results | G2 | In release source. |
| T0135 | Bake-content, push-provenance, and security-fix registry audit | G0 | In release source; report-only tooling. |
| T0136 | Parse concatenated KLIB MCP search results | G2 | In release source. |
| T0138 | Persisted MCP health guard and Telegram alerting | G0/G1 | In release source; units are not automatically enabled. |
| T0140 | `RELEASE_COMMIT` stale-code detection for release snapshots | G0 | In release source. |
| T0142 | Manual release-bake SOP | G0 | In release source; process remains manual. |

### Branch-only or not yet release-merged

| Work item | Branch/evidence | Status and next action |
|---|---|---|
| T0108 | `origin/feature/klib-orchestration-integration` | Media cache cleanup for audio/video/screenshots; review whether to promote to release source. |
| Agentmemory provider | `origin/feature/agentmemory-provider-v2026.8.3` | Separate provider implementation; live evidence was reported in its commit, but it is not release-merged. |
| Memory trim durability | `origin/fix/memory-consolidation-stuck-v2026.8.3` | Push trimmed entries to external memory with bounded fallback; review and promote separately. |
| Memory eviction order | `origin/fix/memory-eviction-order-v2026.8.3` | Move touched entries to the tail; review and promote separately. |
| Test log isolation | `origin/fix/test-log-isolation-v2026.8.3` | Prevent test logs leaking into production logs; assess release relevance. |

### Rebuilt / review-gated

| Ticket | Current state | Required next gate |
|---|---|---|
| ARCH-001 | Commit `f0dd130c835fcd5f2ca94e0f091305ded51d07c9`; 29 focused tests passed on Windows and DGX staging; Ruff/compileall/diff-check passed. AC1 checksum bake and AC2 origin provenance passed. Live release: `v2026.8.3-arch-001-f0dd130c8`; fingerprint, process cwd, health guard, and AC3 audit verified. | Execute `ARCH-001-MAINLINE-001`: reconcile refs, isolate a clean main-based diff, complete review, merge to Hermes `main`, then re-bake/redeploy from mainline. |
| ARCH-001-MAINLINE-001 | Reconcile release branch drift and isolate ARCH-001 for canonical Hermes `main` integration. | `NEEDS_RECONCILIATION`; Codex and AGY agree that the current ARCH branch is ~5,143 commits ahead of main and contains a later unrelated merge. Create a clean main-based worktree before any merge. External Windows planning paths are context only, not repo evidence. |

### Architecture-series status and proposed work

| Item | Purpose | Rule |
|---|---|---|
| T0152 | KMDaily/KLIB end-to-end acceptance and operational completion path. | Treat as draft only until the authoritative ticket source and acceptance criteria are located. |
| ARCH-002 / ARCH-004 | Audit/replay and SQLite/WAL safety contracts. | Planned P0 work after ARCH-001; do not claim implementation from the current refs. |
| SEC-001 / SEC-002 | Shell policy, approval state, and denial breaker. | Planned P0 work after the ARCH contracts; not implemented in current refs. |
| RES-001 / RES-002 | Tool recovery and async compression/state reinjection. | Planned P0 work after their stated dependencies; not implemented in current refs. |
| OPS-001 | Heartbeat and bounded stall watchdog. | Planned P0 work after recovery and storage contracts; not implemented in current refs. |
| Voice/file handoff | G5 product capability. | Create a ticket only after G0/G1 operational acceptance is stable. |
| Team bot and multi-bot | G6/G8 product capabilities. | Deferred; no implementation work should pre-empt G0-G4. |

## 6. Engineering delivery sequence

| Step | Capability | Current position | Exit condition |
|---:|---|---|---|
| E0 | Source/release provenance and health gate | Partially delivered by T0135/T0138/T0140/T0142; manual SOP remains. | Source hash, release marker, running process, health result, and rollback evidence agree. |
| E1 | Capability probes and adapter interfaces | Web gate and coding-CLI bridge exist. | Each external runner has bounded timeout, terminal-state, and error contract. |
| E2 | TaskRouter, isolated worktrees, SQLite leases | ARCH-001 is the current workstream. | Durable profile/session/task/approval state passes independent review and restart tests. |
| E3 | Runner Supervisor | A stale waiter incident showed that `Terminated`, cancelled, timeout, and nonzero outcomes must be terminal. | No waiter polls forever; every runner has bounded cleanup and evidence. |
| E4 | Claude/Codex/AGY adapters and approval integration | Partial; ARCH-001 approval hooks are in rebuild. | Approval expiry/deny/restart recovery are durable and fail closed. |
| E5 | Thin `/goal` routing | Preview/goal-routing work exists in prior planning, but current release acceptance is not confirmed. | One bounded goal route with explicit preview, approval, and audit evidence. |
| E6 | Spark/Telegram E2E and health monitoring | Pieces exist; end-to-end acceptance remains. | Restart, failure, recovery, KLIB health, KMDaily, and rollback smoke all pass. |

The v0.20 dependency order is: `ARCH-001` -> `ARCH-002` -> `ARCH-004`; `ARCH-003` may proceed in parallel with `ARCH-001`; then `SEC-001` -> `SEC-002`; `RES-001` and `RES-002` proceed in parallel once their contracts are ready; `OPS-001` follows all of them. `INT-005` and `A2A-006` remain later phases.

## 7. Review, implementation, and deployment loop

1. Revise the existing ticket/plan against the current release target.
2. Obtain user approval for the revised implementation scope.
3. Send independent Claude and AGY reviews with objective, scope, environment, and expected changes.
4. Reconcile findings; record blockers and unresolved review status.
5. Re-review the revised plan until consensus or an explicitly accepted unresolved blocker is recorded.
6. Implement in an isolated worktree; run focused and regression tests.
7. Commit and push only after the user authorizes publication.
8. Perform DGX read-only preflight, backup/restore rehearsal, staged bake, smoke verification, and rollback readiness.
9. Deploy only after separate deployment approval; record source hash, release marker, process path, service status, health output, and rollback evidence.

## 8. Immediate execution queue

1. Execute `ARCH-001-MAINLINE-001`: clean main-based integration, review, merge, and re-bake/redeploy.
2. Independently inspect the ARCH-003 reference design and reconstruct its authoritative acceptance criteria; do not import the delegated branch blindly.
3. Draft/review ARCH-002 after ARCH-001 is mainline-complete; then implement/review ARCH-004.
4. Implement/review SEC-001 and SEC-002, followed by RES-001 and RES-002 in parallel where dependencies permit.
5. Implement/review OPS-001 and connect the generic runner waiter terminal-state contract exposed by the KMDaily incident.
6. Maintain T0138/T0142 release-bake and health evidence, including the ARCH-001 rollback backup.
7. Decide whether branch-only T0108 and memory branches should be promoted into the release source, then define/confirm T0152 acceptance criteria.
8. Run KMDaily/KLIB E2E and defer INT-005/A2A-006 until the P0 architecture loop is accepted.

## 9. Explicit non-claims

- ARCH-001 is committed at `f0dd130c8`, pushed to `origin/agent/arch-001-dgx-release-rebuild`, and deployed as `v2026.8.3-arch-001-f0dd130c8` with runtime evidence; it is not claimed complete until merged to Hermes `main` and re-baked from mainline.
- A ticket being present in a release branch does not prove it is running on DGX.
- A successful one-shot KMDaily cycle does not prove the full job-monitoring contract.
- Synthetic tests and local pytest results do not replace DGX smoke, health, or rollback evidence.
- No Claude/AGY consensus is claimed for the rebuilt ARCH-001 until those independent reviews are completed.
