---
title: "KANBAN-SWARM-RESULT-DELIVERY-001 — synthesizer output must reach Telegram"
status: IMPLEMENTED_WITH_POST_DEPLOY_E2E_PENDING
date: 2026-08-24
type: incident-and-runbook
ticket: KANBAN-SWARM-RESULT-DELIVERY-001
target_repo: hermes-agent
source_commit: 92283ab9c18f5bb4ffb138bd2792db2cb8562dfb
release: v2026.8.24-kanban-result-delivery-92283ab9c1
---

# KANBAN-SWARM-RESULT-DELIVERY-001

## Objective

Make a four-lane Kanban swarm fail closed when the synthesizer returns only
progress metadata, and ensure the exact synthesizer deliverable is the text
handed to Telegram. A graph that is done is not sufficient evidence of
user-visible success.

## Observed production failure

The deploy-before-fix four-lane run was:

- root: t_220bdce2
- workers: t_731ab875 (native_hermes), t_079c550f (claude),
  t_91eab9aa (grok), t_42953f17 (agy)
- verifier: t_7c55da14
- synthesizer: t_23b5edd2

All cards reached done, and the Telegram delivery ledger recorded a
successful delivery. That did not mean the final output was correct:
the synthesizer task.result was a status-only sentence and the attached
JSONL artifact was not a valid final joke deliverable. The notifier also
preferred the completed event's short summary over the synthesizer's
task.result. The transport path therefore delivered a message while the
user-facing deliverable was absent.

This distinction is intentional and must remain in future incident reports:

graph completion != synthesizer correctness != Telegram delivery != phone display.

## Root causes

### 1. Synthesizer completion contract was too weak

The synthesizer validator required only a non-empty result. A sentence saying
that work was processed and an artifact existed could pass even when the
actual final deliverable was missing.

### 2. Event summary precedence hid the result

gateway/kanban_watchers.py selected task_events.payload.summary first.
That summary is an operational handoff and is appropriate for workers, but
for a synthesizer it is not the user-facing result.

### 3. Attachment-only output was not safe for this path

The synthesizer could claim that a file had been prepared without placing the
complete human-readable deliverable in task.result. Telegram notification
does not inline arbitrary artifact contents, so the final result disappeared.

## Implemented correction

Commit 92283ab9c18f5bb4ffb138bd2792db2cb8562dfb implements:

- hermes_cli/kanban_swarm.py
  - synthesizer contract explicitly requires the exact final user-facing
    deliverable in result;
  - artifact use requires complete human-readable contents to be inlined in
    result;
  - the observed status-only synthesizer pattern is rejected with a bounded,
    auditable retry reason.
- gateway/kanban_watchers.py
  - for a synthesizer task, prefer the complete task.result over the event
    summary;
  - carry the same result into the wake handoff;
  - retain the existing summary-first behavior for non-synthesizer tasks.
- regression tests cover both the validator rejection and Telegram notifier
  selection.

The validator is intentionally narrow. It is not a global Telegram sanitizer
and must not rewrite ordinary user text.

## Validation and deployment evidence

Focused tests executed before deployment:

- 14 passed:
  - tests/hermes_cli/test_kanban_swarm_context_and_output_contract.py
  - tests/gateway/test_kanban_notifier.py
- git diff --check: passed.
- release snapshot marker matched the full source SHA.
- modified files compiled successfully in the release tree.

Immutable deployment:

- source commit: 92283ab9c18f5bb4ffb138bd2792db2cb8562dfb
- release:
  /home/cwliao/.hermes/releases/v2026.8.24-kanban-result-delivery-92283ab9c1
- venv:
  /home/cwliao/.hermes/venvs/gateway-92283ab9c1
- systemd drop-in:
  /home/cwliao/.config/systemd/user/hermes-gateway.service.d/96-kanban-result-delivery-92283ab9c1.conf
- post-restart gateway: active/running, ExecMainStatus=0,
  NRestarts=0
- process cwd and interpreter matched the new release/venv.
- prior release v2026.8.24-kanban-dispatch-9c378ae40e and its venv remain
  available for rollback.

A deploy-time restart is not a user-visible E2E. The fresh post-deploy
Telegram four-way test remains pending until a new phone-originated inbound
is observed.

## Required future acceptance checklist

For every four-lane swarm, record all of the following before saying it
worked:

1. Root ID, four worker IDs, verifier ID, and synthesizer ID.
2. Each worker has a real done result, not only a status comment.
3. Verifier has a contract-valid gate=pass result and verified lane count.
4. Synthesizer task.result is the exact final deliverable, not a progress
   report. Check result length and content class without copying raw content
   into logs.
5. If an artifact exists, validate that it is parseable and consistent with
   task.result; never infer validity from file existence alone.
6. Telegram delivery ledger has a new delivered row for this run.
7. The delivered message contains the synthesizer result by comparing hashes
   or bounded metadata, not by printing secrets or private content.
8. For phone acceptance, observe the actual Telegram message or obtain
   equivalent user-visible evidence. Never infer phone display from gateway
   health, a completed graph, or an API delivery receipt alone.

## Safe diagnosis sequence

When output appears missing:

1. Check gateway health and effective release identity.
2. Query the Kanban graph and task results by ID.
3. Compare synthesizer task.result with completed-event summary.
4. Inspect artifact size/content type/parseability without dumping private
   content.
5. Inspect the delivery ledger status and message length.
6. Only then inspect Telegram routing metadata and phone-visible evidence.
7. Do not restart or patch based solely on a successful transport receipt.

## Known follow-up

The current rejection predicate is deliberately targeted to the observed
status-only wording. A later hardening ticket may replace it with a
contract-driven deliverable-shape check, but must preserve ordinary text,
avoid global sanitization, and remain bounded. Any such change needs focused
tests, a new immutable release, and a fresh Telegram E2E.

## Cross-review and honesty rule

Code review, local tests, service health, production graph completion,
Telegram API delivery, and phone-visible display are separate gates. A missing
external reviewer or missing phone observation must be recorded as
UNAVAILABLE or PENDING, never upgraded to PASS.


## 2026-08-24 fail-closed downstream execution patch

The later live report reused the old root ID `t_220bdce2` and claimed a new
verifier ID that was absent from the live database. The actual graph showed
that the root is only a planning/dispatch anchor. In the subsequent observed
run, the four workers and the verifier did finish, but the synthesizer had the
full default toolset, no runtime bound, and created an unrelated child task;
the Telegram launch receipt was emitted before the downstream workflow was
complete. This was a real workflow failure, not phone-display success.

The fail-closed correction is deployed in immutable release
`v2026.8.24-kanban-fail-closed-431ae0dcd1`:

- verifier/synthesizer workers receive only lifecycle Kanban tools;
- downstream roles cannot create/link/review/attach new work;
- verifier and synthesizer receive a 300-second `max_runtime_seconds` bound;
- a successful launch receipt is reported as pending until the live verifier
  passes and the synthesizer is `done` with a non-empty result;
- the final result must still be verified through the live graph, delivery
  ledger, journal, and actual phone-visible evidence.

Validation: focused regression suite `85 passed`; `git diff --check` and
syntax checks passed; gateway restarted into the new release with
`NRestarts=0` and `ExecMainStatus=0`. A fresh phone-originated Telegram E2E
after this deployment remains `PENDING`; no success is inferred from the
previous message.


## 2026-08-24 exact synthesizer delivery patch

The fresh E2E graph t_30714f4d exposed a second delivery-layer failure. The
live graph was valid (4 workers done, verifier 4/4 pass, synthesizer done with
a non-empty result), and the delivery obligation was marked delivered, but the
push notifier woke the main agent after the synthesizer completed. The main
agent rewrote the exact synthesizer result and invented unrequested artifact
claims and an unrelated old task ID. Therefore transport delivered content,
but user-visible content was not trustworthy.

The correction is deployed in immutable release
v2026.8.24-kanban-exact-synth-841f3ada04:

- a completed synthesizer result is sent verbatim to the push adapter;
- the completed synthesizer event no longer wakes a model turn that can rewrite
  or fabricate the result;
- other worker/status/blocked notification paths retain their existing wake
  behavior;
- the regression test requires exact text equality and zero wake turns.

Validation: notifier and wake-ordering suites passed 14 tests. Gateway
restarted into the new release, is active, Telegram polling is connected, and
the dispatcher heartbeat is healthy. A fresh phone-originated E2E after this
release remains PENDING.


## 2026-08-24 Traditional Chinese output gate

The delivered result from root t_30714f4d was not acceptable: it contained
Simplified Chinese and incoherent fabricated joke text. Exact-result delivery
correctly preserved the bad synthesizer output, so a language/quality gate was
missing.

The new contract requires the synthesizer to emit Taiwan Traditional Chinese
and rejects high-signal Simplified glyphs at the kernel completion boundary.
The result is rejected and retried; it is never auto-converted or silently
marked done.

Deployed immutable release:
v2026.8.24-kanban-traditional-2d4f952400
SHA: 2d4f952400e447c0a44bb861965114addb91ad96
Relevant regression suite: 136 passed
Gateway: active, NRestarts=0, Telegram polling connected
Fresh post-release phone E2E: PENDING.

## 2026-08-24 22:18 CST — pending response propagation fix

The live phone test exposed a second delivery defect: the execution guard replaced final_msg with the fail-closed pending text, but final_response still held model prose, so Telegram delivered stale history before the downstream graph completed. The fix synchronizes the guarded final_msg content into final_response before turn finalization. The notifier also logs only synthesizer task ID and result length after exact-result delivery; no result body or secret is logged.

- Regression suite: 18 passed, 5 warnings.
- Immutable release: /home/cwliao/.hermes/releases/v2026.8.24-kanban-pending-039cd20952
- Release marker: 039cd2095201ab95e895e33dc91f03fd72a06533
- Effective drop-in: zzz-kanban-pending-039cd20952.conf
- Gateway: active, PID 3632993, NRestarts=0, ExecMainStatus=0; Telegram polling confirmed healthy after restart.
- Rollback: /home/cwliao/.hermes/releases/v2026.8.24-kanban-traditional-2d4f952400, marker 2d4f952400e447c0a44bb861965114addb91ad96.
- Fresh post-deploy Telegram E2E: pending; do not claim success until the new reply and delivery audit are observed.


## 2026-08-24 22:49 CST — second phone E2E observation

The second phone-originated test used correlation 91a5d0a04c18458f and created root t_37799dc7 with four workers t_e4b79cb9, t_9841cd3b, t_1951d8f3, t_4ee7789f, verifier t_d3d2cdad, and synthesizer t_b660e01f. All four workers and verifier completed. The synthesizer first failed the Traditional Chinese gate, then retried successfully with a non-empty result of length 14. The notifier recorded exact synthesizer delivery for t_b660e01f with chars=14. No matching Telegram delivery_audit line for that direct notifier send was observed, so phone-visible final delivery remains unconfirmed.


## 2026-08-24 23:08 CST — corrupted mixed-script result gate

The second E2E synthesizer result was not acceptable: it contained a Unicode replacement character and Bengali letters mixed into otherwise Han text. The previous gate only rejected selected Simplified Chinese glyphs and required at least one Han character, so it incorrectly accepted the result.

Added a conservative Unicode quality gate: reject U+FFFD replacement characters and non-ASCII alphabetic characters outside Han script. The result is rejected and retried; it is not converted or delivered.

- Immutable release: /home/cwliao/.hermes/releases/v2026.8.24-kanban-unicode-fe2686d6e8
- Release marker: fe2686d6e852f5e738efbf4f1b62195929bee008
- Regression tests: 73 passed
- Gateway: active, PID 3694680, NRestarts=0, ExecMainStatus=0; Telegram connected
- Fresh post-release E2E: pending


## 2026-08-24 current-turn stale-goal binding fix

A fresh Telegram E2E created a new root (t_0d4b7ac4), but the root goal
reused the previous request category. The synthesizer task (t_e8d0bfc4)
therefore produced a previous-run result; the notifier was not the source of
the stale content. This is classified as workflow failure, not success.

Fix:
- bind the authoritative current inbound user text in a turn-local ContextVar;
- for four-lane swarm mutations, override any stale model-supplied goal with
  the current turn text;
- retain explicit goal behavior for non-swarm and CLI calls;
- add regression coverage proving a new cat request cannot create a Winter
  jokes root.

Validation:
- targeted Kanban suite: 134 passed, 5 dependency deprecation warnings;
- immutable release:
  /home/cwliao/.hermes/releases/v2026.8.24-kanban-turn-bound-1431f63a67
- release SHA:
  1431f63a67f34a6d2074d34dbbdb84326695926fd19d61646f4656e337476317
- gateway: PID 3745072, active/running, NRestarts=0, ExecMainStatus=0;
- Telegram polling: confirmed healthy after restart;
- fresh post-deploy Telegram E2E: PENDING.


## 2026-08-25 fresh Telegram E2E after turn-bound release

Correlation e1c3520f1d414ee4 created a new root t_91ad8607. The root title
matched the current small-animal request, proving current-turn goal binding
prevented reuse of the previous Winter goal.

Graph evidence:
- four workers completed;
- verifier t_5cc03073 completed;
- synthesizer t_409d3b84 completed with non-empty result length 32;
- the first synthesizer completion was rejected by the Traditional Chinese
  contract, then a bounded retry completed successfully;
- notifier logged exact synthesizer delivery for t_409d3b84.

The initial pending response had Telegram delivery audits. The background exact
synthesizer send did not produce a matching Telegram delivery_audit line in the
observed journal. Therefore graph completion and notifier send are PASS, while
Telegram transport audit and phone-visible display remain PENDING. Do not claim
user-visible success without that evidence.


## 2026-08-25 synthesizer stale-goal output gate

The fresh E2E root t_91ad8607 correctly bound the current small-animal goal,
but synthesizer t_409d3b84 still produced an unrelated prior-style Traditional
Chinese result. The notifier delivered that task.result exactly; Telegram was
not replaying an older task.

The synthesizer contract previously checked language/format only. Added
goal_anchor_terms derived conservatively from Chinese homophone goals. For a
small-dog goal the anchor is dog; a result without the current anchor is
rejected and retried before task completion or notification.

- Regression suite: 135 passed, 5 dependency warnings
- Release: /home/cwliao/.hermes/releases/v2026.8.25-kanban-goal-anchor-d2dcf94b03
- SHA: d2dcf94b039e7041bad2bfa1d7f739eff3cb7d4cf8501de0e0fe7755dea7af9a
- Gateway: PID 3773777, active/running, NRestarts=0, ExecMainStatus=0
- Telegram polling: connected and healthy
- Fresh post-release Telegram E2E: PENDING

## 2026-08-25 consensus-revised synthesizer lifecycle ticket

Three independent cross-reviewers reviewed the proposed timeout/retry ticket.
All three returned the same verdict: the direction is correct, but the ticket
must define the full synthesizer attempt lifecycle before implementation. The
Consensus plugin was not callable in this session, so this is recorded as
local cross-review consensus, not external Consensus research.

### Ticket

**KANBAN-SWARM-002 — Define and enforce synthesizer attempt lifecycle,
ownership fencing, terminal propagation, and recovery invariants**

### Problem statement

The observed synthesizer task timed out after its 300-second run limit. The
first run became `timed_out` and a retry started, while the logical task was
observed as `running`. Existing generic retry/breaker behavior is finite, but
it does not yet provide a complete, auditable contract for task state,
attempt state, process termination, late completion, root propagation, or
user-visible Telegram status.

The fix must prevent a timeout from creating duplicate active synthesizers,
stale `running` state, false root completion, stale-result delivery, or an
unbounded token/runtime burn.

### Scope and state contract

Treat a logical Kanban task and each execution attempt as separate entities.
Every attempt has a unique `run_id` and ownership claim.

Attempt states:

```text
pending -> running -> succeeded
                  -> failed
                  -> timed_out
                  -> canceled
```

Logical synthesizer task states:

```text
todo/ready -> running -> retrying -> ready -> running
                              \\-> blocked/failed
                 \\-> succeeded
```

The implementation may map these states to the existing schema, but the
mapping must be explicit and tested. `timed_out` is an attempt outcome, not a
successful logical task state. A retry-exhausted task must be terminal and
must not be auto-claimed again.

### Required behavior

1. Define a per-role retry policy. State whether the limit counts total
   attempts or additional retries; define timeout, crash, spawn failure and
   invalid-output handling; define backoff/cooldown and an overall deadline.
   The policy must not rely on an ambiguous global failure counter.
2. On timeout, close the old `task_run` exactly once with `outcome=timed_out`.
   Confirm SIGTERM/SIGKILL termination before releasing ownership and starting
   a successor attempt. If termination is not confirmed, enter an explicit
   `termination_pending` recovery path instead of spawning in parallel.
3. Fence ownership with `run_id`/claim CAS. A late heartbeat, completion,
   block, process exit, or replayed timeout from an old run must be rejected
   and must not modify the successor run, result, or terminal task state.
4. Require the verifier dependency to be the successful result for the same
   root and generation. Verifier completion alone is insufficient.
5. Propagate truthful state: while synthesizer is running/retrying, the root
   cannot be done; after retry exhaustion the root is explicitly blocked or
   failed; only a non-empty, contract-valid synthesizer result can succeed.
6. Keep Telegram progress truthful: retry means retry, timeout means timeout,
   spawn failure means spawn failure, and retry exhaustion is terminal. No
   final notifier delivery may use an old root, old result, event summary, or
   status-only text.
7. Record bounded observability fields without result bodies or secrets:
   `root_id`, `verifier_id`, `synthesizer_id`, `run_id`, `role`, `attempt`,
   `max_attempts`, elapsed/timeout seconds, termination confirmation,
   retry status, failure reason, and final state.

### Acceptance criteria

- First synthesizer timeout produces exactly one timed-out attempt and at
  most one bounded successor attempt.
- Second timeout or retry-budget exhaustion produces one terminal
  `blocked`/`failed` state and one truthful terminal event; no further spawn.
- Timeout cleanup leaves no stale claim, expiry, worker PID, current run, or
  open run for the old attempt; at most one active synthesizer exists.
- A late `kanban_complete` from an old run is rejected, logged as stale-run
  ownership failure, and cannot change the successor or root state.
- Dispatcher timeout and worker iteration-budget timeout are idempotent: one
  failure count, one timeout event, one run close, and no duplicate retry.
- Restart recovery handles stale `running`, dead PID, missing current run,
  orphaned run, pending retry, and retry-launch failure without duplicate
  spawn.
- Verifier success and synthesizer retry exhaustion produce a blocked/failed
  root, never a successful graph or empty final deliverable.
- Retry and terminal Telegram notifications are truthful and correlated to
  the current root/run; final delivery occurs only after exact synthesizer
  result validation.
- Regression tests cover timeout-then-success, repeated timeout, mixed
  timeout/crash, failed termination, late completion, duplicate dispatcher
  tick, restart recovery, notifier ordering, and output-contract rejection.
- A new phone-originated Telegram E2E uses a unique correlation and goal:
  pending response, verifier completion, synthesizer retry/timeout behavior,
  exact final-result delivery or truthful terminal failure, matching delivery
  audit, and actual phone-visible evidence are recorded as separate gates.

### Review decision

`REQUEST CHANGES` before implementation. This ticket is now sufficiently
specified for a design review, but implementation should not begin until the
maintainer chooses concrete defaults for `max_attempts`, backoff/cooldown,
overall deadline, termination grace period, and the terminal state name.

## 2026-08-25 consensus-final revision — implementation defaults

The three-reviewer consensus identified the previous remaining ambiguity:
the ticket listed required decisions but did not select safe defaults. This
revision makes those defaults explicit so implementation and tests can begin
without silently changing retry semantics.

### Approved defaults

- `max_attempts = 2` total attempts: one initial attempt plus one retry;
  `max_retries = 1` is derived, not an additional budget.
- Per-attempt synthesizer wall-clock timeout: `300s`.
- Retry backoff/cooldown: `30s`, with no same-tick respawn.
- Termination grace period: `15s`; retry is forbidden until the old PID is
  confirmed gone and the old claim/run is closed.
- Overall synthesizer deadline: `660s` from the first attempt start. This is
  a hard cap that includes attempt runtime, termination grace, backoff, and
  scheduler overhead.
- Terminal logical task state after budget exhaustion: `blocked` with
  `block_kind=synthesizer_retry_exhausted` and one `gave_up` event.
- If process termination cannot be confirmed: `blocked` with
  `block_kind=termination_pending`; do not spawn a successor automatically.
- Retryable outcomes: timeout, transient worker exit, transient spawn failure,
  and one output-contract rejection. Permanent dependency failure and an
  ownership/CAS rejection are not retried automatically.

### State and ownership invariants

1. Every attempt has one immutable `run_id`; `tasks.current_run_id`, claim
   ownership, worker PID, and the open `task_run` must refer to that same run.
2. The timeout transition is one atomic compare-and-set operation. It closes
   the old run once with `outcome=timed_out`, increments the failure budget
   once, and emits one timeout event.
3. Only after termination is confirmed does the logical task become `ready`
   for the single 30-second-delayed retry. A retry cannot be claimed in the
   same dispatcher tick as the timeout.
4. A heartbeat, completion, block, timeout replay, or process-exit callback
   carrying an old `run_id` is rejected as stale ownership and cannot modify
   the successor result, task state, root state, or notifier cursor.
5. After the second attempt fails, the task is terminal `blocked`; no later
   dispatcher tick, event replay, or restart recovery may spawn another run.
6. The swarm root becomes `blocked` with the same bounded failure reason. It
   cannot be `done` unless the synthesizer is `done` with a non-empty,
   contract-valid result for the same root/generation.

### Required implementation changes

- Represent the attempt number, retry budget, timeout reason, termination
  result, and final state in bounded task/run metadata; never log result
  bodies, Telegram text, credentials, or tokens.
- Separate timeout, crash, spawn failure, invalid output, dependency failure,
  and stale ownership in event kind/reason fields. `gave_up` must not claim a
  timeout was a spawn failure.
- Make dispatcher timeout and worker iteration-budget timeout idempotent for
  one `run_id`; duplicate failure accounting and duplicate `gave_up` events
  must be rejected.
- Add recovery reconciliation for stale `running`, dead PID, missing
  `current_run_id`, orphaned run, pending retry, and failed retry launch. The
  recovery action must be requeue, terminal block, or manual review—not an
  unbounded respawn.
- Keep verifier success bound to the same root/generation. A completed
  verifier from another turn cannot unlock this synthesizer.

### Final acceptance matrix

| Scenario | Required result |
|---|---|
| First timeout, old PID terminated | exactly one timeout event, one delayed retry |
| First timeout, kill not confirmed | `termination_pending`; no retry |
| First timeout, second attempt succeeds | synthesizer/root `done`; exact valid result only |
| Second timeout or retryable failure | synthesizer/root `blocked`; one truthful terminal event |
| Late old-run completion | rejected as stale; successor/root unchanged |
| Duplicate dispatcher tick/event replay | no duplicate run, counter, event, or notification |
| Gateway restart with orphan state | deterministic reconcile to retry or blocked |
| Invalid Traditional/Unicode/goal-anchor output | one bounded retry; then blocked if repeated |
| Retry in progress | no final success delivery and no old-result delivery |

### Telegram E2E definition

For a new phone-originated correlation and unique goal, record these gates
separately: pending inbound response, four worker completion, verifier
success for the same root, synthesizer attempt/retry state, output-contract
validation, exact-result notifier delivery, matching delivery audit, and
actual phone-visible message. Gateway health, Kanban `done`, API delivery, or
an old Telegram message cannot substitute for the phone-visible gate.

### Final review decision

`APPROVED FOR DESIGN/IMPLEMENTATION` with the defaults above. The ticket is
not a claim that the fix is implemented; source changes, focused tests,
immutable release deployment, and a fresh Telegram E2E remain required before
marking it complete.
