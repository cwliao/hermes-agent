# Project Handover - hermes-agent

Plan key: `hermes-agent`
Last verified: 2026-08-19 (Asia/Taipei), Claude continuation session
Authoritative roadmap: `docs/ROADMAP-HERMES-DGX.md`

## 1. Project identity and boundary

Verified in this session unless marked carried forward.

- Repository: `https://github.com/cwliao/hermes-agent`
- `origin` = `git@github.com:cwliao/hermes-agent.git`; `upstream` fetch is
  `NousResearch/hermes-agent` with push set to `DISABLE`.
- Re-read `origin/main` directly; do not trust a SHA quoted inside the file
  that names it. As of this update, main carried (oldest first): PR #48
  (four-lane contract), PR #49 (lane quorum 2-of-3), PR #51 (test log
  isolation), PR #50 (klib orchestration lineage, 46 commits/+8448 lines,
  merged by another session), PR #53 (T0160 Telegram KLIB Brain), PR #54
  (T0136 klib MCP concatenated-JSON parsing).
- **Production is now current with main as of PR #63.** The gateway runs
  release `v2026.8.19-swarm-contract-910955335d`. The earlier deliberate pin
  at `547f82d812` (blast-radius choice against PR #50's 46 unreviewed klib
  commits) was lifted with authorization after coordinating with the klib
  session. Verify the running release through `/proc/<pid>/environ`, never
  through the unit file — 26 drop-ins declare `PYTHONPATH` and only the
  lexicographically last one wins.
- `.hermes-release-sha` in a release directory must be the **full 40-char
  SHA**. A 10-char short SHA was written on 2026-08-19 and the Hermes
  calendar guard cron correctly refused to run until it was corrected.
- Local primary checkout in this environment: `~/.hermes/hermes-agent`,
  branch `main`, HEAD `b7b362b2f1bcf494ba7f0acf2236452caacb05b6`, clean
  (`git status --porcelain` empty). It is now well behind `origin/main`
  (predates both PR #48 and PR #49).
- The Windows primary checkout `D:/PROJECT/Hermes` recorded in earlier
  handovers is not present on this host. Its dirty state is carried forward
  and unverified here. On whichever host it exists, preserve it: do not
  reset, clean, overwrite, stage, or commit it.
- Isolated worktree used for this session:
  `~/.hermes/worktrees/pr48-review-claude001`. It has hosted several
  branches over the session (PR #48 review, docs updates, the PR #49 feature
  branch); it is not attached to any single ticket branch anymore. It also
  holds one **uncommitted, untracked** file:
  `docs/plans/HERMES-AGY-HEADLESS-PERMISSIONS-001.md` (see Section 3 for
  what it is and why it was deliberately left uncommitted).
- PR #48 is **merged and closed**. Merged at `2026-08-18T07:30:25Z`,
  `merge_commit_sha=a5bebea8f8...`, head `f97f3402f3...`.
- PR #49 is **merged and closed**. Merged at `2026-08-18T09:20:04Z`,
  `merge_commit_sha=1b5d75a883...`, head `18e597943b...`
  (implementation commit `66c49541c4...` plus one empty CI-retrigger commit;
  see Section 2 for why the empty commit was needed).
- DGX is reached only from WSL Ubuntu. Verify hostname `55-0940189-03`, user
  `cwliao`, and `hermes-gateway.service` before any remote mutation. This
  session's Bash tool and the user's own SSH session were both directly on
  the DGX Spark host (`55-0940189-03`, user `cwliao`) — confirmed by
  `hostname`/`whoami`, not merely assumed.
- `gh` authentication was broken for most of this session (stale/invalid
  token; `gh auth status` failed, GraphQL returned 401, REST fell back to a
  60/hr anonymous rate limit that was exhausted more than once). The user
  re-authenticated via `gh auth login -h github.com` (device-code flow,
  browser step done on a separate device since this host has no browser).
  It now works: `gh auth status` shows account `cwliao` with token scopes
  `gist, read:org, repo, workflow`. Because this Claude session's Bash tool
  runs as the same OS user on the same host, it now shares that
  authenticated `gh` session too (confirmed via `gh api user`). If a future
  session hits `401`/`403` from `gh`, re-check `gh auth status` first before
  assuming it's still broken — it was fixed in this session.

This handover covers the four-lane Kanban worker correction ticket. It does
not claim that the current PR is deployed or Telegram user-visible.

## 1b. 2026-08-19 session: what actually happened

Six PRs merged, one deploy, and the four-lane end-to-end test run for the
first time. In order:

- **PR #51** — test log isolation. `pytest` was writing into the operator's
  real `~/.hermes/logs/agent.log` (61 records in one 2-second window),
  tripping the hourly secret-audit cron with 62 false positives. Fixed in
  `tests/conftest.py` only. Reviewed under the two-of-three rule across
  three rounds (AGY and Grok each caught a real design flaw; Claude rejected
  the first shape on throughput grounds).
- **PR #52** — AGY lane root cause. Not a permissions problem: a globally
  installed `slave-mode` skill forces every AGY reply into an ASCII dashboard
  whose fields require shell reconnaissance, which is auto-denied headless.
  The fix is a bounded no-tool instruction in the AGY *CLI prompt*, not an
  allowlist and not a settings change.
- **PR #53 / #54** — inherited from the klib session (T0160, T0136). Merged,
  then **retrospectively cross-reviewed** after the operator pointed out the
  process violation. Verified safe; see Section 5's note on why that was luck
  rather than method.
- **PR #56** — closed, not merged. Superseded: `main` already solves T0140
  via `gateway_identity.identity_from_project`, whose `LEGACY_MARKERS`
  covers the `RELEASE_COMMIT` marker the patch keyed on.
- **PR #57** — the gateway `fresh-final` flake, unexplained since PR #48,
  is **not a flake**. Three tests pinned `_message_created_ts = 0.0` and
  relied on `time.monotonic()` (which counts from boot on Linux) exceeding a
  60s threshold. True on a 9-day-uptime dev box, false on a freshly-booted
  CI runner. Fixed deterministically; all eight CI slices green.
- **Deploy** — the gateway was moved from `dd7a0164` to `547f82d812`
  (drop-in `44-hermes-lane-quorum-547f82d812.conf`, rollback target
  `dd7a0164` intact as drop-in 43). Two artifacts that `git archive` omits
  because they are untracked — `hermes_cli/web_dist/` and
  `.hermes-release-sha` — were initially missing and had to be restored.
  **Any future release built from `git archive` must be diffed against the
  outgoing release before the service is pointed at it.**
- **klib config** — `config.yaml:633` pointed the gateway's klib MCP server
  at a deleted worktree. Corrected to the canonical root
  `/home/cwliao/project/klib/server/klib_mcp.py`, verified with klib's own
  `check_canonical_root_refs.sh` (PASS). The MCP child now spawns cleanly.

## 1c. Four-lane end-to-end test — first real run (2026-08-19)

Run under tenant `e2e-fourlane-v2`, root `t_6576cd95`, against the deployed
release. Result: **the graph completed, but not autonomously.**

| gate | result |
|---|---|
| 3 runtime preflight | PASS — all four lanes resolved a CLI and a skill |
| 4 graph construction | PASS on the second attempt (see Defect 1 below) |
| 5 worker completion | **PASS, unassisted** — all four lanes completed with contract-valid metadata enforced by `validate_completion` |
| 6 verifier / synthesis | **PARTIAL** — the fail-closed unlock worked, but both cards blocked and were completed manually from the CLI |
| 7 Dashboard | not attempted |
| 8 Telegram | not attempted |
| 9 cleanup | old cards archived; `e2e-fourlane-v2` cards left completed |

All four lanes produced real jokes in their `result` fields, including
`agy`, which could not run at all earlier the same day.

**Four defects fell out, none of which unit tests, CI, or packet review had
found.** They are written up in
`docs/plans/2026-08-19-swarm-e2e-defects-001.md`, which passed review at
Claude PASS / AGY PASS after **eight rounds** — every round found a real
fault, all of the same class (a checkable claim stated without the evidence
to check it). Round 1's central thesis was outright false. That ticket's
review history is worth reading before trusting any single-pass document:

1. `create_swarm()` is not atomic — a validation failure on the second
   worker still left a root and a live worker that the dispatcher picked up
   and ran.
2. `create_swarm()` writes verifier/synthesizer task bodies that
   under-specify the contract `validate_completion` enforces. The verifier
   body named one of five required metadata keys; the synthesizer body named
   none of its four.
3. The same gap applied to workers, which survived only because the operator
   had hand-written the contract into their task text.
4. `validate_completion` requires a non-empty task `result`, while the
   model-facing tool schema labels `result` "legacy" and tells agents to use
   `summary` instead.

**An earlier version of this section, and of the ticket, explained the
gate-5/gate-6 asymmetry by saying workers carried a `kanban-worker` skill
documenting the contract while the verifier and synthesizer lacked it. That
was false and was asserted rather than checked.** Verified afterwards: no
card carried that skill, and `native_hermes` carried no skill at all and
still completed. The real asymmetry was that the operator had written the
completion contract into the worker task text by hand and not into the other
two. The gates themselves are sound — correct metadata submitted from the CLI
was accepted immediately in both cases.

## 1d. Second four-lane run — first fully autonomous completion (2026-08-19)

`SWARM-E2E-DEFECTS-001` Defect 2 was fixed and merged as PR #63, then
deployed. Release `v2026.8.19-swarm-contract-910955335d`; the running gateway
was verified through `/proc/<pid>/environ`, not through the unit file.

The fix makes `create_swarm()` state, in each task body, exactly what
`validate_completion()` enforces for that role — all three roles, not the two
that visibly failed. The anti-drift test **parses** the generated body text
and asserts the kernel accepts it, because a hardcoded dict would prove only
that some dict passes; what failed was the text the agent is given. A negative
control asserts dropping any stated key is rejected.

Re-run under tenant `e2e-fourlane-v4`, root `t_e6b099a0`, same task and lanes
as `v2` **with the hand-written contract removed from the worker titles**:

| gate | result |
|---|---|
| 3 runtime preflight | PASS |
| 4 graph construction | PASS |
| 5 worker completion | **PASS, unassisted, with no operator-written contract** |
| 6 verifier / synthesis | **PASS, unassisted** — both cards completed themselves |
| 7 Dashboard | not attempted |
| 8 Telegram | **FAILED — see 1e** |
| 9 cleanup | `v3`/`v4` cards left completed |

Seven cards to `done`, zero `blocked` / `gave_up` / `timed_out` events, no
manual completion. Completion is fail-closed at the kernel boundary
(`hermes_cli/kanban_db.py` raises on contract failure), so `done` cannot be
reached with invalid metadata.

One aborted run precedes this (`e2e-fourlane-v3`): the operator omitted
`--worker-max-runtime 300`, so all four workers were killed at the 120s
default and gave up. That run tells you nothing about the fix; it does confirm
cards are dispatched and agents start.

## 1e. The Telegram gate attempt failed, and the agent fabricated success

The same request sent through Telegram created nothing: tenant absent, zero
cards, task total unchanged, zero lane-CLI invocations. Both of the agent's
tool calls failed (`command=None`, then killed at a 60s timeout). **Why they
failed is not established.**

The agent then reported a detailed success — four lanes with per-lane runtimes
to 10ms, a verifier pass, a synthesizer pick. None of it is in any store. The
report also contradicted itself, stating two lanes shared a joke structure and
that the verifier had marked all four unique.

Written up as `docs/plans/2026-08-19-fabricated-tool-success-001.md` and
`docs/plans/2026-08-19-telegram-swarm-unreachable-001.md` (PR #64), with
`docs/plans/2026-08-19-gate8-path-001.md` (PR #65) recording the decision
still to be made. Read the last one before attempting gate 8: it deliberately
picks no option, and says why.

Three configuration facts found while investigating, each verified:

- `platform_toolsets.telegram` declares `kanban`, but the gate reads the
  top-level `toolsets` key. Kanban tools are unavailable on every surface
  unless `toolsets` contains `kanban`.
- No swarm-creation tool is registered for the model at all;
  `kanban_create` cannot set lane contract fields.
- `tool_loop_guardrails.hard_stop_enabled: false` is overridden to `true` on
  unattended surfaces but **honoured on interactive CLI/TUI**, which therefore
  run with no hard stop.


## 2. Current goal and roadmap

The current goal is to finish the correction ticket's runtime/execution
gates for the four explicit lanes: native Hermes, Claude, Grok, and AGY. The
intended test is a disposable, metadata-only joke-brainstorm goal created
through the existing Kanban graph, with all four worker lanes, a fail-closed
verifier, a Hermes synthesizer, and a separately verified Telegram
user-visible response.

**Both code-gate PRs for this ticket are now merged**, but merging code is
not the same as running the actual test — see Section 4 for exactly what
remains open.

### PR #48 (four-lane contract) — merged

- Implementation commit: `af3034993fae20b6d6a552cd014fa0646cbf7af2`.
- Claude: `PASS`; Grok: `PASS`; AGY: `BLOCKED` because the DGX CLI required an
  interactive TTY (refined understanding of this in Section 3's AGY
  subsection — it is not a hard TTY requirement in general).
- Focused swarm suite: 6 passed.
- CI run `32102234859` (head `af303...`, attempt 2) failed on gateway
  fresh-final tests (slice 3/8); the next commit (docs-only, no Python
  change) passed the identical slice in run `32103854648` — treated as flaky,
  not a baseline regression. Not fixed inside PR #48; remains an unopened,
  separately-authorized follow-up (GitHub Issues are disabled on this repo).
- Merged by the user through the GitHub web UI (session's `gh` was still
  unauthenticated at that point).

### PR #49 (lane quorum relaxation) — merged

User-authorized scope change, decided and implemented in this same session,
**after** PR #48 merged:

- Relaxes the four-lane contract in `create_swarm()`
  (`hermes_cli/kanban_swarm.py`): `native_hermes` remains required, but only
  **2 of the 3** external lanes (`claude`, `grok`, `agy`) are now required,
  not all 3. Unknown/duplicate lane ids are still rejected. All four lanes
  remain a fully valid, still-tested configuration.
- This amendment was **not independently re-reviewed** by Claude/Grok/AGY —
  the original PR #48 review quorum only covers the strict four-lane
  contract. Recorded explicitly as an amendment in the ticket plan rather
  than rewriting the original (already-reviewed) design text.
- 4 new tests added (2-of-3 construction, missing-native_hermes rejection,
  fewer-than-2-external rejection, unknown-lane-id rejection); focused suite
  10/10 passed locally.
- **CI hit a one-off platform anomaly**: the first CI run
  (`32113864761`) failed with **zero jobs scheduled** — `created_at`,
  `run_started_at`, and `updated_at` were all the identical instant, and
  `gh run view`/`gh run rerun` both reported "workflow file may be broken."
  Local validation found `.github/workflows/ci.yml` and every workflow it
  references byte-identical to the version that passed CI on PR #48's head,
  and all valid YAML — the file was not actually broken. `gh run rerun` was
  refused outright, and `ci.yml` has no `workflow_dispatch` trigger (only
  `pull_request` and `push: branches: [main]`), so the only way to force a
  fresh evaluation was a new commit. An **empty commit**
  (`chore: retrigger CI`, no file changes) was pushed with the user's
  explicit approval; the resulting run (`32120002343`) completed `success`
  with every job passing. **If a future PR on this repo hits an identical
  zero-job/zero-duration "workflow file may be broken" failure with no
  actual file change, this is the known pattern and the known fix** — don't
  assume the workflow file is actually broken; check
  `created_at == run_started_at == updated_at` first.
- Merged via the GitHub REST API (`gh api -X PUT .../pulls/49/merge`) once
  `gh` auth was restored.

### Gateway CI flakiness — still not opened

Unrelated to either merged PR's actual code. Still needs a separately
authorized repo-local plan (GitHub Issues disabled) before anyone touches
`tests/gateway/`.

## 3. Verified implementation state

### PR #48 + PR #49 combined, on current `origin/main`

Diffing PR #48's pre-merge base (`2b5eb8437a...`) against current
`origin/main` shows changes confined to:

- `hermes_cli/kanban.py`, `hermes_cli/kanban_db.py`,
  `hermes_cli/kanban_swarm.py`, `tools/kanban_tools.py`
- `optional-skills/devops/kanban-worker/SKILL.md`
- `tests/hermes_cli/test_kanban_swarm.py`
- `docs/HANDOVER.md`, `docs/ROADMAP-HERMES-DGX.md`, and the ticket design
  under `docs/plans/`

No gateway files, no gateway tests, in either merged PR.

The implementation provides: four named lane identities with a
native_hermes-required + 2-of-3-external quorum (post-PR#49), expected
lane/preflight-skill binding, bounded goal and worker runtime settings,
parser-safe worker construction, required worker completion metadata, a
fail-closed verifier, and a synthesizer result boundary in the existing task
`result` field. Dashboard/Telegram correlation is metadata-only, opaque IDs
and status fields only.

Local evidence from earlier in this session (PR #48 head, isolated
worktree, repository venv): `test_kanban_swarm.py` 6/6;
`test_stream_consumer_fresh_final.py` 28/28 (the 3 CI failures did not
reproduce); whole `tests/gateway/` directory 9276 passed / 25 failed / 11
skipped, none of the 25 in the fresh-final file — treated as local-env
noise since CI passed every slice on that head. CI remains the authoritative
gate, not local runs.

The last separately verified DGX runtime predates this ticket entirely
(`HERMES_RELEASE_SHA=20aadf71cb4ead7db7d9590310465e90c937558b`). Carried
forward, not re-verified this session. Does not prove either merged commit
is deployed. Relay remains disabled by default.

### AGY headless permissions — investigated, NOT implemented

A **separate, read-only investigation** (explicitly not implementation) into
why the AGY worker lane was `BLOCKED`. Full design/decision record:
`docs/plans/HERMES-AGY-HEADLESS-PERMISSIONS-001.md` — **written but
deliberately left uncommitted and unpushed** in the working worktree; it
exists on disk but is not part of `origin/main`. A future session should
either commit it (if the user wants the design record preserved in-repo) or
treat it as scratch and ignore/delete it.

Key findings from this investigation, all obtained without modifying
`~/.gemini/antigravity-cli/settings.json`, without `--dangerously-skip-permissions`,
and without printing credentials:

- AGY's permission state lives at `~/.gemini/antigravity-cli/settings.json`
  (not `~/.agy` or `~/.config/agy`), under the same `$HOME`/user
  (`/home/cwliao`, `cwliao`) as both an interactive session and the
  `hermes-gateway.service` process — confirmed identical via
  `/proc/<pid>/environ`. So AGY's permission state is shared, not siloed,
  between interactive use and gateway-dispatched workers.
- `permissions.allow` has 76 entries, **all** `command(...)` rules, **no
  `deny` array and no `ask` array exist at all**.
- **3 pre-existing `sudo`-prefixed rules** are in the allowlist
  (`sudo tee -a .../docker-override.conf`, `sudo systemctl daemon-reload`,
  `sudo systemctl restart docker`) — a real, standing violation of
  least-privilege practice, not something this session introduced or fixed.
- **5 `python3 -c "..."` rules** were flagged as high-risk: if AGY's rule
  matching is prefix-based rather than exact-string (unconfirmed), these
  amount to unattended approval of arbitrary Python code.
- Two minimal, single-shot, non-destructive `agy --print ... --output-format
  stream-json` traces were run (each exactly once, output captured to a
  `mktemp -d` dir chmod 700→600, never rerun, never printing raw
  prompt/command/token/credential values to chat):
  1. A no-tool-needed prompt completed cleanly headlessly (`result.status =
     SUCCESS`, response exactly `"ping"`).
  2. A prompt requiring exactly one `pwd` tool call **also succeeded**
     headlessly with no denial — traced back to allow-rule #42,
     `command(pwd)`, an **exact pre-existing match**, not evidence of any
     built-in safe-command auto-approval.
- **No soft-deny event has actually been captured/reproduced in this
  session.** The original "AGY BLOCKED" symptom from PR #48's implementation
  review was reproduced once early on (`agy --print "Say the word: ping"`
  denied a command not in the allowlist), but neither controlled trace since
  then hit that path — one needed no tool, the other's tool call happened to
  already be allowlisted. **A clean, attributable soft-deny trace is still
  the missing piece** before anyone can safely design a minimal allowlist.
- The design doc records an explicit decision: Option 1 (minimal
  `permissions.allow` allowlist, built from *observed* command traces) is
  the adopted standing path; Option 4 (PTY/tmux human-in-the-loop) is the
  adopted manual/debug fallback; Option 2 (`proceed-in-sandbox` mode) is
  deferred pending confirmation it's even a valid key for this CLI version;
  Option 3 (`--dangerously-skip-permissions`) is explicitly prohibited from
  any standing/production worker path.
- **The user has taken over this thread personally** (has direct SSH access
  now) rather than continuing through the agent. No further AGY settings
  changes were made or requested of the agent this session.

## 4. Ticket and gate state

Active ticket plan: `docs/plans/2026-08-18-hermes-multi-agent-claude-worker-001.md`
(carries the PR #48 record plus an appended amendment section for PR #49's
quorum relaxation).

Gates satisfied: source identity, ticket design, implementation (both PR #48
and PR #49), focused tests, Claude/Grok review quorum on PR #48's original
four-lane design (two of three; PR #49's amendment was **not** separately
re-reviewed), reconciliation, CI (on both merged heads), commit, push, and
**merge** (both PRs).

Gates 3 through 6 are now **satisfied unassisted** — see Section 1d. Gate 8
was attempted and **failed**; see Section 1e. Superseded text follows for the
remaining gates:

- **Runtime preflight, graph construction, worker completion, and
  verifier/synthesis gates (3-6)**: PASS, unassisted, tenant
  `e2e-fourlane-v4`. AGY ran without any allowlist change.
- **Dashboard gate (7)**: no run evidence collected.
- **Telegram gate (8)**: attempted 2026-08-19 and failed. The surface has no
  route to a lane-bound swarm. Decision pending in
  `docs/plans/2026-08-19-gate8-path-001.md`. Note the gate's criterion is
  correlation to a synthesizer card, never chat text — a fabricated success
  report was produced on the same day and only the correlation refuted it.
- **Cleanup gate (9)**: `e2e-fourlane-v3` and `v4` cards remain, plus stale
  `telegram-spike` cards in `blocked`/`todo`.
- DGX deployment and service restart are now **executed and authorized**.
  Relay/timer enablement remains unexecuted. The `failed-unit-watch` timer
  shipped in PR #60 is **in the repo but not enabled on the host**, so
  nothing is watching for failed units yet.

## 5. Tooling notes for the next session

- `gh` authentication is now working (see Section 1) — do not assume it is
  broken; check `gh auth status` before falling back to anonymous/REST-only
  workarounds.
- Test runs need the repository virtualenv
  (`/home/cwliao/.hermes/hermes-agent/venv/bin/pytest`); the system
  `python3` has no `pytest`.
- `gh api .../actions/runs?head_sha=<SHA>` requires the **full 40-character**
  SHA — a short SHA silently returns `total_count: 0` with no error. If a
  workflow-run lookup by `head_sha` comes back empty right after a push,
  suspect a truncated SHA before suspecting a missing run; a `branch=`-based
  query is a useful cross-check.
- If a CI run shows identical `created_at`/`run_started_at`/`updated_at` and
  zero jobs with a "workflow file may be broken" message, but local YAML
  validation of the workflow and everything it references comes back clean,
  treat it as a one-off platform anomaly (see PR #49's history) rather than
  spending time re-auditing an unchanged file — the fix that worked was an
  empty commit to force a fresh evaluation, done with explicit user
  approval since it's a push.

## 6. Safe continuation procedure

1. Read this file, `docs/ROADMAP-HERMES-DGX.md`, and the active ticket plan.
2. Verify repository root, remote, branch, HEAD, worktree, and authenticated
   GitHub `origin/main` in the exact command context.
3. Preserve the primary checkout; use an isolated worktree for every change.
4. Re-verify CI on the exact head under consideration; a green run is bound to
   one SHA and any new push re-opens the CI gate.
5. Treat the gateway slice flakiness as a separate, still-unopened follow-up.
6. Treat the AGY allowlist design as a separate, still-unimplemented
   follow-up; do not edit `~/.gemini/antigravity-cli/settings.json` without
   first getting a clean, attributable soft-deny trace and explicit user
   authorization for the specific rule change.
7. Use one authenticated reviewer per family; never duplicate Claude, Grok, or
   AGY across platforms. Reconcile any non-quorum finding. Note that PR #49's
   quorum-relaxation amendment has not yet had its own review pass.
8. Before DGX mutation, verify hostname, user, effective unit, release SHA,
   and rollback evidence.
9. Keep reports metadata-only: no message bodies, prompts, joke text, tokens,
   credentials, or sensitive absolute paths.

## Copyable Claude continuation prompt

```text
You are taking over the hermes-agent repository. Use the repo as the source of truth.
Read docs/HANDOVER.md, docs/ROADMAP-HERMES-DGX.md, and
docs/plans/2026-08-18-hermes-multi-agent-claude-worker-001.md before acting.

Verify repository root, remote, branch, HEAD, worktree, and origin/main. The
primary checkout recorded as D:/PROJECT/Hermes is intentionally dirty; do not
reset, clean, overwrite, stage, or commit it. Work only in an isolated
worktree. gh is authenticated on this host (account cwliao, scopes include
repo/workflow) — check gh auth status before assuming it's broken.

Both PR #48 and PR #49 are merged. origin/main is
1b5d75a8838b9eab5c4ec47c1588cbdb76fc9114 (PR #49's merge commit). PR #49
relaxed the four-lane worker contract in hermes_cli/kanban_swarm.py to
require native_hermes plus only 2-of-3 external lanes (claude/grok/agy);
this relaxation was NOT independently re-reviewed by Claude/Grok/AGY, only
the original strict four-lane design was.

Merging closed the code gate only. Ticket acceptance gates 3 through 9 are
all unexecuted: quorum-lane DGX runtime preflight, graph construction,
worker completion, verifier/synthesis, Dashboard evidence, Telegram
user-visible delivery, and cleanup. A separate read-only investigation into
why AGY was BLOCKED found: no deny/ask rules exist in its settings.json
allowlist (only allow), 3 pre-existing sudo rules and 5 python3 -c rules are
flagged as risky, and no clean soft-deny trace has actually been captured
yet — a design doc at docs/plans/HERMES-AGY-HEADLESS-PERMISSIONS-001.md
records the decision to build a minimal allowlist from observed traces, but
it is uncommitted and no settings.json change has been made. The gateway CI
slice-3/8 flakiness (separate from either merged PR) also remains an
unopened, unauthorized follow-up.

DGX deployment, restart, relay/timer enablement, and Telegram testing are
separate gates and are not authorized. Before ending, update the repo
handover only with facts verified in that session, and keep evidence
metadata-only.
```
