# SWARM-LANE-TIMEOUT-RETEST-002-LEFTOVERS follow-up 2: a real, new lead on the notify-sub root cause

Status: evidence-gathering only, no code or config changed. This is a
"keep looking" follow-up to
`docs/plans/2026-08-22-notify-sub-honest-return-fix.md` (which shipped
a root-cause-agnostic defensive fix after three specific theories were
ruled out). This document originally proposed a fourth theory (the
gateway's on-boot systemd-unit self-heal racing an in-progress restart
transaction) as the "identified, high-confidence" cause of a
consistent restart-failure pattern. **Independent cross-review, before
this merged, found a counter-example in the same logs that falsifies
that causal claim** (see the correction in "What's newly confirmed"
below) -- the self-heal is not a necessary cause. What survives: a
real, consistent, still-unexplained restart-failure pattern, and a
documented-but-unconfirmed-here failure class (orphan dispatchers
racing on WAL frames) worth keeping on record. Read this document as
weaker evidence than its own first draft claimed, not as a settled
fourth theory.

## What's newly confirmed

**Correction, caught by independent cross-review before this document
merged**: an earlier draft of this section claimed all 9 restarts
logged today showed the self-heal (`"↻ Updated gateway ... service
definition"` / `daemon-reload`) firing together with the
`status=1/FAILURE` exit, and framed the self-heal as the "identified,
high-confidence" cause of that exit. Independently re-verified against
the full raw log (`journalctl --user -u hermes-gateway.service --since
"2026-08-21 00:00:00" | grep -E "Stopping hermes-gateway|Updated
gateway|Main process exited|Started hermes-gateway"`) after the review
flagged it: **that causal claim does not hold.** Of the 9 "Stopping"
events, one (08:06:29) crosses an actual system reboot and never logs
a `status=1` exit at all (masked by the reboot boundary -- can't
confirm either way). Of the remaining 8 genuine service restarts, 7
show both the self-heal line and `status=1/FAILURE` together, but
**one (12:05:54) shows the identical old-PID-reprints-banner-then-
exits-1 pattern with NO self-heal line at all**:

```
12:05:54 Stopping hermes-gateway.service...
12:05:54 python[3184]: WARNING gateway.run: Shutdown context: signal=SIGTERM ...
12:06:04 python[3184]: ⚕ Hermes Gateway Starting...
12:06:14 systemd: hermes-gateway.service: Main process exited, code=exited, status=1/FAILURE
12:06:14 systemd: Started hermes-gateway.service...
```

No `"↻ Updated gateway..."` line appears anywhere between the
`Stopping` and `Main process exited` lines for this restart. Since the
failure pattern reproduces without the self-heal firing, **the
self-heal's `daemon-reload` cannot be the cause of the
`status=1/FAILURE` exit as a matter of necessity** -- something else,
common to ordinary restarts regardless of whether the unit happens to
need rewriting, is producing that exit code. The self-heal is
correlated with 7/8 observed restarts (plausibly because today was an
unusually deploy-heavy session, each deploy pinning a new release that
made the unit look stale), not established as causal for any of them.

**What remains solid**: every genuinely-observed restart today (8/8,
excluding the ambiguous reboot-crossing one) shows the old PID
reprinting the gateway's own startup banner ("⚕ Hermes Gateway
Starting...") a few seconds after receiving `SIGTERM`, then exiting
with `status=1/FAILURE` before systemd starts the actual new process.
That pattern itself is real and consistent. Its cause is now an open
question again, not the self-heal as originally claimed here.

## Root cause of *that* pattern: NOT established

The self-heal theory above is ruled out as a *necessary* cause (see
correction). No alternative explanation for the consistent
old-PID-restart-banner-then-`status=1/FAILURE` pattern was identified
in this pass. Whatever `hermes gateway run` does on `SIGTERM` that
produces this shape -- independent of whether
`refresh_systemd_unit_if_needed()` also happens to fire -- is still
unexplained and worth its own investigation, separate from the
notify-sub mystery this document originally set out to explain.

## Connection to the notify-sub mystery: weaker than originally claimed here

The orphan-dispatcher/WAL-race theory below was originally framed as
resting on the self-heal `daemon-reload` racing an in-progress restart
transaction. Since that specific trigger is no longer established (see
correction above), this connection is now speculative on *two* levels
rather than one: (a) whether an orphan process survives a restart at
all on this host, by *any* mechanism, remains unconfirmed, and (b)
even if one does, whether it caused *this specific* incident is
unconfirmed either way. Recorded anyway because the general failure
class it points at is real and documented in this exact codebase, not
because the specific causal chain to `v6`'s missing row is established:

`hermes_cli/kanban_db.py::_dispatch_tick_lock`'s own docstring
describes this failure class by name: *"a `hermes gateway run
--replace` / `gateway restart` ... can leave an orphan gateway whose
dispatcher escapes the service cgroup, survives `systemctl restart`,
and becomes a second long-lived writer on the same `kanban.db`. Two
dispatchers that each believe they own the file both pass SQLite
`busy_timeout` and then race on WAL frames -- the documented root
cause of multi-writer corruption."* This is not a hypothetical for this
host: four `kanban.db.corrupt.*.bak` files exist from 2026-08-20,
07:07-07:10, confirming this class of corruption has actually happened
here before. `kanban.db` is confirmed running in `wal` journal mode
right now, which is the mode the corruption-avoidance comment is
about.

**What this does NOT establish** (expanded after the correction
above): whether *any* restart today -- via the self-heal, via whatever
actually causes the `status=1/FAILURE` exit, or via some third
mechanism -- ever left a genuinely orphaned second writer process
alive; whether `_dispatch_tick_lock` (which only guards the periodic
dispatch tick, not arbitrary tool-call-triggered writes like
`_maybe_auto_subscribe`'s) would have prevented a race even if an
orphan existed; or any causal link to the specific `v6` incident at
19:52:45. No orphan process was directly observed at any point (this
entire investigation was conducted after the fact, from logs). This
section is now a documented, real failure class this codebase already
guards against elsewhere, offered as a still-plausible but
meaningfully weaker lead than originally claimed -- not a confirmed
causal chain to the specific `kanban_notify_subs` row that went
missing.

## What was ruled out this round

None of the three theories from the original leftover-issues document
were revisited or need correction -- this is a genuinely new,
independent lead (unit-file self-reload racing an in-progress restart
transaction), not a refinement of the earlier three (write mechanism,
delete cascade, thread-pool ContextVar loss).

## Suggested next steps for whoever picks this up

1. Find what actually causes the consistent old-PID-restart-banner-
   then-`status=1/FAILURE` pattern, now that the self-heal is ruled out
   as a *necessary* cause (12:05:54 shows the pattern without it). Since
   the same shape appears whether or not the self-heal fires, look
   first at whatever `hermes gateway run` unconditionally does in
   response to `SIGTERM` -- restart once with `journalctl -f` open and
   trace the old process's own shutdown-handler code path (starting
   from `WARNING gateway.run: Shutdown context: signal=SIGTERM ...`,
   already confirmed present in every restart) through to its exit.
2. Separately (not gated on #1 anymore, since the self-heal's role is
   unconfirmed either way): decide whether
   `refresh_systemd_unit_if_needed()` running unconditionally on every
   boot -- including ones systemd itself just initiated, while
   systemd's own transaction on that unit may still be in flight -- is
   worth deferring regardless of whether it's the cause of this specific
   symptom. Calling `daemon-reload` from inside a unit's own process
   during systemd's own restart transaction on that unit is a fragile
   pattern in general, independent of whether it explains this incident.
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
