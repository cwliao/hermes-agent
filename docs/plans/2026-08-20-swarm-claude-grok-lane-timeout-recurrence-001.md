# SWARM-CLAUDE-GROK-LANE-TIMEOUT-RECURRENCE-001

Status: ticket, not yet investigated further. Needs cross-review before
implementation starts (if any code change turns out to be warranted at all
-- this may turn out to be host load, not a code bug).

## Context

Live four-lane swarm test today (tenant `fix-verify-v1`, root `t_ec46132f`,
run to verify `WORKER-SUBPROCESS-SESSION-ENV-001` -- see that ticket and
`docs/plans/2026-08-20-kanban-worker-subprocess-session-env-001.md`, now
merged as [PR #84](https://github.com/cwliao/hermes-agent/pull/84)). The
notify-subs fix under test worked correctly (confirmed via direct DB query
and gateway.log). Separately, two of the four lanes never produced a
result:

- `t_183f5b52` (`claude_lane:秋天俏皮話`, skill `claude-code`)
- `t_b832ba13` (`grok_lane:秋天俏皮話`, skill `grok`)

## What's confirmed from `task_events`

Both lanes ran the identical failure pattern twice, back to back:

```
claimed -> spawned -> 5 heartbeats -> timed_out (elapsed_seconds=300,
limit_seconds=300) -> claimed (retry) -> spawned -> 5 heartbeats ->
timed_out (elapsed_seconds=300, limit_seconds=300) -> gave_up
(failures=2, effective_limit=2, limit_source=dispatcher,
trigger_outcome=timed_out)
```

Both hit the exact same 300s ceiling on both attempts, then the
dispatcher's 2-failure circuit breaker (`effective_limit=2`,
`limit_source=dispatcher`) stopped retrying and marked both `blocked`.
`agy_lane` (`t_727e2096`) failed differently and is out of scope for this
ticket -- see the companion
`2026-08-20-swarm-agy-headless-oauth-block-001.md`.

## This looks like a recurrence of an already-diagnosed and "fixed" class of problem -- not yet established whether it actually is

`docs/plans/` history (`c7828e42a8`, `89ecadb89d`: "worker timeouts are
inference contention, not a code fault"; `36671c8e70`/PR #78: "bound
dispatcher worker concurrency by default", introducing
`kanban.max_in_progress` with `DEFAULT_MAX_IN_PROGRESS = 3`) already
diagnosed and shipped a fix for exactly this symptom class (workers timing
out under concurrent inference load) roughly a day before this session.
`gateway.log` shows the dispatcher spawned 3 workers essentially
simultaneously for this run (`kanban dispatcher [kanban]: spawned=3
...` at 21:41:25, one more shortly after for the 4th lane) -- consistent
with the same contention pattern PR #78 was written to bound.

**Checked, and this is NOT simply "the cap isn't being enforced":**
`kanban.max_in_progress` read via `cfg_get` returns `None` (unset in
config.yaml), but `hermes_cli/kanban_db.py`'s resolver function (around
line 9096, the one that actually gates the dispatcher's spawn loop) falls
back to `DEFAULT_MAX_IN_PROGRESS = 3` whenever the raw config value is
`None` -- confirmed by reading the resolver, not assumed. So `spawned=3`
concurrently is very likely the cap *working as designed*, not a bypass.

**What's actually open:** if the cap is correctly enforced at exactly 3,
and 3 concurrent inference calls on this host still produces a 300s
timeout for 2 of 4 lanes, that means either (a) 3 is still too many for
this host's current load/model backend, or (b) `claude-code`/`grok`
specifically (as opposed to `native_hermes`/`agy`, which either succeeded
or failed differently) have their own slowness/hang characteristic
unrelated to host-level contention -- e.g. a slow-to-respond upstream API,
a CLI startup cost, or an auth/network issue that happens to also manifest
as a timeout. **Not established which.** This ticket's evidence only shows
the timeout symptom recurring under the same load shape PR #78 already
targeted; it does not establish PR #78's fix is insufficient versus a
different, coincidentally-timeout-shaped cause.

## Suggested next steps

1. Re-run a claude-only or grok-only single-lane task (not inside a
   4-lane swarm, so no concurrency contention at all) with the same
   300s limit and see if it still times out. If it does, the cause is
   specific to `claude-code`/`grok`'s own CLI latency, not contention --
   points investigation at those CLI integrations, not the dispatcher.
2. If a solo lane succeeds well under 300s, re-run the 4-lane swarm and
   capture per-worker wall-clock time from `spawned` to first heartbeat
   and between heartbeats, to see whether the *inference* step itself is
   what's slow under concurrency (host contention, matching PR #78's
   original diagnosis) or something else (network, auth) is newly slow
   only when several lanes launch at once.
3. Consider whether `DEFAULT_MAX_IN_PROGRESS = 3`'s own comment ("chosen
   to be bounded, not optimal... nothing establishes that three beats two
   or four") means this specific host/backend combination now has enough
   evidence to justify tuning it down, rather than assuming 3 is still
   right a day later under different load.

## Process notes

- This ticket is evidence-gathering only; no code changed to produce it.
- Needs cross-review of whatever hypothesis step 1/2 above confirms before
  any config or code change is implemented, per this effort's established
  working rule.
