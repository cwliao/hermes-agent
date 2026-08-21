# Session handover — notify-subs fix, T0213/T0214 recovery, lane-failure investigation

Date: 2026-08-20/21. Written for a fresh session (handing off from Claude
Code to Codex) to pick up where this one stopped. Host: DGX Spark,
hostname `55-0940189-03`, SSH-reachable at `140.96.58.171` as user
`cwliao` (`ssh cwliao@140.96.58.171` — this exact host/user is already
confirmed reachable from the user's WSL environment as of 2026-08-19, per
`central-brain`'s `TOPOLOGY.md`). Repo: `hermes-agent`
(`github.com/cwliao/hermes-agent` — **not** `NousResearch/hermes-agent`;
`gh` commands need `-R cwliao/hermes-agent` or they hit the wrong repo).

**Working directory used all session, still there, still clean**:
`/home/cwliao/.hermes/worktrees/pr48-review-claude001` — a linked git
worktree of the same repo whose primary checkout lives at
`/home/cwliao/.hermes/hermes-agent`. Both are on `main`, both fast-forward
clean to `origin/main` as of this handover (`git status --short` empty in
both). A companion repo, `klib` (`github.com/cwliao/klib`), was also
touched this session at `/home/cwliao/project/klib` — currently on branch
`migration/T0154-canonical-root`, clean.

Never work directly in `~/.hermes/releases/*` (deployed release
snapshots) — always in a worktree, then deploy a fresh release archive.

## What this session shipped, in order

1. **`WORKER-SUBPROCESS-SESSION-ENV-001`** (PR
   [#84](https://github.com/cwliao/hermes-agent/pull/84), merged) — the
   actual bug fix this session was called in for. Dispatcher-spawned
   kanban worker subprocesses (`hermes_cli/kanban_db.py::_default_spawn`)
   never forwarded `HERMES_SESSION_PLATFORM`/`_CHAT_ID`/`_KEY` into the
   child env, so any `kanban_create`/`kanban_swarm` call made *from
   inside* a worker's own turn always silently failed to auto-subscribe
   to Telegram notification delivery. Fixed by persisting a new
   `origin_platform`/`origin_chat_id`/`origin_thread_id`/`origin_user_id`/
   `origin_session_key`/`origin_profile` identity on the `tasks` row at
   creation (explicit, or inherited from the first parent that has one —
   this is what propagates it down an entire swarm/worker tree), and
   having `_default_spawn` stamp it into the worker's env. New shared
   resolver: `gateway/session_context.py::resolve_notify_origin()`.
   Cross-reviewed twice (ticket design, then the implementation diff) by
   independent agents before merge. Full trail:
   `docs/plans/2026-08-20-kanban-worker-subprocess-session-env-001.md`.
   **Confirmed fixed live**: a real four-lane swarm test (tenant
   `fix-verify-v1`) had its `native_hermes` worker call `kanban_create`
   for a standalone follow-up task from inside its own turn, and it
   correctly auto-subscribed — this exact scenario would have silently
   failed before the fix.

2. **`NOTIFY-SUBS-DEBUG-LOG-LOCATION-AND-HERMES-CLI-DRIFT-001`**
   (investigation only, no PR of its own — findings absorbed into other
   work). Two findings:
   - A *prior* session's "debug log never fired" mystery was resolved:
     the log fired correctly, just into `~/.hermes/logs/agent.log`
     (the `tools.kanban_tools` logger), not `~/.hermes/logs/gateway.log`
     (that session was checking the wrong file) — confirmed structurally
     via `hermes_logging.py`'s `_ComponentFilter`, not just by
     correlation.
   - The `hermes` CLI wrapper (`~/.local/bin/hermes`) unconditionally
     does `unset PYTHONPATH; unset PYTHONHOME` before exec'ing the venv's
     own `hermes`. Since `hermes-agent` is installed **editable**
     (`pip show hermes-agent` → `Editable project location:
     /home/cwliao/.hermes/hermes-agent`), any `hermes <subcommand>` run
     via the `terminal` tool (as opposed to a typed in-process tool call)
     resolves `hermes_cli`/`tools`/`gateway`/etc. against
     **`/home/cwliao/.hermes/hermes-agent`'s own on-disk state**, not
     whatever release `hermes-gateway.service`'s systemd drop-in
     currently pins `PYTHONPATH` to. This was empirically confirmed (not
     just read from the mapping table) with `env -u PYTHONPATH -u
     PYTHONHOME ... python -c "import tools.kanban_tools"` from a neutral
     cwd. **This remains true and unfixed** — no code changed the
     wrapper's behavior this session. What *did* change: the checkout it
     resolves against (`/home/cwliao/.hermes/hermes-agent`) is now
     up to date (see item 4) instead of 36 commits stale, which
     meaningfully shrinks the practical risk without touching the
     wrapper itself. Whether the wrapper's `unset PYTHONPATH` behavior
     should change at all is an open design question, deliberately not
     decided this session — see "Open items" below.

3. **T0213 and T0214 recovered from zero-git-backup live state.**
   Independent of the notify-subs work, auditing
   `/home/cwliao/.hermes/hermes-agent` for the drift issue above surfaced
   two **already-implemented, already-cross-reviewed, currently-running**
   pieces of work that existed only as uncommitted/unpushed files on this
   one host's disk, with no backup anywhere:
   - **T0213** (bounded auto-restart allowlist + MCP-health gateway
     recovery bridge + app deep health checks) — was a single unpushed
     local commit on this checkout's `main`. Preserved via PR
     [#85](https://github.com/cwliao/hermes-agent/pull/85), merged.
     Cross-review verdict: **ready to merge**, 37/37 tests pass, no
     correctness bugs. **One real gap it flagged, still open**: the two
     new systemd unit templates this PR adds
     (`scripts/systemd/failed-unit-allowlist-repair.{service,timer}.in`,
     `scripts/systemd/app-deep-health-check.{service,timer}.in`) are
     **not wired into any installer script** — `scripts/
     install_calendar_guard.py` and `scripts/install_kanban_summary.py`
     each hardcode their own one template, there's no generic "render
     all `scripts/systemd/*.in`" mechanism. This is how they apparently
     got onto this host in the first place: manual, out-of-band
     rendering. A future redeploy elsewhere (or a rebuild of this host)
     will NOT automatically install these two units. Worth a follow-up
     ticket — not done this session.
   - **T0214** (klib baked-release drift detection, Objective #2) — three
     more untracked files with the same zero-backup problem:
     `hermes-agent`'s `scripts/release_drift_watch.sh` +
     `scripts/systemd/release-drift-watch.{service,timer}.in` (PR
     [#86](https://github.com/cwliao/hermes-agent/pull/86), merged), and
     the companion `klib/scripts/check_release_drift.sh` (klib PR
     [#12](https://github.com/cwliao/klib/pull/12), merged). Both timers
     are enabled and running live on this host right now.
   - **Near-miss during recovery, now a documented rule**: the three
     T0214 files were briefly *deleted* (mistaken for unused cruft
     because `git status` showed them untracked) before being restored
     from a backup, and separately, committing one of them then
     switching `klib` back to its working branch made it vanish from
     disk again (a *different* mechanism — a tracked-only-on-one-branch
     file disappears on checkout, unlike an untracked file which
     persists). Both incidents are written up with the resulting rules
     in `central-brain`'s `AGENTS.md` ("Working and Git Rules" section)
     and `environments/55-0940189-03.linux.md` §8/§8.1 — **read that
     before deleting or moving any untracked file on this host, or
     committing one and then switching branches**.

4. **`/home/cwliao/.hermes/hermes-agent`'s own `main` branch reconciled
   with `origin/main`.** It was 36 commits behind (T0213 was written on
   top of that stale state) with ~70 files of separate stale/uncommitted
   drift on top (unrelated to T0213/T0214 — an entire deleted
   `plugins/mermaid_renderer` plugin, several deleted `docs/plans/`, etc.
   — backed up to
   `~/.hermes/_to-delete/hermes-agent-uncommitted-wip-20260820/` with a
   README, then reverted via `git checkout -- .`). After that cleanup, a
   **local, unpushed** `git merge origin/main` (not a reset/fast-forward)
   brought this checkout's trunk current while preserving T0213, before
   T0213 was even merged upstream. Once #84/#85/#86/#87/#88 all landed on
   `origin/main`, both `/home/cwliao/.hermes/hermes-agent` and the
   `pr48-review-claude001` worktree were fast-forwarded to match exactly
   — **as of this handover, `main` locally == `origin/main` in both
   places, 0 ahead / 0 behind, working tree clean.**

5. **Live swarm re-test surfaced two more, unrelated problems** — filed
   as tickets (PR [#87](https://github.com/cwliao/hermes-agent/pull/87)),
   then partially investigated with concrete reproduction attempts (PR
   [#88](https://github.com/cwliao/hermes-agent/pull/88)), both merged.
   **Neither is fixed. Both are open follow-up work:**
   - `docs/plans/2026-08-20-swarm-claude-grok-lane-timeout-recurrence-001.md`
     — claude/grok lanes both hit the dispatcher's 300s worker timeout
     twice in the same 4-lane run and gave up. Solo, headless
     reproduction of each CLI (`claude -p "..." < /dev/null`, `grok -p
     "..." < /dev/null`) came back clean in under a second — supports
     PR #78's "inference contention" diagnosis (concurrent load, not a
     CLI-specific bug) over a new per-CLI slowness theory, but this
     isn't proven yet; next step is re-running the 4-lane swarm with
     per-worker timing instrumentation.
   - `docs/plans/2026-08-20-swarm-agy-headless-oauth-block-001.md` — the
     agy lane blocked, claiming (in its own `kanban_block` reason and a
     529-char comment) that `agy` needs interactive browser OAuth and
     both `-p` and `-p --sandbox` timed out after 60s trying it headless.
     **Directly reproduced the opposite**: running those exact two
     invocations headless (`< /dev/null`), both from an interactive shell
     and with the worker's own env vars (`HERMES_PROFILE`,
     `HERMES_KANBAN_TASK`) and cwd (its actual workspace directory, still
     on disk), all returned `OK` in under a second using the existing
     stored token
     (`~/.gemini/antigravity-cli/antigravity-oauth-token`) — no browser
     prompt, no hang. This contradicts the worker's own reported reason
     and raises a **fabrication concern**: that lane ran on `ornith:35b`,
     a smaller local model, and this repo already has a named
     `fabrication-guard` mechanism (PR #68) whose coverage of
     `kanban_block` reasons specifically hasn't been checked. Also
     independently corroborated: asked a peer Claude Code session on
     this same host (`dgx-workspace-16`, via `SendMessage`) whether it
     had recently used `agy` successfully — **no reply received yet as
     of this handover; check for one, and treat it as supporting
     evidence either way, not conclusive on its own.**
   - Neither ticket has a decided fix. Both explicitly need whatever
     follow-up investigation their "next steps" sections describe,
     cross-reviewed, before any code/config change.

## Current live state (verify before trusting, per this whole effort's own established discipline)

- `hermes-gateway.service`: `active`, has been running the whole session
  without a restart needed for the final state (last restart during this
  session was for the throwaway debug-tracing deploy, which was reverted
  before this handover — see the ticket for that if curious, not relevant
  going forward). `HERMES_RELEASE_SHA` = `399b72846a85721a932432de48c3459553fbe350`
  (T0213's merge commit) at `PYTHONPATH=/home/cwliao/.hermes/releases/
  v2026.8.20-gate8-notify-subs-and-t0213-t0214-399b72846a`. **This
  release predates PR #87/#88 (docs-only, no redeploy needed for those)
  but does include #84/#85/#86** — verify with `tr '\0' '\n' <
  /proc/$(systemctl --user show -p MainPID --value hermes-gateway.service)/environ
  | grep HERMES_RELEASE_SHA` before assuming this is still accurate; time
  will have passed.
- All PRs opened this session (#84, #85, #86, #87, #88 on hermes-agent;
  #12 on klib) are **merged**. Two pre-existing, unrelated open PRs on
  hermes-agent (#52, #21) and one on klib (#1) were left untouched — not
  this session's work, don't assume they're related.
- `git status --short` is empty in `/home/cwliao/.hermes/hermes-agent`,
  `/home/cwliao/.hermes/worktrees/pr48-review-claude001`, and
  `/home/cwliao/project/klib` as of this handover.

## Open items for the next session, roughly in priority order

1. **Systemd installer gap for T0213's two new unit templates** (see
   item 3 above) — write whatever generic-or-specific installer wiring
   makes sense, cross-review, ship.
2. **`agy` fabrication concern** — get the actual `process`-tool command
   args from the blocked worker's session (not visible at current log
   level) to settle whether a real `agy` call was made and genuinely
   misbehaved, or whether the block reason was invented. Check
   `dgx-workspace-16`'s reply if one has landed by now.
3. **claude/grok timeout re-test with instrumentation** — confirm or
   rule out inference contention as the cause under real concurrent
   dispatch, not just solo.
4. **The `hermes` CLI wrapper's `unset PYTHONPATH` design question** —
   deliberately not decided this session (see item 2 above). Needs the
   user's input on whether `/home/cwliao/.hermes/hermes-agent`'s role as
   an always-current "stable trunk" for CLI-shelled commands and several
   auxiliary systemd services (vs. the gateway's own pinned-release
   model) is the intended long-term design, before touching the wrapper.
5. Nothing else outstanding from this session specifically — but this
   repo has substantial *other* history (36+ commits/day of unrelated
   Gate 8 swarm work across many prior sessions); don't assume this
   handover is a complete picture of the whole project, only of what
   this session touched.

## Process notes for whoever picks this up

- **Cross-review discipline held throughout**: every ticket got a second
  independent agent's read before implementation, and every
  implementation got a second independent agent's read (with its own
  test run, not trusting the first agent's claims) before merge. This
  caught real things — e.g. a cross-reviewer's claim that `tools` wasn't
  in the editable-install `MAPPING` dict turned out to be the reviewer's
  own mis-read, caught by re-verifying independently rather than trusting
  either side blindly.
- **Verify delegated agent work directly, every time** — extends to
  background agents' own claims about test results, file contents, and
  "done" status. This session's own background-agent completion
  notifications were unreliable across a session restart (one review
  agent's "no completion record found" turned out to have a complete,
  useful result sitting in its saved transcript — read the transcript
  file directly rather than assuming a missing notification means lost
  work).
- **This is a live, single, shared production service** with no staging
  environment — `hermes-gateway.service` on this host. Confirm with the
  user before any deploy/restart, git push, or PR merge; this session did
  so consistently and it caught real issues before they shipped (e.g. an
  early over-eager platform-gate design in a *prior* session's work,
  caught by a test before merge).
- **Telegram testing loop**: the user pastes the bot's own narrated
  messages back into chat. Treat that narration as unverified — always
  independently query `hermes_cli.kanban_db` directly against the real
  task ids before accepting a reported outcome. This session's own agy
  finding (item 5 above) is a fresh example of exactly this pattern, just
  at the worker level instead of the bot-reply level.
