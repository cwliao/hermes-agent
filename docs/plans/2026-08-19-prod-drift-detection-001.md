---
title: "PROD-DRIFT-DETECTION-001 — a P0 guard left production for 28 hours, unobserved by any existing monitor"
status: DESIGN_ONLY_NOT_IMPLEMENTED
date: 2026-08-19
type: ticket-design
ticket: PROD-DRIFT-DETECTION-001
target_repo: hermes-agent
base: 92b74b9fcb230d4459b7d0567850a930af3d1e7b
---

# PROD-DRIFT-DETECTION-001

Design and decision only. No implementation is authorized by this document.

## The incident

A P0 security control — the model self-impersonation guard (T0104/T0105) —
stopped running in production on 2026-08-18 09:28 and was still absent
28 hours later. It surfaced on 2026-08-19 only because two agents were
arguing about an unrelated deploy and one of them read the release
directories.

Scope of the "nothing detected it" claim, stated precisely because it is
this ticket's load-bearing assertion. Monitoring *does* exist on this host —
`systemctl --user list-timers` shows active timers including
`hermes-mcp-health-guard.service`, `hermes-gateway-recovery.service`, and
`ollama-gpu-healthcheck.service`. None of them observes release content. No
alert was found in the journal for this condition, and no human noticed for
28 hours. What is *not* claimed: that an exhaustive search of every alerting
path was performed. The defensible statement is that the monitoring which
exists does not look at what changed here, and nothing surfaced it.

Three user-facing subsystems left production in the same event, verified by
file presence in each deployed release tree rather than by recollection:

```
                                                  8889a75e9d  dd7a0164  547f82d812  92b74b9fcb
plugins/mermaid_renderer                            present     absent    absent      present
plugins/platforms/telegram/docubot_mcp_gateway.py   present     absent    absent      present
scripts/hermes_drive_watch.py                       present     absent    absent      present
```

The `8889a75e9d` column is load-bearing and was missing from an earlier
draft. Without it the table cannot distinguish "left production" from "was
never deployed" — if these had first appeared in `92b74b9fcb` they would be
new features, not a regression. They are present in the pre-incident
baseline, so the loss is real. A reviewer caught the omission; the guard
table above had always carried that column and these three had not.

### Evidence

Guard presence, measured by grepping the deployed release trees on disk —
not the git branch, which is what made this hard to see:

```
v2026.8.11-t0160-8889a75e9d                     gateway/run.py : 2
v2026.8.18-...-dashboard-dd7a0164                gateway/run.py : 0
v2026.8.19-lane-quorum-547f82d812                gateway/run.py : 0
v2026.8.19-full-main-92b74b9fcb                  gateway/run.py : 2
```

Window, from drop-in file mtimes. The intermediate deploy is included
because the window's continuity depends on it — a 28-hour claim spanning a
release change is only sound if that intermediate release also lacked the
control:

```
2026-08-18 09:28:10   drop-in 43 written, pinning dd7a0164     -> guard leaves production
2026-08-19 10:23:52   drop-in 44 written, pinning 547f82d812   -> still absent (grep: 0)
2026-08-19 13:31:31   drop-in 45 written, pinning 92b74b9fcb   -> guard returns (grep: 2)
```

Both `dd7a0164` and `547f82d812` grep to 0 for the guard, so the absence is
continuous across the intermediate deploy rather than two separate gaps.
That is 28 hours 3 minutes with the control absent.

### What made it invisible

The guard was never deleted and was never broken. It stayed on disk in the
`t0160` release tree the entire time, and it was present on `main`. Every
artifact a person would naturally check looked correct:

- `git grep impersonat origin/main` -> present. True, and irrelevant to what
  is running.
- the source tree in the repo -> present.
- the release directory holding it -> still there, untouched.

Only one thing was false: the release the service was actually executing.
Nothing compared those two.

This is the same failure class as two other incidents on the same day (a
third is introduced below, and it is the one that makes the class
falsifiable). Naming the class precisely is the point of this ticket. An
earlier draft described it as "something stops being true and nothing is
watching". The peer agent involved in the other incidents rejected that
wording as too loose, and was right — it makes the Drive case fit only by
stretching:

- klib deployment worktrees: something that existed **stopped existing**.
- this guard: something deployed **stopped being deployed**.
- duplicate Drive folders: nothing was removed at all. A **new** sibling was
  created and path resolution silently moved to it.

(The first and third are the two other incidents; the second is this one,
listed for contrast rather than counted.)

The shared element is narrower and stronger than "something broke unwatched":

> **An invariant everything depended on was never asserted anywhere, so a
> silent state change had no surface on which to appear.**

Not that nobody was watching — that nobody had ever written down what must
remain true, so there was nothing to watch. In this incident the unasserted
invariant was "the release the gateway executes contains the
self-impersonation guard". Nothing in the repo, the units, or the monitors
stated it, so its violation produced no signal anywhere.

That formulation is testable rather than retrospective, and it was tested.
The peer wrote `scripts/check_drive_name_collisions.sh` **from** the pattern
rather than from any known failure — asserting the structural invariant
(names must be unique) instead of guarding a known path. On its first run it
found a further instance nobody had reported — the third "other", and the
fourth counting this one: duplicate `install` folders
created 08-11/08-12, predating the incident that prompted the search, and
invisible because nothing consumes that path. A collision that breaks
nothing stays invisible indefinitely.

A pattern that predicts an unreported instance before anyone looks is not
retrofitted. That is the strongest available answer to the reasonable
objection that four same-day incidents were assembled into a class after the
fact.

## Why existing gates could not catch this

- **CI** tests the branch, not the deployed artifact. `main` had the guard
  throughout.
- **Unit and integration tests** run against a checkout. They cannot observe
  what a systemd unit points at.
- **Service health checks** were green the whole time. The gateway was
  `active`, `NRestarts=0`. A release missing a security control is exactly
  as healthy as one that has it.
- **The deploy that dropped it** was itself a legitimate action — a newer
  drop-in superseding an older one. Nothing about it was erroneous in
  isolation. The defect is that "this release lacks something the previous
  one had" was never asked.

## Proposed direction (for review, not authorized)

The shape of the answer is a check that compares **the running process**
against **an expected inventory**, because that is the exact comparison no
existing gate makes.

1. **Deploy-time regression gate.** Before a drop-in is activated, diff the
   incoming release tree against the outgoing one and require every
   disappearance to be acknowledged. This already exists informally as a
   habit — it caught `hermes_cli/web_dist` being silently omitted twice on
   2026-08-19 alone, both times because `git archive` ships only tracked
   files. Making it a step rather than a habit is the cheapest single fix
   here, and it would have caught this incident at the moment it was caused.

   Both `web_dist` omissions occurred in this session and were caught by
   that diff before the service was pointed at the tree — first on the
   `547f82d812` bake, again on the `92b74b9fcb` bake. In both cases
   `diff -rq <outgoing> <incoming> | grep "^Only in <outgoing>"` returned
   `hermes_cli: web_dist` and nothing else outside `__pycache__`.

2. **Runtime inventory assertion.** A periodic check that reads
   `/proc/<gateway pid>/environ` for the live `HERMES_RELEASE_SHA` and
   asserts that a named set of controls is present in that release. Start
   with the security-relevant ones; the list should be short and explicit
   rather than a general file census. Read the live process, not the unit
   files — that distinction is the whole incident.

3. **Drift alarm on the pin.** Alert when the deployed SHA falls more than
   N commits behind `origin/main`, or when a commit tagged as
   security-relevant is on main but not in the deployed release. Production
   was 55 commits behind when this was found:

   ```
   $ git rev-list --count 547f82d812..92b74b9fcb
   55
   ```

Not proposed: reverting to the older release, or preventing supersession of
drop-ins. The drop-in that caused this was a valid deploy. The gap is
detection, not permission.

## Open questions

- What is the authoritative list of "controls that must be present"? Naming
  it is most of the work, and getting it wrong in the permissive direction
  reproduces the incident with extra steps.
- Should the runtime assertion fail closed — refuse to start a gateway whose
  release lacks a required control — or alarm only? Fail-closed on a
  security control is defensible; fail-closed on a subsystem like the
  Mermaid renderer probably is not, which suggests two tiers.
- How long is acceptable between merge and deploy for security work? The
  guard reached `main` when PR #50 merged at `2026-08-19T10:18:33+08:00`
  and was deployed at `2026-08-19 13:31:31 +0800` — **3h13m**, not the
  eleven hours an earlier draft of this ticket stated. That figure came from
  subtracting a local timestamp from a UTC one. It is corrected here rather
  than quietly, because a ticket about undetected drift should not carry an
  arithmetic error in its own timeline.
- ~~Was anything else dropped in the same window?~~ **Answered.** A reviewer
  flagged that the four items were found by following a thread rather than
  by enumeration, so the census was run:

  ```
  $ diff -rq <dd7a0164> <92b74b9fcb> | grep "^Only in <dd7a0164>" | grep -v __pycache__ | wc -l
  0
  $ diff -rq <dd7a0164> <92b74b9fcb> | grep "^Only in <92b74b9fcb>" | grep -v __pycache__
  docs/handover-coding-cli-webgate.md
  docs/handover-mermaid-renderer.md
  docs/plans/2026-08-18-hermes-multi-agent-claude-worker-001.md
  docs/plans/2026-08-19-swarm-e2e-defects-001.md
  docs/plans/HERMES-AGY-HEADLESS-PERMISSIONS-001.md
  plugins/mermaid_renderer
  plugins/platforms/telegram/docubot_mcp_gateway.py
  scripts/hermes_drive_watch.py
  scripts/mermaid_chromium_probe.py
  tests/gateway/test_telegram_brain_command.py
  tests/test_docubot_drive_watch_wire_contract.py
  tests/test_log_isolation.py
  tests/test_telegram_docubot_callsite.py
  ```

  Nothing present in `dd7a0164` is absent from the current release, so the
  restore is complete and the four-item set was not an undercount. The 13
  additions are all accounted for: five docs and three tests from this
  session's own work, and the five klib-lineage files (mermaid renderer plus
  its probe, DocuBot gateway, Drive watch, and their wire-contract tests).

## Evidence boundary

All SHAs, release directory names, and timestamps above are non-sensitive
deployment metadata. No credentials, tokens, message bodies, or user data
are recorded.
