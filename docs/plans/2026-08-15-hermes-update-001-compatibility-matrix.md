---
title: "HERMES-UPDATE-001 compatibility matrix"
status: IMPLEMENTATION_IN_PROGRESS
date: 2026-08-15
type: operations/reliability
ticket: HERMES-UPDATE-001
---

# HERMES-UPDATE-001 compatibility matrix

This matrix is the implementation boundary for the upstream update. It does
not authorize a DGX change. A row is `PASS` only when the candidate test and
the corresponding protected-state evidence are recorded; `NOT_RUN` keeps the
candidate at `RETAIN_PRIVATE_RELEASE`.

## Source identities

| Source | Ref/SHA | Role |
| --- | --- | --- |
| Private fork | `origin/main` / `2edfacec61599317e9759d0cd2c47c0d87d6b6f2` | Current fork baseline |
| Upstream | `upstream/main` / `45af7a71fcd420b4422d2c074b1ce58b9ce0d048` | Candidate source only |
| DGX runtime | release `v2026.8.15-hermes-telegram-transport-77bcb5d0717e` | Current deployed baseline |
| DGX dirty checkout | `/home/cwliao/project/hermes-agent`, `tmp-wal-close`, `a13f4b8a6b52537fe27dadacf48442e287c6ccea` | Protected; never an update source |

The refs are not fast-forward compatible. The initial inventory was 119
private-only commits, 7,420 upstream-only commits, and 7,024 changed paths.
The source inventory is reproducible with:

```text
python scripts/update_candidate.py --private-ref origin/main --upstream-ref upstream/main
```

The command is metadata-only: it does not fetch, merge, reset, create a
worktree, read `.hermes`, or contact DGX. Its policy is to retain the private
release until every required row below is `PASS`.

## Required compatibility rows

| Area | Private/DGX contract to preserve | Candidate evidence required | Status | Decision |
| --- | --- | --- | --- | --- |
| Launcher identity | `systemctl --user` unit, immutable release path, exact cwd/PYTHONPATH, release marker | Candidate process/unit tuple matches the captured deployment tuple | PASS (baseline only) | Keep current release until deployment gate |
| User state and credentials | `config.yaml`, `auth.json`, credential files, `state.db`/WAL, sessions, memories, skills, plugins, pairing, cron, logs | Redacted pre/post manifest; no secret or message content in artifacts | NOT_RUN | Block promotion |
| SQLite/session continuity | WAL mode, schema identity, session readback, `PRAGMA integrity_check=ok` | Temp `HERMES_HOME` migration/readback and integrity evidence | NOT_RUN | Block promotion |
| Prompt/cache invariants | Byte-stable system prompt and per-conversation cache prefix | Focused cache boundary tests plus real import path | NOT_RUN | Block promotion |
| Message loop | Strict role alternation and no synthetic user insertion | Conversation-loop invariant tests | NOT_RUN | Block promotion |
| Tool and skill discovery | Built-in tools, user skills, optional skills, skill command routing | Discovery/import matrix with missing/import-error failure | NOT_RUN | Block promotion |
| Cron | Job parsing, scheduling, lock, catch-up, output and delivery isolation | Parse/load matrix and scheduler tests | NOT_RUN | Block promotion |
| Plugins | Memory, model/provider, platform and KLIB/KMDaily plugin registration | Import/registration matrix; no credential reads | NOT_RUN | Block promotion |
| Telegram transport | Pairing/authz, polling progress, reconnect, inbound dispatch, outbound send | Authorized inbound/outbound evidence and recovery test | NOT_RUN | Block promotion |
| DGX SSH/config resolution | `140.96.58.171`, hostname `55-0940189-03`, WSL authenticated SSH path | Resolver tests and metadata-only DGX probe | NOT_RUN | Block promotion |
| Release/rollback | Immutable release marker and prior-release selection | Clean candidate snapshot, quantitative rollback checklist | PASS (plan only) | Separate rollback authorization |
| Calendar/reliability guards | Calendar guard, gateway recovery, tool-loop/artifact truth, CI baseline | Private-fork feature tests and explicit port/adapt decision | NOT_RUN | Block promotion |
| Terminal/browser surfaces | Existing tool safety and browser/terminal resolution | Focused smoke tests in isolated candidate | NOT_RUN | Block promotion |

## Candidate decision

The selected decision is `RETAIN_PRIVATE_RELEASE`. Upstream adoption and a
private-fork merge/cherry-pick remain unevaluated until the matrix is complete.
No update target may be the dirty DGX checkout. The candidate must be built
from a clean isolated source ref and carry a full 40-character source SHA.

## Gate separation

This implementation changes source-side inspection and documentation only.
The following remain separate and currently open: state backup evidence,
matrix completion, candidate tests/CI, merge, immutable DGX release creation,
service restart, runtime health, Telegram inbound, and Telegram outbound.
