# SWARM-CLAUDE-GROK-LANE-TIMEOUT-RECURRENCE-001

Status: **step 2 of the "Revised suggested next steps" completed,
2026-08-21.** A fresh 4-lane swarm re-run with `task_events` timing
captured shows the timeout is not specific to `claude`/`grok` -- `agy`
(the third external-CLI lane) hit the identical 300s ceiling too, on
both attempts, while `native_hermes` (the one in-process, non-external-
CLI lane) finished comfortably. See "Resolution" section at the end.
Needs cross-review before any config change (e.g. tuning
`max_in_progress`, or a higher `--worker-max-runtime` specifically for
external-CLI lanes) is implemented.

Status (superseded): partially investigated (step 1 of the suggested next steps below,
completed). Result supports "contention/load", not "CLI-specific
slowness". Needs cross-review before any config change (e.g. tuning
`max_in_progress` down from 3) is implemented.

## Follow-up finding (same day, later investigation)

Ran step 1 of the suggested next steps: `claude -p "Reply with exactly:
OK" < /dev/null` and `grok -p "Reply with exactly: OK" < /dev/null`,
solo (no concurrent swarm, no other lanes running), headless. **Both
returned `OK` in well under the 300s limit -- no hang, no slowness, no
sign of a CLI-specific problem.** This was run as part of the same
investigation session as the agy ticket's follow-up
(`2026-08-20-swarm-agy-headless-oauth-block-001.md`), where the same
"reproduce solo, headless" test on agy's two claimed-failing invocations
also came back clean -- raising the same fabrication concern noted
there as a possible confound for *this* ticket's timeouts too (i.e. it's
not fully ruled out that the dispatcher's own `task_events` "timed_out"
records are accurate wall-clock measurements rather than something else,
though `timed_out` is a dispatcher-side SIGTERM-on-deadline mechanism, not
worker self-report, so it's much less likely to be fabricated the way a
`kanban_block` reason can be).

Given solo invocations are fast and clean, the timeout is more likely to
be a genuine effect of concurrent load (matching step 1's original
purpose) than a per-CLI slowness issue -- consistent with the original
PR #78 diagnosis, just possibly needing a lower cap than 3 for this
host's current conditions, or the load characteristics have changed since
PR #78 shipped (a day earlier).

## Revised suggested next steps

1. ~~Re-run a claude-only or grok-only single-lane task...~~ **Done, see
   above.** Solo lanes are clean.
2. Re-run the 4-lane swarm again with per-worker wall-clock timing
   captured (spawned -> first heartbeat -> subsequent heartbeat gaps) to
   see whether the slowdown under concurrency is inference-step latency
   (matching PR #78's original contention diagnosis) or something else
   that only appears under concurrent dispatch specifically.
3. If contention is confirmed, cross-review a proposal to lower
   `kanban.max_in_progress` below the current default of 3 (or make it
   adaptive to host load) before changing it -- the value's own comment
   in `hermes_cli/kanban_db.py` already says "nothing establishes that
   three beats two or four."

## Original suggested next steps (kept for reference; step 1 above is the completed version of item 1 below)

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

## Resolution (2026-08-21)

Completed "Revised suggested next steps" #2: re-ran the same 4-lane swarm
shape (tenant `timing-recheck-v1`, root `t_7f35c922`, same 300s
`--worker-max-runtime`, same unchanged `DEFAULT_MAX_IN_PROGRESS = 3`
cap -- config.yaml still has no override) with the exact task/skill
prompt structure this ticket and the companion agy ticket used, and read
`task_events`' `created_at` timestamps directly (no code instrumentation
needed -- every `claimed`/`spawned`/`heartbeat`/`timed_out`/`gave_up`
event is already durably timestamped per task).

**Full timeline** (`kanban.boards.kanban.kanban.db`, `task_events`,
times local):

| lane | attempt 1 spawn | attempt 1 outcome | attempt 2 spawn | attempt 2 outcome |
|---|---|---|---|---|
| `native_hermes` (`t_39d43b37`) | 13:40:48 | **completed** 13:43:26 (158s, 3 heartbeats) | -- | -- |
| `claude` (`t_ba2341d0`) | 13:40:48 | `timed_out` 13:45:50 (300s, 4 heartbeats) | 13:45:51 | `timed_out` 13:50:55 (302s) -> `gave_up` |
| `grok` (`t_caf7a20a`) | 13:40:48 | `timed_out` 13:45:51 (300s, 5 heartbeats) | 13:45:51 | `timed_out` 13:50:56 (302s) -> `gave_up` |
| `agy` (`t_043e05be`) | 13:43:48 (queued behind the cap until `native_hermes` freed a slot) | `timed_out` 13:48:53 (304s, 5 heartbeats) | 13:48:53 | `timed_out` 13:53:59 (304s) -> `gave_up` |

Every external-CLI lane hit `elapsed_seconds` between 300 and 304 on
**every one of its six total attempts across this run** (2 each for
claude/grok/agy) -- not a fluke, a hard, repeatable ceiling. Per-attempt
`gave_up` payload matches the original incident's shape exactly:
`{"failures": 2, "effective_limit": 2, "limit_source": "dispatcher",
"trigger_outcome": "timed_out"}`.

**Heartbeat-gap analysis** (the actual "per-worker wall-clock timing"
this step was asked to capture): across all three concurrent lanes
during both the native_hermes+claude+grok phase and the later
claude+grok+agy phase, individual heartbeat-to-heartbeat gaps cluster in
the same **~60-90s range**, including `native_hermes`'s own gaps (61s,
60s, 35s-to-completion) -- i.e. the *per-step* latency looks similar
across all four lanes under the same 3-concurrent-worker load. What
differs is **how many steps each lane needs**: `native_hermes` reached
`completed` after only 3 heartbeats (158s total); every external-CLI
lane was still mid-task at 5 heartbeats and ~300s, without having
reached `completed`.

## Revised conclusion

This reframes the original "contention vs. CLI-specific slowness"
either/or framing from the ticket's own earlier sections. The evidence
now supports a **combination, not a single cause**:

1. **Per-step latency is dominated by shared host contention**, roughly
   equally across all four lanes (matching PR #78's original diagnosis
   -- this part of the earlier finding holds up).
2. **External-CLI lanes (`claude`, `grok`, `agy`) need structurally more
   steps to complete the same task than `native_hermes`.** The agy
   ticket's own transcript (`2026-08-20-swarm-agy-headless-oauth-block-001.md`)
   is a concrete example of why: a plain "produce one joke" task
   expanded into `kanban_show` -> failed `cd`+`agy` attempt -> corrected
   background spawn -> `process wait` -> `kanban_show` -> a second
   background spawn with a different flag -> another `process wait` ->
   `kanban_block`/`kanban_comment` -- each of those is a separate
   turn/heartbeat-bearing step that an in-process `native_hermes` lane
   simply does not pay for. **Caveat, per independent cross-review:**
   this specific step-by-step breakdown is documented in detail for only
   one lane (`agy`, via its own transcript) -- `claude`'s and `grok`'s
   equivalent transcripts were not examined turn-by-turn in this pass.
   The heartbeat-count comparison (native_hermes: 3; every external
   lane: 4-5 without reaching `completed`) supports the same conclusion
   for all three external lanes, but "structurally needs more steps" as
   a *mechanism* is confirmed in detail for `agy` and inferred, not yet
   directly confirmed the same way, for `claude`/`grok`.
3. Combining (1) and (2): a fixed 300s ceiling that is comfortably
   enough for `native_hermes` under the current contention level is
   **not** enough for any external-CLI lane under the *same* contention
   level, because they need more steps at a similar per-step cost. This
   is not "claude/grok are uniquely slow" (the third external lane,
   `agy`, shows the identical pattern) and it is not "contention alone
   explains it either" (`native_hermes` under the same contention
   finishes fine) -- it's the product of both.

**This also means PR #78's fix (bounding concurrency to `max_in_progress
= 3`) is not wrong, but is not sufficient on its own for external-CLI
lanes at the current 300s worker-runtime ceiling.** Two independent,
non-exclusive levers exist to actually fix this, neither implemented
here (per this effort's cross-review-before-change rule):

- **Raise `--worker-max-runtime` specifically for external-CLI lanes**
  (claude/grok/agy), leaving `native_hermes` at a shorter ceiling since
  it does not need the extra headroom. This is the more targeted fix,
  directly addressing the "more steps needed" finding rather than the
  contention level.
- **Lower `kanban.max_in_progress` below 3** (the pre-existing idea from
  this ticket's original suggested next steps), which would reduce
  per-step latency across all lanes but does not address the underlying
  fact that external-CLI lanes need more steps regardless of contention
  level -- likely a partial mitigation at best on its own.

Both should be cross-reviewed as explicit proposals (with the option to
combine them) before either is implemented -- this document remains
evidence-gathering only; no code or config was changed to produce it.

## Process note

The original ticket's "Follow-up finding" section (solo, uncontended
`claude -p`/`grok -p` calls returning `OK` quickly) is **not
contradicted** by this resolution -- it correctly showed neither CLI has
an unconditional startup/auth problem. What that earlier solo test could
not show, because it deliberately avoided contention, is the specific
combination (contention + external-CLI's own higher step count) that
this fresh concurrent re-run now demonstrates directly.
