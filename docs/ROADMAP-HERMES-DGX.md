# Hermes Architecture Roadmap

Snapshot: 2026-08-18 (Asia/Taipei), Claude continuation session. This file is the current repository-local
state summary. Git history contains older deployment snapshots; do not use
those older snapshots to infer the current PR or runtime state.

## Source-of-truth rules

- `origin/main` and the repository-local handover are authoritative.
- The primary checkout is an audit checkout and may be dirty; preserve it.
- Use an isolated worktree for new work.
- Keep design, implementation, tests, independent review, reconciliation, CI,
  commit, push, merge, DGX deployment, runtime health, inbound polling,
  outbound delivery, and Telegram user-visible delivery as separate gates.
- Service health, polling progress, empty updates, `getMe`, or webhook status
  do not prove Telegram user-visible delivery.

## Current topology

| Reference | Verified state |
|---|---|
| Repository | `https://github.com/cwliao/hermes-agent` |
| `origin/main` | PR #49's merge commit `1b5d75a8838b9eab5c4ec47c1588cbdb76fc9114` plus this docs-only handover update on top; pre-merge base was `10314ad04d...` (PR #48's merge plus two docs-only commits). Re-read `origin/main` directly |
| Primary checkout | Recorded as `D:/PROJECT/Hermes`, `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8`; intentionally dirty. Carried forward: that path is absent on the Linux host used for the 2026-08-18 Claude continuation session, where `~/.hermes/hermes-agent` sits on `main` at `b7b362b2f1bcf494ba7f0acf2236452caacb05b6` and is clean |
| PR #48 | **merged and closed** 2026-08-18T07:30:25Z; head `f97f3402f38bb735430c3296fbb19fc387dc7348`, merge commit `a5bebea8f8...`; strict four-lane worker contract, implementation `af3034993...` |
| PR #49 | **merged and closed** 2026-08-18T09:20:04Z; head `18e597943b...`, merge commit `1b5d75a883...`; relaxes the four-lane contract to native_hermes + 2-of-3 external (claude/grok/agy), implementation `66c49541c4...` |
| DGX identity | WSL Ubuntu (or direct DGX Spark host access via SSH, as used in this session) only; expected hostname `55-0940189-03`, user `cwliao`, service `hermes-gateway.service`. Confirmed via `hostname`/`whoami` in this session, not assumed |
| `gh` auth | Working as of this session (account `cwliao`, scopes `gist, read:org, repo, workflow`). Was broken (stale token) for most of the session; re-authenticated via device-code flow. Check `gh auth status` before assuming it's broken again |

## Engineering roadmap

1. Preserve the merged/deployed Hermes baseline and its separate Telegram
   delivery evidence.
2. `HERMES-MULTI-AGENT-CLAUDE-WORKER-001`'s code is now merged across two
   PRs (#48 strict four-lane, #49 relaxed to native_hermes + 2-of-3
   external). Note PR #49's relaxation has **not** had its own independent
   Claude/Grok/AGY review pass — only the original strict contract did.
3. Investigate the independent gateway slice flakiness through a separately
   authorized follow-up; do not silently expand either merged PR.
4. Both PRs are merged, which closes the code gate only. The quorum-lane
   runtime preflight, graph execution, verifier/synthesis, Dashboard
   evidence, and the Telegram user-visible test are the remaining work for
   this ticket. DGX deployment, restart, runtime checks, and the
   metadata-only Telegram delivery test each still require explicit user
   authorization and remain unexecuted.
5. A separate, read-only investigation into the AGY worker lane's `BLOCKED`
   state produced a design doc
   (`docs/plans/HERMES-AGY-HEADLESS-PERMISSIONS-001.md`, uncommitted) but no
   implementation. Building an actual minimal `permissions.allow` allowlist
   still needs a clean, attributable soft-deny trace, which has not yet been
   captured — see the CI history section's sibling detail in
   `docs/HANDOVER.md` Section 3.

## Ticket status

| Ticket | Status | Evidence / next action |
|---|---|---|
| `HERMES-MULTI-AGENT-CLAUDE-WORKER-001` | `BOTH_PRS_MERGED_END_TO_END_TEST_NOT_RUN` | PR #48 (strict four-lane, `af3034993...`) and PR #49 (relaxed to native_hermes + 2-of-3 external, `66c49541c4...`) both merged. PR #49's CI hit a one-off zero-job "workflow file may be broken" anomaly on its first run (`32113864761`); an empty retrigger commit produced a clean `success` (`32120002343`) on unchanged workflow files. Focused swarm tests 10/10 (6 original + 4 lane-quorum). Claude/Grok PASS only on the original strict-four-lane design; the quorum relaxation itself was not re-reviewed. AGY's `BLOCKED` state is now understood in detail (no `deny`/`ask` rules exist in its allowlist, only `allow`; 3 pre-existing `sudo` rules and 5 `python3 -c` rules flagged as risky) but not fixed — no settings change has been made. Ticket acceptance gates 3-9 (runtime preflight, graph execution, worker completion, verifier/synthesis, Dashboard evidence, Telegram user-visible delivery, cleanup) are all unexecuted. |
| Gateway slice flakiness follow-up | `NOT_OPEN` | The three fresh-final failures did not recur on unchanged code in CI, and 28/28 passed locally. Unrelated to PR #49's separate zero-job CI anomaly (different failure signature — see Ticket status row above). GitHub Issues are disabled. Create a repo-local plan only after explicit authorization; keep it separate from either merged PR. |
| AGY headless permissions follow-up | `DESIGN_ONLY_NOT_IMPLEMENTED` | Design/decision doc exists at `docs/plans/HERMES-AGY-HEADLESS-PERMISSIONS-001.md` but is uncommitted (untracked file in the working worktree). Adopted path: build a minimal `permissions.allow` allowlist from *observed* command traces (not yet captured — no clean soft-deny repro exists this session), with PTY/tmux as the human/debug fallback. `--dangerously-skip-permissions` explicitly prohibited from any standing worker path. The user is handling `settings.json` review/changes directly via SSH; no agent-driven settings change has occurred. |
| Previous orchestration baseline | Historical | Do not infer current PR or runtime state from older roadmap entries or releases. |

## Current ticket boundaries

The ticket's merged code changes only existing Kanban/worker surfaces and
tests. It adds explicit lane identities, preflight-skill binding, bounded
goal/worker runtime settings, parser-safe worker construction, required
metadata-only handoffs, a fail-closed verifier, and a synthesizer result
boundary. PR #49 additionally relaxes the lane-count requirement from an
exact four to native_hermes + 2-of-3 external. None of this adds a
dispatcher, provider integration, Telegram adapter, custom Dashboard, or
alternate task store.

The intended disposable test is a Telegram-requested joke brainstorm:
Kanban goal -> quorum-lane parallel workers -> verifier requiring the
declared lane set -> Hermes synthesizer -> same-user Telegram response. The
final response text is not metadata evidence; only direct user confirmation
closes the Telegram gate.

## CI history

### PR #48

Run `32102234859` (head `af303...`, `run_attempt=2`) failed in Python tests
slice 3/8 at `tests/gateway/test_stream_consumer_fresh_final.py`, and its
aggregate `All required checks pass` job failed. PR #48 contains no gateway
files or gateway tests.

Run `32103854648` on head `f97f3402f3...` concluded `success`: all eight
Python slices, including slice 3/8, passed, and `All required checks pass`
passed. The only commit between the two runs is docs-only, so identical
Python code failed and then passed that slice — flaky, not a deterministic
baseline regression. Locally, that gateway file passed 28/28 at the PR head.

### PR #49

Run `32113864761` failed with **zero jobs scheduled**: `created_at`,
`run_started_at`, and `updated_at` were the identical instant, and both
`gh run view` and `gh run rerun` reported "workflow file may be broken."
Local validation found `.github/workflows/ci.yml` and every referenced
reusable workflow/composite action byte-identical to the version that
passed CI on PR #48's head, and all valid YAML — the file was not actually
broken; `ci.yml` has no `workflow_dispatch` trigger, so `gh run rerun`
being refused left no way to force re-evaluation except a new commit. An
empty commit (`chore: retrigger CI`, zero file changes, pushed with
explicit user approval) produced run `32120002343`, which completed
`success` with every job passing. **This is a distinct failure signature
from the gateway flakiness below — zero jobs / identical timestamps /
"workflow file may be broken" despite a verified-valid file — and the fix
that worked was forcing a fresh evaluation via an empty commit, not
touching any file.**

### General

A green CI run is bound to an exact head SHA; re-verify on whatever head is
under consideration rather than reusing an older run.
`gh api .../actions/runs?head_sha=<SHA>` requires the full 40-character SHA
— a short SHA silently returns zero results with no error.

## Gateway slice flakiness

`tests/gateway/test_stream_consumer_fresh_final.py` slice-3/8 failures did
not recur on unchanged code in CI (PR #48), and passed 28/28 locally.
Because it's a required check, it can randomly block future PRs the same
way it blocked PR #48's first CI attempt. GitHub Issues are disabled. No
issue number exists. A gateway flake investigation must be planned and
authorized separately, then independently reviewed and tested.

## AGY headless permissions

A read-only investigation (see `docs/HANDOVER.md` Section 3 for full detail)
found: AGY's permission state (`~/.gemini/antigravity-cli/settings.json`) is
shared identically between an interactive session and the
`hermes-gateway.service` process (same `$HOME`/user, confirmed via
`/proc/<pid>/environ`); its `permissions.allow` list has 76 exact-command
entries and **no `deny`/`ask` arrays at all**; 3 pre-existing `sudo` rules
and 5 `python3 -c` rules are flagged as risky (the latter possibly
overbroad if AGY's matching is prefix-based, which is unconfirmed); and two
controlled headless traces both avoided reproducing the original
`BLOCKED`/soft-deny symptom (one needed no tool call, the other's `pwd` call
was already exact-matched by rule #42) — so a clean, attributable soft-deny
trace is still needed before any allowlist can be safely designed. A design
doc recording the decision framework exists at
`docs/plans/HERMES-AGY-HEADLESS-PERMISSIONS-001.md` but is uncommitted; no
`settings.json` change has been made by the agent. The user is now handling
this directly via SSH.

## DGX runtime boundary

The last separately verified runtime predates commit `af303...` and had
`HERMES_RELEASE_SHA=20aadf71cb4ead7db7d9590310465e90c937558b`, an active
running user service, zero restarts, and rollback evidence. It was not
re-verified in the 2026-08-18 Claude continuation session. This is historical
runtime evidence only; it does not prove `af303...`, `f97f340...`, or
`66c49541c4...` is deployed. Relay remains disabled by default. Before any
remote mutation, reverify WSL route, hostname, user, effective unit, release
identity, and rollback evidence.

## Immediate next action

Read `docs/HANDOVER.md` and the active ticket plan, verify repository
identity, and confirm `origin/main` is `1b5d75a883...`. Then ask the user
for explicit decisions on three separate questions: whether to authorize
the quorum-lane runtime preflight and the disposable end-to-end test that
this ticket actually exists to prove; whether to open a repo-local plan for
the gateway slice flakiness; and whether/how to proceed on the AGY
allowlist design (capturing a clean soft-deny trace, then a minimal
allowlist) — noting the user has been handling AGY directly via SSH. Do not
deploy, restart, enable timers/relay, or run Telegram testing without
explicit authorization.
