status: CONSENSUS (claude, agy, groq independently verified the root-cause
trace against real code -- unanimous, no corrections; codex and grok timed
out on dispatch, review-only so no risk; groq's specific code citations
contained some fabricated function/field names -- e.g. `db.update_task_body`,
`task.is_swarm`, `prompt_for_triage` -- none of which exist in this repo,
so only its high-level reasoning is used, not its code claims). See
t_b07785d4.

## Consensus outcome (revised scope from cross-review)

**Fix A: REFUSE TO AUTO-REWRITE, not mechanically preserve.** claude and
agy disagreed on approach; agy's finding is decisive and changes the
recommendation. agy found the actual risk is worse than marker loss
alone: if the auto-decomposer's LLM classifier picks `fanout: true`
instead of `fanout: false` for a triaged swarm-role task,
`decompose_triage_task()` (`kanban_db.py:~8000-8100`) creates NEW CHILD
TASKS and rewrites `task_links` -- this would corrupt the swarm's entire
DAG topology (worker/verifier/synthesizer graph, quorum calculations),
not just lose one marker line. A mechanically-preserved marker doesn't
protect against this path at all. Additionally, the verifier's body
carries other machine-generated sections (`_completion_requirements()`,
`_completion_call_example()`) specifically written to prevent agent
confusion on retry -- an LLM prose-rewrite destroys those too, even if
the trailing contract line itself were preserved.

Guard: add an early check (`if extract_contract(task.body): refuse`) in
both `kanban_decompose.py::decompose_task()` AND `kanban_specify.py::
specify_task()` (agy found this second entry point also calls
`specify_triage_task()` and was missing from the original draft's scope)
-- for either, if a `[swarm:contract]` marker is present in the current
body, do not auto-rewrite/auto-decompose; leave the task as-is and route
to the same diagnostic mechanism as Fix B below, so refusing doesn't
create a *new*, differently-silent stall (a task parked in `triage`
forever because auto-decompose declined to touch it, with nobody told
why). Add a defense-in-depth guard inside `specify_triage_task()` itself
in case a future caller reaches it through neither of these two paths.

**Fix B: expanded scope.** agy found `recompute_ready`'s existing
`if all(p["status"] in ("done", "archived") for p in parents)` check
already correctly proves anything past that point is evaluating a
completed verifier, not a waiting one -- so the "distinguish waiting vs.
anomaly" framing in the original draft was already naturally satisfied
by existing code structure, not something to newly build. What Fix B
actually needs to add:
1. Emit a diagnostic (a kanban event, e.g. `kind="verifier_gate_rejected"`,
   deduplicated so the ~1s dispatcher tick doesn't spam duplicate rows --
   agy's point) when the verifier is `done` but contract/metadata is
   missing or malformed (the actual 2026-08-27 incident).
2. Transition the synthesizer to `status="blocked"` with a human-readable
   reason (not just log/event) so the stall is actionable directly on the
   board, not just discoverable by an operator who already suspects
   something and goes log-diving (agy's stronger recommendation, adopted
   over the original draft's softer "diagnostic" framing).
3. Cover the same diagnostic for the *new* Fix-A-created stall case
   (task refused for auto-rewrite, parked in triage) so Fix A doesn't
   trade one silent stall for another.
4. The earlier cross-review claim that "a verifier that legitimately
   completes with `gate="fail"` currently hits the exact same silent
   `continue`" turned out to be unreachable. `validate_completion()`
   already hard-requires `gate="pass"` for any verifier
   `kanban_complete`; a non-pass gate is rejected before the task can
   become `done`, so `recompute_ready` never observes a completed
   verifier with `gate != "pass"`. A verifier that finds a real problem
   calls `kanban_block` instead (status stays `blocked`), which the
   existing parent-status check already treats as a normal wait. Fix
   B's scope is therefore just the missing/malformed-contract-or-metadata
   case -- the actual incident -- not a separate `gate=fail` case. The
   `raw_gate != "pass"` branch in `recompute_ready` is kept only as
   defense-in-depth.

**Defense-in-depth addition (both claude and agy independently raised
this):** `extract_contract()` (`kanban_swarm.py:548-559`) currently
returns `None` for both "no marker line at all" and "marker line present
but `json.loads` fails" -- these are silently identical to every caller.
Have it distinguish the two (e.g. raise or return a sentinel for the
malformed case), and have `validate_completion()` reject a completion
outright when the contract is present-but-malformed (as opposed to
genuinely absent, which remains a legitimate no-op for non-swarm tasks).
This is narrow, low-risk, and closes a related but distinct blind spot
from today's specific incident.

**On rejecting a `validate_completion()`-based primary fix:** confirmed
correct to keep as non-primary (conflates "no contract" with "contract
lost," as originally reasoned) -- the malformed-vs-absent distinction
above is the right-sized defense-in-depth piece to take from this
discussion, not a broader change to `validate_completion()`'s role-branch
logic itself (which both reviewers confirmed is already correct and
symmetric between worker/verifier).

# A swarm's `[swarm:contract]` marker can be silently destroyed by the generic auto-decomposer, stalling the synthesizer forever with no error

## Problem

Discovered 2026-08-27 during regression testing. Swarm root `t_2c9416c4`,
verifier `t_ef898954`, synthesizer `t_e563a66f`. The verifier completed
(`status=done`, plausible summary "All four sorting algorithm lanes
verified; contract satisfied.") but the synthesizer never promoted from
`todo` to `ready`. Confirmed via directly calling `kb.dispatch_once(conn)`:
runs cleanly, `promoted=0`, no error, no log line -- indefinitely.

## Root cause (traced precisely via the actual event log, not speculation)

1. The verifier task was created normally with a `[swarm:contract]
   {...role:"verifier"...}` marker line appended to its body (per
   `hermes_cli/kanban_swarm.py`'s `_contract_line()`).
2. The verifier blocked twice for an unrelated, spurious reason
   ("missing bubble sort" -- itself likely model confusion during
   today's earlier network instability window, not a real defect),
   tripping this codebase's generic block-loop circuit breaker
   (`block_loop_detected` event), which routed the task to the `triage`
   column. This is a generic Kanban safety mechanism with no swarm
   awareness.
3. The gateway's auto-decomposer background job
   (`gateway/kanban_watchers.py:1859-1919`) picked up the triaged task
   and called `hermes_cli/kanban_decompose.py::decompose_task()`, which
   used an LLM to rewrite the task's `title`/`body` into plainer language
   and called `specify_triage_task()` (`hermes_cli/kanban_db.py:7789`) to
   commit the rewrite and promote the task back to `todo`.
4. `specify_triage_task()` is a fully generic primitive -- a blind
   `UPDATE tasks SET body = ?` with whatever body the caller supplies
   (`kanban_db.py:7826-7829`). Neither it nor `kanban_decompose.py`
   (confirmed via grep: zero hits for "swarm"/"contract") knows the
   `[swarm:contract] ` line is machine-load-bearing. The LLM's rewritten
   body preserved the human-readable instructions (including the literal
   JSON metadata example, in prose) but dropped the actual
   `[swarm:contract]` marker line, since nothing told it that line was
   special.
5. The verifier resumed on the rewritten body, called `kanban_complete`
   with only `summary=` (no `metadata=`), and **this passed validation
   silently** -- not because verifiers get a weaker check than workers,
   but because the contract marker that would have triggered any check
   at all was already gone.

## Confirmed NOT a validate_completion weakness for verifiers

Investigated directly: `hermes_cli/kanban_swarm.py:600-602`'s
`validate_completion()` runs `extract_contract(task.body)` *before*
branching by role; if no contract is found (`extract_contract()` returns
`None` because there's no `[swarm:contract] ` line in the body), it
short-circuits and returns `None` (no rejection) **regardless of role**.
The `role == "verifier"` branch (`kanban_swarm.py:628-635`), when a
contract *is* present, checks `gate == "pass"` plus lane counts exactly
as strictly as the worker branch does. There is no verifier-specific
weakening in the role-branch logic itself -- the bug is entirely that the
contract marker was destroyed upstream, by a completely different,
swarm-unaware subsystem.

## How promotion actually determines "verifier passed" (confirmed)

`recompute_ready()` in `kanban_db.py:4980-5045`. It only engages this
extra gate if the **synthesizer's own body** still carries a
`[swarm:contract]` with `role == "synthesizer"` (line 5001) -- confirmed
present here. It then independently re-reads the **verifier's body** for
its own `[swarm:contract]` marker (`verifier_contract`, lines 5011-5024)
and the verifier's last completed run's metadata (`verifier_metadata`,
lines 5025-5036). Promotion is blocked (`continue`, silently skipping the
row) unless `verifier_contract` is present with matching role/root_id
**and** `verifier_metadata.get("gate") == "pass"` with matching
`root_id`. Since the verifier's body contract was gone, `verifier_contract
is None` -> `continue` fires on every dispatch tick forever, with no
event, log line, or status change.

## Two independent gaps, not one

- **Cross-subsystem gap (the actual root cause):** the generic
  triage/auto-decompose path can silently destroy a swarm-role task's
  machine-readable contract as a side effect of an LLM rewrite it has no
  awareness of.
- **Promotion-diagnostic gap:** `recompute_ready` *correctly* refused to
  promote on missing/invalid verifier contract (this actually saved the
  swarm from silently accepting an unverified "pass") -- but it fails
  "loudly-never": nothing distinguishes this anomaly from the ordinary
  "verifier hasn't completed yet, keep waiting" case, so an operator has
  no way to discover the swarm is stuck short of manually diffing a
  verifier's actual completion event against its own body's stated
  contract, as done here.

## Proposed fix (two narrow, independent changes)

**Fix A -- prevent the marker loss** (the actual root cause):
`specify_triage_task()` (`hermes_cli/kanban_db.py:7789`, or one layer up
in `kanban_decompose.py::decompose_task()` before it constructs the new
body) should preserve any trailing `[swarm:contract] ` line from the
existing body when overwriting it -- e.g. detect the marker in the old
body and re-append it verbatim to the new body if the LLM-rewritten body
doesn't already end with one. Alternative, more conservative option:
refuse to auto-specify (via the LLM rewrite path) any task whose current
body carries a `[swarm:contract]` marker at all, routing it to a human
instead -- since a lane-mode swarm role task hitting the generic
block-loop/triage path is already an edge case the auto-decomposer likely
wasn't designed to handle safely. Reviewers should pick between
"preserve the marker mechanically" vs. "refuse to auto-rewrite swarm-role
tasks" -- both close the hole; the latter is more conservative (never
touches swarm task bodies via this generic path at all) at the cost of
requiring human intervention for a rare edge case.

Regression risk: low for ordinary (non-swarm) triage tasks -- they never
carry this marker, so behavior is unchanged either way.

**Fix B -- surface the stall**: `recompute_ready`'s verifier-gate
`continue` branch (`kanban_db.py:~5037-5045`) should distinguish "verifier
hasn't completed yet" (normal -- keep waiting silently) from "verifier is
`status == 'done'` but its contract/metadata is missing or malformed" (an
anomaly -- should emit a diagnostic, e.g. a comment/event on the
synthesizer or root, or a log line at minimum) rather than an
indistinguishable silent no-op in both cases.

Regression risk: must not fire the diagnostic for the ordinary waiting
case (only when verifier status is actually `done`). Naturally scoped
away from non-lane-mode swarms since the whole block is already gated on
the synthesizer's own `[swarm:contract]` with `role == "synthesizer"`
being present (line 5001) -- ungated/non-contract swarms are unaffected
either way.

**Explicitly rejected**: extending `validate_completion()` itself to
"require `gate=pass` metadata for verifiers even when no contract is
present." That would conflate "no contract present" (legitimate for
non-lane-mode swarms and plain Kanban tasks generally) with "contract was
lost" (this bug). The fix belongs upstream of `validate_completion`
(Fix A) plus a diagnostic in the promotion path (Fix B), not inside
`validate_completion` itself.

## Non-goals

- Not investigating the original "missing bubble sort" spurious block
  that triggered the block-loop breaker in the first place -- separate,
  likely network-instability-related model confusion, not a code defect.
- Not touching `validate_completion()`'s existing role-branch logic
  (confirmed correct and symmetric between worker/verifier).
- Not building general "protect all special body markers from all
  rewrite paths" infrastructure -- scope this narrowly to the
  `[swarm:contract]` marker and the specific triage/auto-decompose path
  that destroys it.

## Verification plan (post-fix)

- Fix A: test that `specify_triage_task()`/`decompose_task()` preserves
  (or refuses to touch) a `[swarm:contract]` marker present in the
  pre-rewrite body -- assert the marker survives (or the rewrite is
  refused) and `extract_contract()` still returns the original contract
  afterward.
- Fix B: test that `recompute_ready()` emits a diagnostic when a
  synthesizer's gating verifier is `done` but its contract/metadata check
  fails, and does NOT emit anything when the verifier simply hasn't
  completed yet (regression guard against false-positive noise).
- Manual reproduction: recreate a similar block-loop -> triage ->
  auto-decompose sequence on a real swarm verifier and confirm the
  contract marker survives and/or the operator gets a visible signal.
