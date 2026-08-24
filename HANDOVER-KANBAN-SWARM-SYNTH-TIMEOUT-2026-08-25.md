# Handover: Hermes Kanban Swarm Synthesizer Timeout

- Date: 2026-08-25 (Asia/Taipei)
- Repository: `/home/cwliao/.hermes/hermes-agent`
- Service: `hermes-gateway.service`
- Working language: Traditional Chinese for user-facing output
- Secrets/raw Telegram content: intentionally excluded

## Executive status

The current live failure is not Telegram transport alone. The latest observed
swarm reached four workers and verifier successfully, but the synthesizer timed
out twice and the swarm root was incorrectly left as `done`.

Latest observed graph:

- root: `t_8b495cab`
- workers: `t_4d5e335f`, `t_6828588b`, `t_c66addcf`, `t_d71f3232`
- verifier: `t_95258f78` (done)
- synthesizer: `t_39c7d964` (blocked)
- synthesizer attempts: runs `363` and `364`, both `timed_out` at about 301 seconds
- synthesizer result: empty
- synthesizer events: two timeout events and one `gave_up`
- exact synthesizer notifier: not observed
- root status: `done` despite synthesizer blocked — this is the key false-completion bug

Do not claim Telegram success, final output, phone visibility, or full E2E
success from this graph.

## Current deployed release

The active release before the next fix was:

```
/home/cwliao/.hermes/releases/v2026.8.25-kanban-goal-anchor-d2dcf94b03
release marker: d2dcf94b039e7041bad2bfa1d7f739eff3cb7d4cf8501de0e0fe7755dea7af9a
```

The gateway was observed `active`; Telegram polling and dispatcher heartbeat
were healthy during the earlier checks. Always re-check effective systemd
drop-ins, release SHA, service PID, and journal before changing deployment.

## Already implemented in the dirty tree/release history

The working tree contains cumulative user-approved changes including:

1. Fail-closed four-lane receipt/current-turn graph proof.
2. Restricted verifier/synthesizer toolsets and bounded downstream runtime.
3. Exact synthesizer result delivery; completed synthesizer results are not
   rewritten by a wake/model turn.
4. Pending response propagation so stale model prose cannot replace the
   fail-closed pending response.
5. Traditional Chinese output validation.
6. Unicode mixed-script and replacement-character rejection.
7. Current-turn goal binding to prevent stale model goals.
8. Synthesizer goal-anchor validation to reject valid-looking output from an
   older request.

Relevant modified source/test areas include:

- `agent/conversation_loop.py`
- `agent/kanban_execution_guard.py`
- `agent/turn_context.py`
- `gateway/kanban_watchers.py`
- `gateway/session_context.py`
- `hermes_cli/kanban_db.py`
- `hermes_cli/kanban_swarm.py`
- `tools/kanban_tools.py`
- corresponding Kanban, notifier, worker-spawn, guard, and tool tests

Preserve all user changes. Do not reset, clean, checkout, or discard files.

## Authoritative ticket

The durable plan is:

```
/home/cwliao/.hermes/hermes-agent/docs/plans/2026-08-24-kanban-swarm-result-delivery-001.md
```

The implementation ticket is:

**KANBAN-SWARM-002 — Define and enforce synthesizer attempt lifecycle,
ownership fencing, terminal propagation, and recovery invariants**

Three independent reviewers reached the same consensus: this is not merely a
retry-counter problem. The implementation must separate logical task state
from execution-attempt state, fence ownership by run ID, guarantee process
termination before retry, propagate terminal failure to the root, and keep
Telegram notifications truthful.

Approved defaults:

- `max_attempts=2` total (initial attempt plus one retry)
- per-attempt synthesizer timeout: `300s`
- retry backoff/cooldown: `30s`
- termination grace: `15s`
- overall synthesizer deadline: `660s`
- retry exhaustion: task/root `blocked`,
  `block_kind=synthesizer_retry_exhausted`, one `gave_up` event
- kill not confirmed: `block_kind=termination_pending`, no automatic retry
- retryable: timeout, transient worker exit, transient spawn failure, one
  output-contract rejection
- not automatically retryable: permanent dependency failure and ownership/CAS
  rejection

Required invariants:

- Every attempt has one immutable `run_id`.
- `tasks.current_run_id`, claim, PID, and open `task_run` agree.
- Timeout closes exactly one run and records exactly one failure/event.
- A retry cannot start in the same dispatcher tick.
- Late heartbeat/completion/block/timeout from an old run is rejected.
- After budget exhaustion no later tick, replay, or restart may spawn again.
- Root cannot be `done` unless the same-generation synthesizer is `done`
  with a non-empty contract-valid result.
- No notifier may deliver an old result, event summary, status-only text, or
  final success while retrying/blocked.
- Logs contain bounded metadata only, never secrets or result bodies.

## Recommended implementation sequence

1. Read `AGENTS.md`, this handover, and the authoritative plan.
2. Inspect the current timeout/retry implementation in
   `hermes_cli/kanban_db.py`, `hermes_cli/kanban_swarm.py`, dispatcher
   code, and notifier code.
3. Add/adjust the state transition and ownership CAS with the existing schema
   where possible; avoid an unrelated broad refactor.
4. Ensure timeout termination is confirmed before requeue/spawn. If not,
   enter `termination_pending`.
5. Fix root/graph propagation so verifier done + synthesizer blocked results in
   root blocked/failed, never root done.
6. Add regression tests for:
   - first timeout then one successful retry;
   - two timeouts then blocked/gave_up;
   - failed SIGTERM/SIGKILL confirmation;
   - late old-run completion;
   - duplicate timeout/dispatcher tick/event replay;
   - restart/orphan/dead-PID recovery;
   - mixed timeout/crash;
   - notifier ordering and truthful messages;
   - exact output contract and no stale result delivery.
7. Run focused tests, then the relevant full test subsets and
   `git diff --check`.
8. Build a detached immutable release, restart the gateway only after tests pass,
   verify release SHA/PID/health/journal.
9. Run a new phone-originated Telegram E2E with a unique goal/correlation.
10. Report separate gates: graph, synthesizer contract, notifier, Telegram
    delivery audit, and actual phone-visible result. Never collapse them.

## Safe diagnostic commands

```bash
cd /home/cwliao/.hermes/hermes-agent
sed -n '1,260p' AGENTS.md
sed -n '1,260p' docs/plans/2026-08-24-kanban-swarm-result-delivery-001.md
git status --short --branch
systemctl --user status hermes-gateway.service --no-pager
```

Use read-only DB queries against
`/home/cwliao/.hermes/kanban.db`. Do not print Telegram message bodies,
task.result bodies, credentials, tokens, or raw journal lines containing user
content.

## Full prompt for the next Claude session

You are taking over a live Hermes Kanban four-lane swarm reliability fix on
DGX Spark. Work in `/home/cwliao/.hermes/hermes-agent`.

Before acting:

1. Read `AGENTS.md`.
2. Read
   `docs/HANDOVER-KANBAN-SWARM-SYNTH-TIMEOUT-2026-08-25.md`.
3. Read
   `docs/plans/2026-08-24-kanban-swarm-result-delivery-001.md`.
4. Inspect `git status --short --branch`; preserve all existing user
   changes. Do not reset, clean, checkout, or discard files.
5. Do not expose secrets, credentials, tokens, raw Telegram text, task result
   bodies, or private journal content.

Objective:

Implement KANBAN-SWARM-002. Fix the false-completion path where verifier
succeeds, synthesizer times out twice and becomes blocked, but the root remains
done. Also make timeout/retry ownership and recovery safe.

Use these defaults unless code constraints require a documented change:
`max_attempts=2` total, `300s` per attempt, `30s` retry backoff,
`15s` termination grace, `660s` overall deadline,
`termination_pending` when kill is unconfirmed, and terminal root/task
`blocked` with `synthesizer_retry_exhausted` after budget exhaustion.

Implementation requirements:

- Separate logical task state from attempt/run state.
- Fence every transition with immutable `run_id` and claim ownership.
- Make timeout accounting idempotent.
- Confirm old process termination before retry; never create two active
  synthesizers.
- Reject late old-run heartbeat, completion, block, timeout, and cleanup.
- Require same-root/same-generation verifier success.
- Propagate synthesizer retrying/blocked to the root; root cannot be done
  without a valid non-empty synthesizer result.
- Keep notifier messages truthful and prevent old/stale result delivery.
- Record bounded observability metadata only; never log secrets or result text.

Testing requirements:

- Add regression coverage for timeout-then-success, repeated timeout,
  failed termination, late completion, duplicate dispatcher/replay,
  restart/orphan recovery, mixed timeout/crash, notifier semantics, root
  terminal propagation, and output-contract rejection.
- Run focused tests plus relevant existing Kanban/notifier/guard/tool suites.
- Run `git diff --check`.
- Do not claim completion until tests and deployment evidence exist.

Deployment/E2E:

After implementation and tests, create a detached immutable release, restart
`hermes-gateway.service` using the existing deployment conventions, and
verify service state, effective release SHA, PID, restart count, and journal.
Then run one new phone-originated Telegram test with a unique correlation and
goal. Record graph completion, exact notifier delivery, delivery audit, and
phone-visible display as separate gates. If phone-visible evidence is absent,
mark it PENDING.

Final response must state:

- files changed and tests run;
- release path/SHA and gateway health;
- exact root/worker/verifier/synthesizer IDs for the new E2E;
- whether synthesizer result was non-empty and contract-valid;
- notifier/delivery-audit/phone-visible gates separately;
- any remaining blocker;
- no secrets or raw Telegram content.

Do not commit or push unless the user explicitly authorizes it in the active
session.
