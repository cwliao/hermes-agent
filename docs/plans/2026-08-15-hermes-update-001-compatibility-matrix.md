---
title: "HERMES-UPDATE-001 compatibility matrix"
status: MATRIX_INCOMPLETE_GATED
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
| User state and credentials | `config.yaml`, `auth.json`, credential files, `state.db`/WAL, sessions, memories, skills, plugins, pairing, cron, logs | Redacted pre/post manifest; no secret or message content in artifacts | PARTIAL_PASS: DGX baseline manifest captured | Post-change manifest remains not run; block promotion |
| SQLite/session continuity | WAL mode, schema identity, session readback, `PRAGMA integrity_check=ok` | Temp `HERMES_HOME` migration/readback and integrity evidence | PARTIAL_PASS: private 382 passed; upstream 257 passed | Protected-state post-check and candidate decision still required |
| Prompt/cache invariants | Byte-stable system prompt and per-conversation cache prefix | Focused cache boundary tests plus real import path | PARTIAL_PASS: 29 passed | Candidate comparison still required |
| Message loop | Strict role alternation and no synthetic user insertion | Conversation-loop invariant tests | PARTIAL_PASS: 37 passed | Candidate comparison still required |
| Tool and skill discovery | Built-in tools, user skills, optional skills, skill command routing | Discovery/import matrix with missing/import-error failure | PARTIAL_PASS: 4 general discovery tests passed | Candidate comparison still required |
| Cron | Job parsing, scheduling, lock, catch-up, output and delivery isolation | Parse/load matrix and scheduler tests | PARTIAL_PASS: 4 passed | Candidate comparison still required |
| Plugins | Memory, model/provider, platform and KLIB/KMDaily plugin registration | Import/registration matrix; no credential reads | PARTIAL_PASS: general 4, browser 31, web 50 passed | Candidate comparison still required |
| Telegram transport | Pairing/authz, polling progress, reconnect, inbound dispatch, outbound send | Authorized inbound/outbound evidence and recovery test | NOT_RUN | Block promotion |
| DGX SSH/config resolution | `140.96.58.171`, hostname `55-0940189-03`, WSL authenticated SSH path | Resolver tests and metadata-only DGX probe | NOT_RUN | Block promotion |
| Release/rollback | Immutable release marker and prior-release selection | Clean candidate snapshot, quantitative rollback checklist | PASS (plan only) | Separate rollback authorization |
| Calendar/reliability guards | Calendar guard, gateway recovery, tool-loop/artifact truth, CI baseline | Private-fork feature tests and explicit port/adapt decision | NOT_RUN | Block promotion |
| Terminal/browser surfaces | Existing tool safety and browser/terminal resolution | Focused smoke tests in isolated candidate | PARTIAL_PASS: browser/web suites 81 passed | Candidate comparison still required |

## Execution evidence

The following checks ran against the isolated private-fork worktree using a
locked Windows `.venv` created with the repository's `uv.lock` and the `dev`
plus `anthropic` extras. They are source-level baseline evidence only; they do
not prove upstream compatibility or DGX readiness:

| Check | Result | Evidence boundary |
| --- | --- | --- |
| `tests/cron/test_cron_profile_isolation.py` + provider/candidate suites | `10 passed` | Cron, general discovery, and inventory behavior only |
| `tests/run_agent/test_anthropic_prompt_cache_policy.py` | `29 passed` | Prompt/cache policy import and focused invariants only |
| `tests/gateway/test_config_driven_access_policy.py` | `71 passed` | Gateway access-policy behavior only |
| `tests/plugins/browser/test_browser_provider_plugins.py` | `31 passed` | Browser plugin import/registration behavior only |
| `tests/plugins/web/test_web_search_provider_plugins.py` | `50 passed` | Web plugin import/registration behavior only |
| `tests/test_hermes_state.py` + `tests/test_hermes_state_wal_fallback.py` | `382 passed` | State/WAL/session behavior in isolated temp homes only |
| `tests/run_agent/test_message_sequence_repair.py` | `37 passed` | Message-sequence invariant behavior only |
| `tests/run_agent/test_primary_runtime_restore.py` | `36 passed` | Provider recovery behavior after installing locked `anthropic` extra |
| `tests/hermes_cli/test_backup.py` | `138 passed`, `4 failed`, `3 skipped` | Windows wrapper/path/permission semantics remain unresolved |
| `tests/test_hermes_constants.py` | `103 passed`, `15 skipped`, `5 failed` | Windows `HERMES_HOME`/symlink privilege semantics remain unresolved |
| `tests/test_atomic_replace_symlinks.py` | `6 passed`, `5 failed`, `6 skipped` | Five symlink cases require Windows symlink privilege |

The same focused suites were then run against the clean detached upstream
candidate at `45af7a71fcd420b4422d2c074b1ce58b9ce0d048` using its own locked
environment (`hermes-agent 0.20.1`, `pytest 9.1.1`, `anthropic 0.87.0`):

| Candidate check | Result | Evidence boundary |
| --- | --- | --- |
| Prompt/cache policy | `40 passed` | Upstream candidate source behavior only |
| Gateway access policy | `53 passed` | Upstream candidate gateway policy only |
| Browser provider plugins | `23 passed` | Upstream candidate plugin behavior only |
| Web search provider plugins | `28 passed` | Upstream candidate plugin behavior only |
| State/WAL/session suites | `257 passed`, `2 skipped` | Upstream candidate isolated temp homes only |
| Message-sequence repair | `16 passed` | Upstream candidate message invariant only |
| Primary runtime restore | `24 passed` | Upstream candidate provider recovery only |
| Backup suite | Linux `51 passed`, `1 warning` | Linux candidate PASS; Windows-native cases are out of scope |

The Windows-only boundary was validated separately in the same detached
upstream candidate through WSL Linux using Python 3.11.15 and the upstream
locked environment:

| WSL/Linux candidate check | Result | Evidence boundary |
| --- | --- | --- |
| Backup suite | `51 passed`, `1 warning` | Linux deployment-target behavior; PASS |
| Atomic symlink suite | `21 passed`, `4 skipped` | Linux deployment-target behavior; PASS |
| Hermes constants suite | `61 passed`, `5 skipped` | Linux deployment-target behavior; PASS |

The canonical `scripts/run_tests.sh` is a POSIX shell runner and was not
executed from native PowerShell; the recorded fallback used the isolated
Windows `.venv` directly. The isolated environment installed only locked
test/provider extras and no product code was changed to hide environment
failures. Windows-native portability is explicitly out of scope for the
declared Linux deployment target. The candidate remains
`RETAIN_PRIVATE_RELEASE` until the post-change state manifest, private-feature
adaptation decision, and Telegram/DGX evidence are complete.

## Candidate decision

The selected decision is `RETAIN_PRIVATE_RELEASE`. The clean upstream
candidate focused suites are recorded, but upstream adoption and a
private-fork merge/cherry-pick remain unevaluated until the matrix is complete.
No update target may be the dirty DGX checkout. The candidate must be built
from a clean isolated source ref and carry a full 40-character source SHA.

## Gate separation

This implementation changes source-side inspection and documentation only.
The following remain separate and currently open: state backup evidence,
matrix completion, candidate tests/CI, merge, immutable DGX release creation,
service restart, runtime health, Telegram inbound, and Telegram outbound.
