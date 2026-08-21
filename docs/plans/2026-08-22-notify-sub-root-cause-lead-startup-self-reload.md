# SWARM-LANE-TIMEOUT-RETEST-002-LEFTOVERS follow-up 2: a real, new lead on the notify-sub root cause

Status: evidence-gathering only, no code or config changed. This is a
"keep looking" follow-up to
`docs/plans/2026-08-22-notify-sub-honest-return-fix.md` (which shipped
a root-cause-agnostic defensive fix after three specific theories were
ruled out). This document records a fourth theory, with real supporting
evidence, that was NOT confirmed as the definitive cause but is
substantive and different enough from the first three to be worth its
own record.

## What's newly confirmed

**Every single `hermes-gateway.service` restart today (9 total,
`journalctl --user -u hermes-gateway.service --since "2026-08-21
00:00:00"`) shows the same abnormal shutdown pattern**: the old process
receives `SIGTERM`, then -- under the *same PID* -- prints the
gateway's own startup banner ("⚕ Hermes Gateway Starting..."), then
exits with `status=1/FAILURE` a few seconds later, and only then does
systemd start the actual new process. Example (the restart nearest the
`v6` swarm incident, 19:34:40-55, ~18 minutes before the swarm was
created at 19:52:45):

```
19:34:40 Stopping hermes-gateway.service...
19:34:40 python[1127306]: WARNING gateway.run: Shutdown context: signal=SIGTERM ...
19:34:48 python[1127306]: ↻ Updated gateway user service definition to match the current Hermes install
19:34:48 python[1127306]: ⚕ Hermes Gateway Starting...
19:34:54 systemd: hermes-gateway.service: Main process exited, code=exited, status=1/FAILURE
19:34:55 systemd: Started hermes-gateway.service...
```

The same PID (1127306) that was told to stop is the one printing a
fresh startup banner eight seconds later, before finally dying with a
failure exit code. This is not a one-off -- every restart logged today
follows this exact shape.

## Root cause of *that* pattern (identified, high confidence)

`hermes_cli/gateway.py::refresh_systemd_unit_if_needed()` is called
unconditionally on every gateway boot (`hermes_cli/gateway.py` line
~4772, inside the `hermes gateway run` startup path, explicitly to
"self-heal" restart settings even when the process was respawned
out-of-band). When it decides the installed unit is stale relative to
`generate_systemd_unit()`'s current expected content, it **rewrites
the systemd unit file and calls `systemctl --user daemon-reload`
itself** -- from inside the very process systemd is, at that moment,
in the middle of stopping as part of an externally-triggered restart
transaction. The `"↻ Updated gateway ... service definition"` log line
is this self-heal firing.

Calling `daemon-reload` from inside a unit's own process while systemd
is actively mid-transaction on that same unit is a generally fragile
pattern -- it can perturb the very job systemd is executing. It is a
highly plausible explanation for the consistent `status=1/FAILURE`
exit seen on every restart today, though this document does not claim
to have proven the exact failure mechanism inside systemd/the process
teardown that produces exit code 1 specifically.

## Connection to the notify-sub mystery (plausible, NOT confirmed)

`hermes_cli/kanban_db.py::_dispatch_tick_lock`'s own docstring
describes exactly this failure class by name: *"a `hermes gateway run
--replace` / `gateway restart` ... can leave an orphan gateway whose
dispatcher escapes the service cgroup, survives `systemctl restart`,
and becomes a second long-lived writer on the same `kanban.db`. Two
dispatchers that each believe they own the file both pass SQLite
`busy_timeout` and then race on WAL frames -- the documented root
cause of multi-writer corruption."* This is not a hypothetical for this
host: four `kanban.db.corrupt.*.bak` files exist from 2026-08-20,
07:07-07:10, confirming this class of corruption has actually happened
here before.

`kanban.db` is confirmed running in `wal` journal mode
(`PRAGMA journal_mode` = `wal`) right now, which is the specific mode
the corruption-avoidance comment is about.

**What this does NOT establish**: whether the specific restart at
19:34:40-55 actually left a genuinely orphaned second writer process
alive for the ~18 minutes until the `v6` swarm was created at 19:52:45,
or whether `_dispatch_tick_lock` (which only guards the periodic
dispatch tick, not arbitrary tool-call-triggered writes like
`_maybe_auto_subscribe`'s) would have prevented a race even if an
orphan existed. No orphan process was directly observed at the time
(this analysis is after the fact, from logs) -- this is a strong,
evidence-backed *lead*, not a confirmed causal chain to the specific
`kanban_notify_subs` row that went missing.

## What was ruled out this round

None of the three theories from the original leftover-issues document
were revisited or need correction -- this is a genuinely new,
independent lead (unit-file self-reload racing an in-progress restart
transaction), not a refinement of the earlier three (write mechanism,
delete cascade, thread-pool ContextVar loss).

## Suggested next steps for whoever picks this up

1. Determine whether `refresh_systemd_unit_if_needed()`'s self-triggered
   `daemon-reload` during an in-progress externally-initiated restart
   is itself the (or a) cause of the `status=1/FAILURE` exit seen on
   every restart today -- e.g. by restarting once with `journalctl -f`
   open and correlating the exact moment of the `daemon-reload` call
   against systemd's own transaction log (`systemd-analyze`/`journalctl
   -u init.scope` equivalent for the user manager), or by temporarily
   disabling the self-heal call for one test restart and comparing.
2. If confirmed, decide whether the self-heal should be deferred to
   *after* a restart-triggered boot has fully settled (e.g. skip it
   when the process detects it was launched by systemd within the last
   few seconds of a `Stopping` event for the same unit), rather than
   running unconditionally on every boot including ones systemd itself
   just initiated.
3. Separately, check for direct evidence of an actual second
   `hermes_cli.main gateway run` process existing simultaneously with
   the primary one at any point today -- a live `ps`/`pgrep` check
   during a future restart (not after the fact) would settle whether
   the orphan-dispatcher scenario is really happening here, versus the
   self-heal reload being merely correlated but not actually producing
   a second live writer.
4. This entire investigation was conducted after the fact, from logs
   and static analysis. A live reproduction -- watching a real restart
   with `ps` running continuously in another terminal -- would settle
   items 1 and 3 far more conclusively than further log archaeology.

## Process note

This finding does not retract or weaken
`docs/plans/2026-08-22-notify-sub-honest-return-fix.md`'s defensive
fix -- that fix (verify the write before reporting success) remains
correct and valuable regardless of which of these theories, if any,
turns out to be the actual root cause. It is exactly the kind of
protection that matters most when the underlying cause is this hard to
pin down.
