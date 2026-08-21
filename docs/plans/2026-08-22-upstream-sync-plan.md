# UPSTREAM-SYNC-2026-08: plan for catching up to `upstream/main`

Status: planning only, revised after two independent cross-reviews.
No merge has been attempted. `main` is unchanged by this document.
Both reviewers verified this document's numbers against the live repo
independently and found them accurate (one cosmetic date-count error,
now fixed below). Both reviewers returned **"needs revision before
proceeding"** -- their findings are folded into this revision. See
"Cross-review outcome" near the end for the consolidated verdict and
what changed as a result.

## Current divergence (as of this fetch, 2026-08-22)

- `upstream` remote: `https://github.com/NousResearch/hermes-agent.git`
  (fetch-only; push is disabled via `git config remote.upstream.push
  DISABLE`).
- Merge-base of local `main` and `upstream/main`:
  `569b912d7d0931c7256e9f5fb326609e9deda377` (2026-07-15).
- Local `main` last actually merged `upstream/main` on **2026-07-15**
  (`8719c62c3a`, "Merge remote-tracking branch 'upstream/main'"). That
  is the last sync -- **38 days stale** as of today (2026-08-22;
  original draft said 37, off-by-one caught by reviewer A).
- `upstream/main` is now **8,888 commits** ahead of that merge-base.
- Local `main` carries **363 commits** not in `upstream/main` -- this
  repo's own customizations (kanban-swarm, notify-sub fixes, gateway
  self-heal, web_gate adapter, DGX/Windows wrapper handling, and this
  entire session's Telegram-swarm-delivery investigation chain).
- Raw diffstat between merge-base and `upstream/main`: **7,733 files
  changed, +1,159,355 / -434,492 lines**. Includes a large desktop-app
  surface (browser control broker, tab-strip UI, artifact endpoints)
  this fork may or may not track at all -- not yet confirmed whether
  this fork carries the desktop/website tree as a real dependency or
  as vendored-but-unused code.

This is not a normal "pull a few weeks of commits" sync. It is closer
in size to re-vendoring a large fraction of the upstream project.

## Precedent: how this repo has handled upstream syncs before

Not a new problem -- this repo has done this several times, following
a consistent pattern visible in branch history:

1. Create a `backup/pre-upstream-<date>` branch from `main` *before*
   touching anything (e.g. `backup/pre-upstream-2026-07-01`,
   `backup/pre-upstream-2026-07-02`) -- a cheap, zero-risk rollback
   point.
2. Do the actual merge work on a dedicated `integrate/upstream-<date>`
   branch (e.g. `integrate/upstream-2026-07-01`), never directly on
   `main`.
3. Only merge `integrate/upstream-<date>` into `main` once conflicts
   are resolved and the merge is validated.
4. Numerous smaller feature branches in this repo's history show the
   same discipline at branch scope ("Merge upstream main into
   feat/hermes-relay-shared-metrics", repeated ~10 times), suggesting
   upstream syncs were sometimes done incrementally per-feature-branch
   rather than once on `main` -- worth considering here too, given the
   size of this particular gap.

This plan follows the same shape, scaled to the size of the gap.

## What makes this sync specifically risky here

- **No staging environment.** `55-0940189-03` runs the only instance
  of `hermes-gateway.service`, serving real Telegram traffic. A merge
  that breaks gateway boot, kanban dispatch, or the Telegram plugin
  path is a production outage with no fallback except reverting the
  deploy.
- **This exact session's unmerged-context risk.** The last ~2 weeks of
  work in this repo (worker_quorum, the swarm completion-loop fix,
  the notify-sub honest-return defensive fix, the still-open
  restart-failure-pattern investigation) all sit in the 363
  local-only commits. A large three-way merge is exactly the kind of
  operation that can silently reintroduce a bug in one of these areas
  (e.g. a conflict resolved by picking "theirs" in
  `hermes_cli/kanban_swarm.py` would silently drop the sticky-block
  fix from PR #97) without any obvious signal at merge time.
- **Scale exceeds manual review capacity.** 8,888 commits cannot be
  read commit-by-commit. Validation has to rely on: automated test
  suite, targeted diffing of files this fork has modified, and
  behavioral smoke tests of the specific subsystems this session
  cares about (kanban dispatch, swarm creation, notify-sub, gateway
  boot/restart) -- not a full manual audit of upstream's changes.
- **Unknown surface overlap.** Not yet established how much of the
  upstream diff (7,733 files) is in trees this fork actually runs
  (`hermes_cli/`, `gateway/`, `tools/`) versus trees it may not track
  meaningfully (`website/`, desktop app, browser-control broker). This
  needs to be scoped before estimating real conflict risk -- the raw
  file count overstates the risk if most of it is in an unused tree.

## Alternatives considered (added per reviewer B -- blocking gap in the original draft)

The original draft jumped straight to "do the full merge" without
weighing it against cheaper options. On a single-instance,
no-staging production host, the full merge is the *highest*-risk of
the plausible options, so it should be the fallback, not the default:

1. **Cherry-pick only specific upstream fixes this fork actually
   needs** (e.g. a named security fix or bug fix known to matter
   here). Small, individually testable, no exposure to the other
   8,800+ unrelated commits. Best option if the actual motivation for
   syncing is a handful of known upstream improvements rather than
   "staying current" in the abstract -- worth asking the user directly
   whether specific upstream changes motivated this request.
2. **Resume incremental syncing going forward** (this repo's own
   history shows it used to merge `upstream/main` far more often --
   note the ~10 `Merge upstream main into feat/hermes-relay-shared-metrics`
   commits) and do a smaller, more frequent catch-up now rather than
   one 8,888-commit jump, accepting that this specific gap still needs
   closing somehow.
3. **Defer entirely until a staging environment exists.** The single
   biggest risk multiplier here is that conflict-resolution mistakes
   are only caught by targeted regression tests and cross-review, not
   by dry-running against a non-production copy of the real Telegram
   traffic path. Standing up even a minimal staging instance (a second
   `hermes-gateway.service` unit pointed at a scratch kanban DB and a
   test Telegram bot token) before attempting a merge this large would
   meaningfully de-risk step 4 below.
4. **Full merge now (this document's original proposal).** Only
   preferred over 1-3 if the goal is genuinely "catch this fork up to
   current upstream," not just "get specific fixes," and if the
   48-hour+ effort (see step 4 revision below) is acceptable now.

This plan does not resolve which of these to pick -- that is a
decision for the user, not something to infer. The rest of this
document assumes option 4 (full merge) is chosen, since that is what
the original request asked to be planned, but option 4 should be
explicitly confirmed rather than assumed as the only path.

## Proposed plan

1. **Scope the overlap. DONE (this document).** `git diff --stat
   <merge-base> upstream/main -- hermes_cli/ gateway/ tools/ tests/`
   shows **3,462 of the 7,733 changed files (45%)** are inside the
   trees this fork actually runs and maintains
   (`+569,213/-335,603` lines in just those four directories). So the
   overlap is real, not a rounding error dominated by the desktop/web
   tree -- roughly half of upstream's changed files are in code this
   fork depends on. This does not reduce the estimated risk; if
   anything it confirms conflict-resolution work in step 4 will be
   substantial, not a formality.
2. **Create `backup/pre-upstream-2026-08-22`** from `main` explicitly
   (`git branch backup/pre-upstream-2026-08-22 main`, not from ambient
   `HEAD`) before any merge work starts, per established precedent.
   **Note (caught by reviewer A):** at plan-writing time the working
   tree's checked-out branch is `docs/gateway-restart-pattern-solved`,
   not `main` -- branching from a bare `main` ref rather than relying
   on whatever happens to be checked out avoids accidentally forking
   from the wrong branch.
3. **Create `integrate/upstream-2026-08-22`** from `main` explicitly
   (same reasoning), never directly on `main`.
3.5. **Pre-flag high-risk conflict files (added per reviewer B).**
   Before starting conflict resolution, both diff sets were compared
   directly: `comm -12` between the fork's changed-file list
   (merge-base..`main`) and upstream's changed-file list
   (merge-base..`upstream/main`) gives **exactly 104 files touched by
   both sides** -- confirmed independently by both reviewers, so this
   is not an estimate. Of those, 40 are actual source files (the rest
   are tests/docs/website). The ones directly relevant to this
   session's own recent fixes, to review file-by-file *first*, before
   the broader merge:
   - `hermes_cli/kanban_swarm.py` -- worker_quorum sticky-block guard
     (#97), completion-call-example (#98). **Confirmed upstream also
     modifies this file independently, +123/-11 lines** (verified by
     reviewer A via direct diff) -- a real conflict, not hypothetical.
   - `hermes_cli/kanban_db.py`, `hermes_cli/kanban.py`,
     `tools/kanban_tools.py` -- notify-sub honest-return fix (#100),
     `_dispatch_tick_lock`, `add_notify_sub`/`list_notify_subs`.
   - `tools/tirith_security.py` -- the evidence-count truncation cap
     (`_MAX_SUPPRESSIBLE_EVIDENCE_COUNT`, PR #95).
   - `gateway/session_context.py`, `gateway/kanban_watchers.py`,
     `gateway/run.py` -- ContextVar session-identity plumbing and the
     gateway startup/shutdown path this session's still-open
     restart-failure-pattern investigation is currently tracing.
   - Full 104-file list saved for reference:
     `hermes_cli/kanban_db.py`, `hermes_cli/kanban.py`,
     `hermes_cli/kanban_swarm.py`, `tools/kanban_tools.py`,
     `tools/tirith_security.py`, `gateway/session_context.py`,
     `gateway/kanban_watchers.py`, `gateway/run.py`,
     `gateway/config.py`, `gateway/platforms/base.py`,
     `hermes_cli/config.py`, `hermes_cli/doctor.py`,
     `hermes_cli/main.py`, `hermes_cli/model_switch.py`,
     `hermes_cli/oneshot.py`, `hermes_cli/plugins.py`,
     `hermes_cli/web_server.py`, `agent/agent_init.py`,
     `agent/chat_completion_helpers.py`, `agent/prompt_builder.py`,
     `agent/relay_tools.py`, `agent/skill_utils.py`,
     `agent/tool_executor.py`, `agent/tool_guardrails.py`,
     `agent/transports/hermes_tools_mcp_server.py`,
     `agent/turn_finalizer.py`, `run_agent.py`, `cron/scheduler.py`,
     `cron/blueprint_catalog.py`, `plugins/platforms/telegram/adapter.py`,
     `scripts/release.py`, `tools/approval.py`, `toolsets.py`,
     `tools/file_operations.py`, `tools/mcp_tool.py`,
     `tools/patch_parser.py`, `tools/terminal_tool.py`, `pyproject.toml`,
     `uv.lock`, `AGENTS.md`, `cli-config.yaml.example`, plus the
     `skills/productivity/google-workspace/*` pair -- and ~64 more
     test/docs/website files (full list generated via `comm -12` on
     the two diff's `--name-only` output, not reproduced in full here).
4. **Attempt the merge on that branch, in checkpointed passes, not one
   sitting (revised per reviewer B).** ~3,462 overlapping files and 40
   overlapping source files is realistically multi-day work, not a
   single continuous session -- attempting it in one sitting invites
   fatigue-driven conflict resolution exactly in the files that matter
   most (the 40 above). Resolve conflicts with priority given to
   preserving this fork's own fixes (worker_quorum sticky-block guard,
   notify-sub read-back verification, Tirith evidence-count cap,
   completion-call-example rendering) when a conflict touches one of
   those specific mechanisms -- confirm intent by re-reading each
   fork-side commit's own diff, not by reflexively picking "ours". **But
   also read upstream's side of every such conflict before discarding
   it (added per reviewer A)** -- a blanket "keep ours" can silently
   drop an unrelated upstream fix layered in the same hunk (e.g. the
   confirmed +123/-11 upstream change to `kanban_swarm.py` needs to be
   read on its own merits, not assumed to be safely discardable just
   because the fork's own patch lives in the same file).
5. **Run the full test suite** on the integration branch (per this
   session's own established practice: don't trust "tests pass" from
   a tool's own report -- rerun and read the actual output).
6. **Targeted regression check** of the subsystems this session's work
   depends on: create a real kanban swarm, verify worker_quorum,
   verify notify-sub read-back, verify gateway boot/restart behavior
   -- against the integration branch, not just unit tests.
7. **Cross-review the merge** before it touches `main`, per this
   effort's standing practice for anything non-trivial -- a fresh
   Agent reviewing the resolved conflicts specifically for silently
   dropped fork-side logic.
8. **Only after 4-7 pass**, merge `integrate/upstream-2026-08-22` into
   `main`, and only then consider a `release_snapshot.py` deploy --
   with an explicit rollback plan (the `backup/pre-upstream-2026-08-22`
   branch and the current running release under
   `~/.hermes/releases/`) ready before restarting the live service.
   **Rollback artifact freshness, checked directly (reviewer A raised
   this as a concern; independently re-verified and found NOT
   stale):** reviewer A's own check used `ls -la | tail -10`, which
   under plain alphabetical sort places `v2026.8.22-...` *before*
   `v2026.8.3-...` (`'2'  < '3'` as the first differing character), so
   the newest snapshots were cut off the tail and the check
   under-counted. Sorting by mtime instead shows the actual latest
   release is `v2026.8.22-notify-sub-honest-return-aa9fca2d53`,
   created 2026-08-22 03:01 -- today, matching PR #100's merge commit
   -- and `/proc/<gateway-pid>/cwd` resolves to exactly that directory,
   confirming the running process really is that snapshot. The rollback
   net is current, not stale. (This is the kind of reviewer claim this
   session's standing practice requires re-verifying independently
   before acting on it, rather than accepting or rejecting it on
   trust -- see the PR #96 precedent.) Still worth doing as a cheap
   step regardless: cut a fresh named snapshot immediately before
   starting step 8's merge-to-main, so the rollback point is pinned to
   the exact pre-merge commit rather than relying on today's most
   recent unrelated snapshot still being current when step 8 actually
   runs, potentially days after step 1-7 started.

## Cross-review outcome

Two independent reviewers examined this document (before this
revision). Both verified every numeric claim against the live repo
themselves rather than trusting the draft, and both returned the same
top-line verdict -- **needs revision before proceeding** -- while
finding different specific issues, which converged on one point
neither knew the other had found: **`hermes_cli/kanban_swarm.py` is a
confirmed real conflict** (both independently ran the diff and got
+123/-11 upstream lines against this exact file). That convergence is
itself useful signal -- it's not two reviewers repeating the same
guess, it's two independent checks landing on the same file.

Findings applied in this revision:
- Alternatives-comparison section added (reviewer B, blocking).
- 104-file / 40-source-file overlap pre-flagged by name (reviewer B,
  important).
- Step 4 reframed as multi-day/checkpointed rather than a single flat
  step (reviewer B, important).
- Conflict-resolution rule now requires reading upstream's side before
  discarding it, not just re-reading the fork's own side (reviewer A,
  important).
- 37 -> 38 day correction (reviewer A, minor).
- `main` branch reference made explicit in steps 2-3 rather than
  relying on ambient `HEAD` (reviewer A, important).

Finding investigated and found NOT to hold, after independent
re-verification (this session's standing practice: never apply a
reviewer's claim without checking it myself):
- Reviewer A flagged the `~/.hermes/releases/` rollback snapshot as
  stale (last dated Aug 11). Re-checked with `ls` sorted by mtime
  instead of the alphabetical default reviewer A's `tail` used: the
  actual latest snapshot is `v2026.8.22-notify-sub-honest-return-
  aa9fca2d53`, from earlier today, and is confirmed (via
  `/proc/<pid>/cwd`) to be the release the live gateway process is
  actually running. Not stale. Kept as a cheap belt-and-suspenders
  step in §8 anyway (cut a fresh snapshot immediately pre-merge) since
  it costs nothing and pins the rollback point precisely.

Not yet resolved by this revision, still requires the user's decision
before step 2 can start: **which of the four alternatives above is
actually wanted.** This document was written assuming "full merge"
per the original request, but a fresh cross-review round would be
warranted again if a different alternative is chosen instead, since
the plan's remaining steps are shaped around the full-merge path.

## Explicitly not done yet

No merge has been attempted. No branch beyond the existing `upstream`
remote fetch has been created. `main` is untouched. This document is
the plan to review/adjust before step 2 (creating the backup branch)
begins.
