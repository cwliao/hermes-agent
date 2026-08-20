# Session handover — Gate 8 / swarm delivery line

Date: 2026-08-20. Written for a fresh session to pick up where this one
stopped. Host: DGX Spark, hostname `55-0940189-03`. Worktree used all
session: `/home/cwliao/.hermes/worktrees/pr48-review-claude001`
(`hermes-agent` repo, remote `cwliao/hermes-agent`, not the `NousResearch`
upstream — `gh` commands need `-R cwliao/hermes-agent` or they silently hit
the wrong repo).

## What this session did, in order

1. **`WORKER-TIMEOUT-CONTENTION-001`** (from before this handover's scope,
   already merged going in) — default `kanban.max_in_progress=3`. Confirmed
   working: a later four-lane swarm ran with concurrency correctly capped.
2. **PR #76, #77, #78** — kanban GC, the contention diagnosis ticket, and
   the concurrency fix. All merged, all deployed.
3. **Gate 8 re-run after #78** — first ever four-lane swarm to complete
   without a single worker timeout. Surfaced two new problems, written up
   and fixed as
   [`GATE8-SWARM-COMPLETED-VERIFIER-RECOVERY-AND-DELIVERY-GAP-001`](2026-08-20-gate8-swarm-completed-verifier-recovery-and-delivery-gap-001.md):
   a verifier that self-heals its own objection but never re-evaluates
   (needed manual `kanban unblock`), and a synthesizer whose result never
   reached Telegram (no notification subscription existed for the run).
   **PR #79**, merged and deployed: workers now always post
   `[swarm:auto-handoff]` to their swarm root on completion; `hermes kanban
   swarm` (the CLI path) now auto-subscribes its synthesizer.
4. **`GATE8-SWARM-CREATION-TOOL-001`** — the deeper problem: the agent had
   no typed way to build a swarm, so it hand-composed
   `hermes kanban swarm ...` shell commands from memory every time and got
   it wrong in new ways each run (wrong lane names, wrong/missing skill, an
   `assignee` naming a Hermes profile that doesn't exist — silently never
   dispatched, no error anywhere — partial graphs left behind mid-turn).
   **PR #80**, merged and deployed: registered `kanban_swarm` as a real
   tool wrapping `create_swarm()` directly, with `preflight_skill_id`
   filled in from a `LANE_SKILL_IDS` table and `profile` defaulted rather
   than guessed. Required updating three separate static tool-name lists
   for the tool to actually reach any surface's schema (`toolsets.py` x2,
   `agent/transports/hermes_tools_mcp_server.py`) — easy to miss one and
   have it silently not work on one runtime.
5. **`KANBAN-TOOLSET-PLATFORM-GATE-001`** (`TELEGRAM-SWARM-UNREACHABLE-001`
   Defect A) — even with `kanban_swarm` registered, Telegram couldn't see
   it: `_profile_has_kanban_toolset` only read the top-level `toolsets`
   config key, not `platform_toolsets`. **PR #81**, merged and deployed.
   **Read this ticket's "Implementation" section before touching this
   area again** — the first design (delegate to
   `hermes_cli.tools_config._get_platform_tools`) was wrong and would have
   made kanban tools visible on every platform unconditionally; caught by
   `test_kanban_tools_hidden_without_env_var` before shipping. The fix that
   actually shipped reads `platform_toolsets` directly instead.

All four PRs (#78-#81) are merged into `main` and deployed to this host.
Current release: `/home/cwliao/.hermes/releases/v2026.8.20-kanban-toolset-gate-92051b5450`
(`HERMES_RELEASE_SHA=92051b54508160454072e55a369e4818a92f9a16`), wired via
`~/.config/systemd/user/hermes-gateway.service.d/53-hermes-kanban-toolset-gate-92051b5450.conf`.
The temporary top-level-`toolsets: [kanban]` workaround that was live for a
few hours today has been reverted — `~/.hermes/config.yaml`'s top-level
`toolsets` is back to `[hermes-cli]` only; Telegram sees kanban tools
through the real `platform_toolsets.telegram` route now, confirmed against
the live config.

## What actually got tested live, and worked

Two four-lane swarms completed fully end-to-end via the new `kanban_swarm`
tool today (tenant `gate8-real` and a variant), both with all four workers
reaching `done`, verifier passing, synthesizer picking a winner. This is
the first time in the whole multi-day effort that happened without a
manual unblock. Gate 8's *technical* pipeline (build → dispatch → verify →
synthesize) now works reliably through the tool.

## What's still open — the actual next task

**`kanban_notify_subs` has an intermittent, not-yet-root-caused defect.**
Symptom: a swarm's synthesizer sometimes gets auto-subscribed to
notification delivery (a row appears, Telegram gets the result) and
sometimes doesn't (zero rows, silent, no warning logged) — for what looks
like the same code path, back to back, in the same session.

What's confirmed:
- `tools/kanban_tools.py::_maybe_auto_subscribe` (used by `kanban_create`
  and `kanban_swarm`) reads `HERMES_SESSION_PLATFORM`/
  `HERMES_SESSION_CHAT_ID` via `get_session_env` and silently returns
  `False` (no exception, no warning) if either is empty.
- `hermes_cli/kanban_db.py::_default_spawn` (dispatcher-spawned worker
  subprocesses) never puts `HERMES_SESSION_PLATFORM`/`_CHAT_ID`/`_KEY` into
  the child process's env — confirmed by reading the spawn code
  (`env = dict(os.environ)` then explicit sets, none of these three). Any
  `kanban_create`/`kanban_swarm` call made *from inside* a dispatcher
  worker will always silently fail to subscribe. This is real and
  reproducible by inspection, not yet fixed.
- **But this does not explain everything observed.** A live, same-turn,
  non-subprocess `kanban_swarm` tool call (verified via gateway.log: single
  turn, no `delegate_task`, no background dispatch) also failed to
  subscribe once today. Manual reproduction of the identical code path —
  `set_session_vars()` → call `_handle_swarm` directly, and separately
  wrapped through `ThreadPoolExecutor` + `propagate_context_to_thread`
  (the real dispatch machinery `agent/tool_executor.py` uses) — **both
  succeeded** in isolation. The bug does not reproduce outside the live
  gateway process.
- A temporary debug log was added directly to the *deployed* release's
  `_maybe_auto_subscribe` (not committed — this was throwaway, already
  reverted and the file diffed clean against `origin/main` before writing
  this handover) to capture `platform`/`chat_id`/thread name at call time.
  **It never fired at all**, for either a failing or a later-succeeding
  live call, despite the running process's `PYTHONPATH` confirmed pointing
  at the patched file. This means live `kanban_swarm` calls during today's
  testing likely went through a *different* code path than
  `tools/kanban_tools.py::_handle_swarm` — most plausibly
  `hermes_cli/kanban.py::_cmd_swarm` (the CLI, shelled out via `terminal`)
  → its own separate `_maybe_auto_subscribe_swarm` — but no
  `hermes kanban swarm` terminal-command log line was found in
  `~/.hermes/logs/gateway.log` for the relevant time window either. This
  is the actual open mystery: **which code path is really running, live,
  and why doesn't the log line meant to observe it ever fire.**

### Suggested next steps for a fresh session

1. Don't re-derive the above by re-reading code — it's already been read
   closely (see the trace in the earlier investigation-agent transcript,
   summarized above). The missing piece is *live* information: which
   function is actually executing when a real Telegram turn builds a
   swarm.
2. Best next probe: add debug logging to **both**
   `tools/kanban_tools.py::_maybe_auto_subscribe` and
   `hermes_cli/kanban.py::_maybe_auto_subscribe_swarm` at once (both are
   cheap, both should fire something), redeploy, trigger one live test,
   and diff which one (if either) actually logs. If neither logs, the
   swarm isn't going through either function at all, which would be a
   more fundamental surprise worth chasing from scratch.
3. Alternative/complementary probe: log the full stack
   (`traceback.format_stack()`) once inside `_maybe_auto_subscribe` on
   first call per process, to settle "which caller reached here" beyond
   doubt, instead of inferring from log-line absence.
4. Once root-caused, the dispatcher-subprocess gap
   (`_default_spawn` not forwarding session env) is a separate, already-
   confirmed, independently fixable issue worth its own ticket regardless
   of what the live-turn mystery turns out to be — forwarding
   `HERMES_SESSION_PLATFORM`/`_CHAT_ID`/`_KEY` into worker subprocess env
   (mirroring what `_inject_session_context_env` in
   `tools/environments/local.py` already does for the `terminal` tool's
   own subprocess spawns) would close it. Not implemented this session;
   nothing was reverted or half-done here, it's just unstarted.

## Process notes for whoever picks this up

- **Cross-review discipline paid off concretely this session**: three
  separate implementation attempts (the swarm-handoff fix, the
  `kanban_swarm` tool, and this ticket's `_get_platform_tools` false
  start) each had a real, shipped-averting bug caught by review or by the
  test suite before merge. Keep doing the "open ticket → cross-review →
  implement → cross-review implementation" cycle; it is not theater here.
- **Verify delegated agent work directly, every time** — this session
  caught two agent sub-investigations stating things that turned out
  wrong on direct empirical check (`_get_platform_tools`'s actual return
  value for an empty config; an early review claiming a design was
  correctly typed when it silently wasn't). Re-running the specific
  claimed command/query yourself is cheap; trusting the prose is not safe.
- **Deploy pattern used all session**: `git archive origin/main | tar -x
  -C <new release dir>`, copy `hermes_cli/web_dist` from the previous
  release (nothing this session touched the web UI), write a new
  `~/.config/systemd/user/hermes-gateway.service.d/NN-<slug>-<shortsha>.conf`
  drop-in pointing `WorkingDirectory`/`PYTHONPATH` at the new release dir
  and setting `HERMES_RELEASE_SHA`, back up the previous drop-in to
  `~/.hermes/deploy-backups/`, `daemon-reload` + `restart`, then verify
  via `/proc/<pid>/environ` and a direct import check against the new
  release's own files (not just "no errors in journalctl" — that alone
  has been insufficient more than once across the broader effort this
  session continues).
- **Telegram testing loop**: the user pastes the bot's own narrated
  messages back into chat. Treat that narration as *unverified* — this
  session caught it fabricating a "final result" from stale content at
  least once, and separately reporting states ("all four done") that
  didn't match the DB. Always independently query `hermes_cli.kanban_db`
  directly against the real task ids before accepting a reported outcome.
- One long-running gateway process shared across all testing today
  (`hermes-gateway.service`, DGX host). Every deploy/restart in this
  session affected the user's real, live Telegram bot — there is no
  separate staging environment. Treat restarts accordingly (confirm
  before doing them, which this session generally did).
