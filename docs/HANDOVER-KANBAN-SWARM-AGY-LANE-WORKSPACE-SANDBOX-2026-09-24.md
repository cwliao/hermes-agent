# Handover: Hermes Kanban Swarm — `worker-agy` Lane Workspace Sandbox

- Date: 2026-09-24 (Asia/Taipei)
- Repository: `/home/cwliao/.hermes/hermes-agent`
- Service: `hermes-gateway.service`
- Working language: Traditional Chinese for user-facing output

## Executive status

A 3-lane cross-model review swarm (`orchestrator-claude`, `worker-agy`,
`worker-grok`) was dispatched to independently review a roadmap file living at
an absolute path outside any kanban workspace
(`/home/cwliao/project/klib/docs/ai-second-brain-next-roadmap-2026-09-24.md`).
`orchestrator-claude` and `worker-grok` read the file and completed normally.
`worker-agy` blocked with:

```
Unable to read the roadmap file
/home/cwliao/project/klib/docs/ai-second-brain-next-roadmap-2026-09-24.md
due to recurring tool errors. Cannot proceed with the review.
```

Observed graph:

- root: `t_8eae5233`
- workers: `t_7d3163d4` (orchestrator-claude, done), `t_0832c6d2`
  (worker-agy, blocked → fixed → done), `t_36ec444c` (worker-grok, done)
- verifier: `t_0dbeae2f`
- synthesizer: `t_b414a697`

## Root cause (working hypothesis, not fully confirmed)

The `worker-agy` lane's tool sandbox appears restricted to the task's own
kanban workspace directory (`~/.hermes/kanban/workspaces/<task_id>/`) and
cannot read arbitrary absolute paths elsewhere on disk, unlike the
`native_hermes`/default lane (which read the same klib project files with no
issue in a separate, unrelated swarm run minutes earlier) and, in this
instance, unlike `orchestrator-claude`/`worker-grok` which also succeeded.

Not yet confirmed:

- Whether this is a hard sandbox boundary specific to the AGY backend/CLI
  integration, or an incidental tool-call failure that happened to recur on
  this one lane during this one run.
- Whether `orchestrator-claude`/`worker-grok` have the same restriction under
  different conditions (e.g. a different filesystem permission model, a
  working-directory default that happened to already contain the target
  path's parent, etc.) — not tested with a file living outside `~/project/`
  or `~`.

## Workaround applied

```bash
cp /home/cwliao/project/klib/docs/ai-second-brain-next-roadmap-2026-09-24.md \
   /home/cwliao/.hermes/kanban/workspaces/t_0832c6d2/ai-second-brain-next-roadmap-2026-09-24.md

hermes kanban comment t_0832c6d2 "The roadmap file has been copied into your own workspace ..."
hermes kanban unblock t_0832c6d2
```

The re-run succeeded once the file existed inside the worker's own workspace.

## Secondary finding: no quorum escape hatch for non-lane-bound swarms

`hermes kanban swarm --worker-quorum` requires `--worker-lane` set on every
worker (a "lane-bound swarm"). Passing plain profile names
(`worker-agy`/`worker-grok`/`orchestrator-claude`) without `--worker-lane`
routes correctly to those profiles, but then both `--worker-lane` and
`--worker-quorum` are rejected:

```
kanban: lane-bound swarms require the native_hermes lane
kanban: worker_quorum is only meaningful for lane-bound swarms
```

Practical effect: a non-lane-bound multi-profile swarm has **no quorum
mechanism** — if one worker permanently blocks and nobody notices, the
verifier (which waits on every parent) deadlocks forever. This matches the
documented `SWARM-PARTIAL-QUORUM-001` rationale for lane-bound swarms, but
that safety net does not extend to swarms built by profile name instead of
lane.

## Suggested follow-up (not done in this session)

1. Confirm whether the AGY lane's tool sandbox is genuinely scoped to the
   kanban workspace directory by design, or a bug; if by design, document it
   next to the swarm CLI's `--worker` help text so operators know to
   pre-stage input files rather than discovering it via a blocked task.
2. Consider whether `--worker-quorum` (or an equivalent timeout-based escape)
   should be extractable for non-lane-bound swarms too, so a single stuck
   profile-routed worker doesn't require manual intervention to unstick the
   whole swarm.
3. If (1) confirms a real sandbox boundary, consider auto-copying any file
   paths referenced in a swarm's goal/worker text into each worker's
   workspace at creation time, rather than relying on the goal being
   self-contained.

Also saved to Claude Code auto-memory
(`project_hermes_kanban_agy_lane_workspace_sandbox.md`), AgentMemory, and
instamem for cross-session/cross-agent recall.
