# SWARM-LANE-TIMEOUT-RETEST-001

Status: investigated and fixed, 2026-08-21. A live retest of PR #94's
lane-aware `worker_max_runtime_seconds` fix
(`docs/plans/2026-08-20-swarm-claude-grok-lane-timeout-recurrence-001.md`)
confirmed the raised ceiling works as intended (external lanes now
survive past the old 300s kill point), but surfaced two distinct,
more specific, more fixable root causes that a runtime-ceiling change
alone cannot address. Both are fixed here.

## What the retest showed

Re-ran the identical 4-lane swarm shape (tenant `lane-fix-verify-v1`,
root `t_8f8d6ef0`) against the just-deployed PR #94 release
(`v2026.8.21-lane-aware-worker-max-runtime-b99a661012`), with no
explicit `--worker-max-runtime` override so the new lane-aware default
applied: 120s for `native_hermes`, 600s for `claude`/`grok`/`agy`.

**The ceiling raise worked**: `claude` and `grok` both survived past
the old 300s kill point (they were still running, heartbeating
normally, well past 300s elapsed) -- something that never happened in
the original incident or the first retest. `grok` and `agy` both
eventually completed successfully on retry. `native_hermes` itself
timed out this run (120s ceiling, unchanged by PR #94, unrelated
run-to-run variance) and gave up, which incidentally freed a dispatch
slot for `agy` sooner.

**But `claude` and `grok` still eventually hit their new 600s ceiling**
on the first attempt (elapsed ~604s each) before completing. Reading
each lane's full persisted transcript (`~/.hermes/state.db`'s
`messages` table, keyed by the session id `agent.log` shows for each
task) directly -- not just the dispatcher's `task_events` timestamps --
found two distinct, unrelated causes, not "needs even more time":

### Finding 1: Tirith false-positives on ordinary Chinese punctuation

`claude`'s worker ran `claude -p '...用繁體中文...。...' --allowedTools ''
--max-turns 3 --output-format json`. The `terminal` tool's security
scan (Tirith, `tools/tirith_security.py`) flagged this command:

```
Security scan — [HIGH] Confusable Unicode characters in text: Content
contains Unicode characters visually identical to ASCII (math
alphanumerics, Cyrillic/Greek lookalikes) appearing near ASCII text,
which may indicate a homoglyph attack
```

Confirmed directly via `~/.hermes/bin/tirith check --json
--non-interactive --shell posix -- "<the exact command>"`: the finding
(`rule_id: confusable_text`) is real, deterministic for this exact
command, and both evidence entries are `U+3002` (IDEOGRAPHIC FULL
STOP) -- the ordinary full stop that ends nearly every Chinese
sentence, flagged as "looks like '.'" merely for appearing near ASCII
CLI flag syntax. This is unavoidable for any CJK-language worker
prompt passed to an external CLI as a `-p` argument alongside flags
like `--allowedTools ''`.

The flagged command wasn't hard-blocked with no recourse -- it went
through `tools/approval.py`'s smart-approval path (an auxiliary LLM
call) and was auto-approved, per the persisted transcript's own
`"approval"` field: *"...auto-approved by smart approval."* But that
LLM round-trip cost **~88 seconds** of the worker's runtime budget for
a single flagged command, and the same false positive can fire more
than once per worker turn (`claude`'s second attempt hit it again on a
follow-up command). Combined with `claude` needing 8 heartbeat cycles
total (vs. 3 for `native_hermes` completing cleanly in the original
test), this false-positive tax was enough on its own to push `claude`
from "would have finished comfortably" to "killed with the joke
already produced and validated, mid-completion."

Isolated black-box testing against `tirith check` directly (not part
of the fix, just verification) found the trigger is not a simple
"any U+3002 near ASCII" rule -- some U+3002-containing commands
triggered it, some didn't, seemingly depending on total command
complexity/length that wasn't fully characterized. What IS
established with high confidence: **when it does fire, the evidence is
consistently and exclusively U+3002**, confirmed via multiple repeated
runs of the exact failing command.

**Fix**: `tools/tirith_security.py` gains a narrow, evidence-based
suppression (matching the existing `.app` TLD suppression's
philosophy) -- a `confusable_text` finding whose entire evidence set is
`U+3002` and nothing else is downgraded to `allow`, whether Tirith
reported `block` or `warn` (the .app suppression only covers `warn`;
this one deliberately also covers `block`, since that's what was
actually observed live). Any finding containing so much as one
different codepoint -- a genuine math-alphanumeric/Cyrillic/Greek
lookalike, or an unverified CJK punctuation mark -- is NOT suppressed,
preserving full detection value against real homoglyph attacks. Only
U+3002 is allowlisted; extending to other common CJK punctuation
(fullwidth comma, colon, etc.) would need its own verification pass
the same way, not a guess -- ad-hoc testing during this investigation
found several other common marks (fullwidth comma/colon/semicolon/
exclamation/question mark) did NOT reproduce the flag in isolated
tests, so there was no evidence to add them.

### Finding 2: workers don't know how to post to the shared blackboard

Both `claude`'s and `grok`'s transcripts show the SAME independent
failure, in two unrelated live runs: after producing a valid joke
quickly, each worker tried to record it on the swarm's shared
blackboard (a `kanban_comment` posted to the root task) and got stuck:

- `grok`: hand-wrote a raw `sqlite3 kanban.db "INSERT INTO
  task_comments ..."` shell command with an unescaped multi-line
  string, which failed with a bash syntax error. Retried by writing
  Python via `execute_code` to work around the shell-escaping bug --
  `execute_code` is BLOCKED outright for unattended/headless workers
  by design ("Cron jobs run without a user present to approve it").
  Tried `execute_code` again for a different sub-task shortly after,
  got blocked again, identically.
- `claude`: same pattern on its second (retry) attempt -- raw SQL via
  shell (syntax error), then `execute_code` (blocked).

Neither lane was actually tool-unfamiliar in general -- both had
already called `kanban_show`/`kanban_comment` correctly earlier in the
very same turn, against their OWN task. The confusion was specific to
"how do I write to the swarm's shared blackboard" -- the worker task
body (`hermes_cli/kanban_swarm.py::_swarm_context`) said only *"Put
cross-worker notes on the root task using structured comments"*,
naming the destination (the root task) but never the tool
(`kanban_comment`) or the exact call shape. Both lanes, independently,
filled that gap by reaching for raw SQL/Python instead of the tool
they'd already used successfully minutes earlier.

**Fix**: `_swarm_context()`'s generated worker-task body now says, in
addition to the correct destination task id (unchanged): *"To post
cross-worker notes on the shared blackboard, call the `kanban_comment`
tool with `task_id="<root_id>"` and your note as `body`. Do NOT write
directly to kanban.db via shell/sqlite3 or execute_code..."* -- naming
the tool, the exact argument shape, and explicitly ruling out both
failure modes actually observed.

## What this means for the earlier ticket's conclusion

`2026-08-20-swarm-claude-grok-lane-timeout-recurrence-001.md`'s
"external-CLI lanes need structurally more steps" framing is not wrong
-- it's incomplete. Some of those "extra steps" are genuine necessary
work (subprocess spawn, `cd`/path handling, output polling -- the agy
ticket's transcript is a real example). But at least two of the extra
steps/delays observed were themselves bugs with concrete fixes: a
security-scanner false positive adding ~88s of pure overhead, and a
missing tool-usage instruction causing repeated blocked-and-retried
dead ends. Fixing both should meaningfully reduce how often the (now
600s) ceiling is needed at all, on top of PR #94's ceiling raise
already preventing the old, much tighter 300s ceiling from killing
otherwise-successful runs.

## Verification

- `~/.hermes/bin/tirith check --json --non-interactive --shell posix
  -- "<exact failing command>"` reproduces the finding deterministically
  across multiple runs, confirming the evidence is always exactly two
  `U+3002` entries.
- New tests: `tests/tools/test_tirith_security.py` (10 new tests --
  `TestCjkFullStopConfusableSuppression`,
  `TestIsCjkFullStopOnlyConfusableFinding`) and
  `tests/hermes_cli/test_kanban_swarm.py` (1 new test locking in the
  `kanban_comment` instruction text). 608 tests pass across
  `test_tirith_security.py`, `test_kanban_swarm.py`,
  `test_kanban_cli.py`, `test_tools/test_kanban_tools.py`, and
  `tests/tools/test_approval.py` (the last confirming no regression in
  the broader approval pipeline this change sits inside).

## Process notes

- This ticket's evidence came from the same technique as the agy
  fabrication investigation: read the full persisted turn-by-turn
  transcript from `~/.hermes/state.db`'s `messages` table directly,
  not just the dispatcher's own `task_events` timestamps or a worker's
  self-reported summary.
- Both fixes are narrow and evidence-based, following this repo's
  existing suppression precedent (`.app` TLD) rather than broad
  heuristic loosening. Needs cross-review before merge, per this
  effort's established practice, especially for the Tirith change
  since it touches a security control.
