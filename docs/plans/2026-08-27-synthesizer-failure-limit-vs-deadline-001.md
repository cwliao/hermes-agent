status: CONSENSUS (claude and agy independently read kanban_db.py and
kanban_swarm.py and converged on near-identical numbers; groq's specific
code citations were fabricated/hallucinated -- no real file access -- but
its numeric recommendation happens to agree, treated as weak corroboration
only; codex and grok timed out on dispatch this round, review-only so no
edit risk). See t_e38248b8.

## Consensus outcome

Function name correction: the doc's references to `detect_dead_workers()`
should read `enforce_max_runtime()` -- that's the actual function name in
`hermes_cli/kanban_db.py` containing the `is_synth`/`deadline_exceeded`
logic around line 9298.

No new API surface needed: `create_task()`'s `max_retries` column already
has top precedence in `_record_task_failure`'s resolution order (task
override -> caller-supplied `failure_limit` -> `DEFAULT_FAILURE_LIMIT`).
The synthesizer task created by `create_swarm()` simply never sets it
(`kanban_swarm.py:~1057` passes neither `max_retries` nor a runtime cap
override), so it silently falls through to the generic default of 2.

Concrete implementation (claude + agy converged):

1. Add `DEFAULT_SYNTHESIZER_MAX_RUNTIME_SECONDS = 1800` (30 min) in
   `hermes_cli/kanban_swarm.py`, alongside the existing
   `DEFAULT_WORKER_MAX_RUNTIME_SECONDS` / lane constants. Use it for the
   synthesizer's `create_task()` call instead of
   `DEFAULT_WORKER_MAX_RUNTIME_SECONDS`. Leave workers/verifier at 1200s
   (unaffected role, completed fine in the reproduction).
2. Pass `max_retries=2` explicitly when creating the synthesizer task
   (wires the existing column that was previously left `None`).
   1800 * 2 = 3600s >= the deadline (see next point), so
   `deadline_exceeded` becomes reachable instead of dead code.
3. Raise `_SYNTHESIZER_OVERALL_DEADLINE_SECONDS` in `kanban_db.py` from
   2700 to 3600, matching the new 1800*2 ceiling with no slack lost.
4. Raise `providers.vllm-local.request_timeout_seconds` in
   `~/.hermes/config.yaml` from 1200 to 1800+ so the HTTP client timeout
   doesn't preempt a longer synthesizer generation before the new 1800s
   worker-level cap even gets a chance to apply (config change, not code;
   matches this session's own established precedent for that setting).
5. Add a regression-guard test asserting
   `DEFAULT_SYNTHESIZER_MAX_RUNTIME_SECONDS * <synthesizer max_retries>
   >= _SYNTHESIZER_OVERALL_DEADLINE_SECONDS`, so this exact
   independently-tuned-constants mismatch cannot silently recur.
6. Check `tests/hermes_cli/test_kanban_swarm_synthesizer_lifecycle.py`'s
   `test_synthesizer_overall_deadline_forces_exhaustion` -- agy found it
   manually constructs its synthesizer fixture with `max_retries=5`,
   which is why this production bug (real `create_swarm()` output never
   setting `max_retries`) wasn't caught by the existing suite. Decide
   whether that test should be adjusted to reflect the new real default
   (`max_retries=2`) while still reliably forcing exhaustion, or left as
   an intentionally-larger-N stress case -- implementer's judgment, not
   mandated here.

Question 4 from the original doc (is 30k+ reasoning tokens itself
suspicious) is resolved: no. Both real reviewers independently traced the
synthesizer's `goal_mode` + up-to-5-judge-evaluated-turns +
`DEFAULT_OUTPUT_CONTRACT_POLICY` quality-gate-retry design (and, per agy,
the `humanizer` skill's draft/self-audit/final-rewrite structure) as
legitimate, intentional mechanisms that can burn many reasoning tokens
without any loop/bug -- consistent with the vLLM evidence (steady
single-stream throughput, zero queueing). No further investigation needed
before shipping this fix; a `hermes --resume <session_id>` on a
still-timing-out session remains cheap follow-up insurance if the fix
doesn't resolve it, not a prerequisite.

# Synthesizer's generic 2-strike failure breaker fires before its own 2700s deadline

## Problem

Earlier this session (commit `3f16898d3a`), `_SYNTHESIZER_OVERALL_DEADLINE_SECONDS`
in `hermes_cli/kanban_db.py` was raised from 660 to 2700 seconds (45 minutes)
so a swarm's synthesizer task would get meaningful headroom across retries
before being permanently blocked
(`force_trip=True`, `block_kind="synthesizer_retry_exhausted"`).

That fix only works if the synthesizer actually gets to run long enough,
across enough retries, to reach 2700s of wall-clock time since its first
`started_at`. It does not, because of an unrelated, more general circuit
breaker:

```python
# hermes_cli/kanban_db.py:8611
DEFAULT_FAILURE_LIMIT = 2
```

`_record_task_failure()` (used for ANY task, not just synthesizers) trips
this breaker after 2 consecutive non-successes and force-blocks the task —
independent of the synthesizer-specific deadline check. The synthesizer's
own per-attempt cap is `DEFAULT_WORKER_MAX_RUNTIME_SECONDS = 1200` (20
minutes). Two consecutive 1200s timeouts = 2400s (40 min), which trips the
2-strike breaker in `detect_dead_workers()` / `_record_task_failure()`
(`kanban_db.py:~9298-9321`) before the `deadline_exceeded` check
(`(now - started_at) >= _SYNTHESIZER_OVERALL_DEADLINE_SECONDS`) ever gets a
chance to fire on a 3rd attempt. The 2700s deadline raised earlier this
session is effectively unreachable for a synthesizer that times out (as
opposed to erroring quickly) on most or all attempts.

## Reproduction (this session, 2026-08-27)

Same swarm as the verifier-skill-mismatch bug (root `t_9e9e56fd`), after
that fix was deployed and the verifier (`t_b6aa50ac`) succeeded. The
synthesizer (`t_3e1af107`) ran twice, both timing out at the per-attempt
cap:

```
#1  timed_out  20m  started 09:26  elapsed 1203s > limit 1200s
#2  timed_out  20m  started 09:47  elapsed 1205s > limit 1200s
```

After run #2, the task went straight to `blocked` (`gave_up`) at ~40
minutes total elapsed -- 5 minutes short of the intended 45-minute budget.

`docker logs vllm-production` for both timeout windows shows
`Running: 1 reqs, Waiting: 0 reqs` continuously, with steady ~25 tokens/s
generation throughput -- no contention, no queueing, no stall. This is
apparently this DGX Spark host's real single-stream generation speed for
gpt-oss-120b. A synthesis step that reads 4 workers' outputs plus a
verifier pass, and produces a full Traditional-Chinese synthesis after
gpt-oss's typically heavy internal reasoning, can plausibly need more than
1200s of generation at ~25 tok/s (1200s * 25 tok/s = 30,000 tokens, which
is not an unreasonable reasoning+output budget for this model on a slow
task).

This is NOT the same failure mode as the verifier-skill-mismatch bug
(docs/plans/2026-08-27-kanban-swarm-verifier-skill-mismatch-001.md) --
that one was a deterministic, guaranteed failure (wrong skill, invalid
tool calls). This one is a resource/timing mismatch between two
independently-tuned constants, surfaced by genuinely slow (but working)
inference.

## Proposed fix

Reviewers should weigh in on which of these (or a combination) is right;
my initial read:

1. **Give the synthesizer a higher generic failure-limit than the default
   2**, via the existing `failure_limit` override mechanism already used
   elsewhere (`_record_task_failure`'s `failure_limit` param, resolved in
   order: task-level override -> caller-supplied -> `DEFAULT_FAILURE_LIMIT`
   -- see `kanban_db.py:4948-4958` and `9985-9999`). Check whether a
   task-level or role-level (`is_synth`) override already exists as a
   plumb-through option before inventing new API surface -- this pairs
   naturally with the existing `is_synth` branch already present in
   `detect_dead_workers()`'s timeout-handling block (the same function
   that computes `deadline_exceeded`).

2. **Raise `DEFAULT_WORKER_MAX_RUNTIME_SECONDS` specifically for the
   synthesizer role** (not workers generally -- workers are lane-bound
   tasks like Quicksort/MergeSort here and completed fine within the
   existing cap) so a single attempt has more realistic headroom given the
   measured ~25 tok/s throughput. This reduces retries needed, which
   indirectly avoids tripping the 2-strike breaker, without touching the
   breaker itself. Would need to become a synthesizer-specific constant
   (or parameter) rather than reusing the shared
   `DEFAULT_WORKER_MAX_RUNTIME_SECONDS`, since that same constant is used
   for lane workers too (see `_default_worker_max_runtime_seconds()` /
   `DEFAULT_EXTERNAL_LANE_WORKER_MAX_RUNTIME_SECONDS` --  already
   lane-aware, so there is precedent for a role-specific runtime cap).

3. **Do nothing / reject**: is 2 attempts x 20 min actually a reasonable
   ceiling and the real problem is that this specific swarm's synthesis
   goal is too heavy for one shot? Reviewers should sanity-check whether
   30,000+ tokens of reasoning for "compare 4 sorting algorithms and write
   a synthesis" is itself a sign of something behaving oddly (e.g.
   excessive/looping reasoning) rather than a legitimately hard task, since
   we don't have the synthesizer's own session log content (the per-task
   log file was 0 bytes while the run was in progress; only vLLM-side
   throughput/queue metrics were available for this diagnosis, not the
   actual token stream/reasoning content). If reviewers think this
   diagnosis is incomplete without seeing the session content, say so --
   this may need a `hermes --resume <session_id>` on the actual timed-out
   session to inspect before deciding.

Recommend (1) + (2) together: raise the synthesizer's per-attempt cap to
something with more realistic headroom at ~25 tok/s (reviewers pick the
number), AND make sure whatever failure-limit governs the synthesizer is
actually compatible with reaching `_SYNTHESIZER_OVERALL_DEADLINE_SECONDS`
(2700s) -- e.g. if per-attempt cap becomes 1800s, `DEFAULT_FAILURE_LIMIT=2`
already allows 3600s of attempts, exceeding 2700s, so the deadline would
correctly fire first; recompute whichever numbers are chosen so the
deadline is actually reachable given the attempt cap x failure limit,
rather than independently retuning both without checking their product
against the deadline.

## Non-goals

- Not touching `DEFAULT_FAILURE_LIMIT` globally (used by every task type,
  not synthesizer-specific) unless reviewers determine a role-scoped
  override doesn't exist and inventing one is overkill for this ticket.
- Not investigating why gpt-oss-120b's single-stream throughput on this
  host is ~25 tok/s (that's a separate hardware/serving-config question,
  not a Hermes bug).
- Not touching the verifier-skill-mismatch fix (already deployed,
  separate ticket).

## Verification plan (post-fix)

- Existing kanban_swarm / kanban_db synthesizer-lifecycle tests must still
  pass (`tests/hermes_cli/test_kanban_swarm_synthesizer_lifecycle.py` and
  related).
- Add/adjust a test asserting the chosen attempt-cap x failure-limit
  combination is actually >= `_SYNTHESIZER_OVERALL_DEADLINE_SECONDS`
  (i.e. a regression guard against this exact mismatch recurring when
  either constant is tuned independently in the future).
- Manually retry the still-blocked `t_3e1af107` (or a fresh equivalent
  swarm) end-to-end post-deploy and confirm the synthesizer produces a
  non-empty result within the new budget.
