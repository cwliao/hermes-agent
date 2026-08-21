# SWARM-AGY-HEADLESS-OAUTH-BLOCK-001

Status: **resolved, 2026-08-21.** Step 1 of the "Revised suggested next
steps" below has now been completed directly from the persisted tool-call
transcript (no log-verbosity change needed -- see below). Verdict: **the
worker's `kanban_block` reason was genuine, not fabricated.** The
"working hypothesis" below (fabricated self-diagnosis by `ornith:35b`)
is **ruled out**. See "Resolution" section at the end of this document.

Status (superseded): investigated (step 2 of the suggested next steps below). Finding
reframes the problem -- likely not an env/auth gap at all. Needs
cross-review of this finding before deciding what (if anything) to
implement.

## Follow-up finding (same day, later investigation)

Reproduced the worker's own claimed failure directly: ran `agy -p "Reply
with exactly: OK" < /dev/null` (headless, no TTY, matching how a
dispatcher-spawned worker runs) both plain and with `--sandbox` -- the
exact two invocations the worker's block comment said it tried -- from an
interactive shell first, then again with the worker's own env vars
(`HOME`, `HERMES_PROFILE=default`, `HERMES_KANBAN_TASK=t_727e2096`) and
cwd (`~/.hermes/kanban/boards/kanban/workspaces/t_727e2096`, the worker's
actual workspace directory, still present on disk). **All four
reproductions returned `OK` in under a second, using the existing stored
OAuth token (`~/.gemini/antigravity-cli/antigravity-oauth-token`), with no
browser-auth prompt, no hang, no timeout.**

This does not match "agy requires interactive OAuth that cannot complete
headless" at all -- that claim, as reported by the worker
(`t_727e2096`'s `blocked` event and its own 529-char comment), could not
be reproduced under conditions as close to the worker's actual execution
context as could be checked without re-running the live dispatcher.

**Working hypothesis, not yet confirmed:** the block reason may be a
fabricated/incorrect self-diagnosis by the worker's own model
(`ornith:35b`, a smaller local model, per `agent.log`'s
`[20260820_214527_170e5c]` session), not a genuine agy limitation. The
worker's `agent.log` session does show two real `process` tool calls
(45.38s and 48.81s durations) in the relevant window -- so *something*
ran and took a while -- but the log format doesn't expose the tool's
actual command-line arguments at INFO level, so what those two calls
literally invoked (and whether they were even `agy` calls, versus e.g. a
misdirected/malformed command) was not confirmed here. This is exactly
the class of problem `docs/plans/` history already has a named mechanism
for (`fabrication-guard`, PR #68) -- worth checking whether that mechanism
covers `kanban_block` reasons specifically, or only some other tool-result
surface.

**Ruled out as a separate confound:** this host has a chronic,
long-running (spans 2026-08-18 through at least 2026-08-21, 476
occurrences in `gateway.log`) pattern of `Primary api.telegram.org
connection failed ... trying fallback IPs` -- but that's specifically
Telegram's own network path, unrelated to `agy`/`claude`/`grok`'s
upstream APIs, and was already present continuously before and after this
test, not a spike coinciding with it. Noted so a future investigator
doesn't chase it as the cause here.

## Revised suggested next steps

1. **Before implementing anything**, get the exact `process` tool
   invocation(s) that session actually made -- either from a more verbose
   log level, or by adding temporary tracing to the `process` tool
   dispatch path -- to settle whether a real `agy` call was made and
   genuinely misbehaved (contradicting this session's clean
   reproduction, which would then need explaining) or whether the "OAuth
   timeout" narrative was invented without a matching real failure.
2. If confirmed as fabrication: this is a correctness/trust problem in
   worker self-reporting, not an agy/auth problem. Consider whether the
   agy lane's assigned model should be upgraded from `ornith:35b` to
   something more reliable for self-diagnosis, and/or whether
   `kanban_block`'s reason text should require some form of
   evidence-attachment (e.g. the actual failing command's exit code/
   stderr) rather than free-text narration, before a block is accepted.
3. If NOT fabrication (a real, narrower trigger condition exists that
   this session's reproduction didn't hit -- e.g. only fails under actual
   dispatcher-concurrency conditions, or only on the first call after
   token near-expiry), that would itself be a useful, more specific
   finding to chase next, but has no supporting evidence yet.

## Original suggested next steps (superseded by the above for step 2; steps 3/4 below still apply if fabrication is ruled out)

## Context

Same live four-lane swarm test as
`2026-08-20-swarm-claude-grok-lane-timeout-recurrence-001.md` (tenant
`fix-verify-v1`, root `t_ec46132f`, run to verify
`WORKER-SUBPROCESS-SESSION-ENV-001`, now merged as PR #84). The agy lane
(`t_727e2096`, `agy_lane:秋天俏皮話`, skill `antigravity-cli`) blocked with
`block_kind=needs_input`, not a timeout like the other two failed lanes --
a distinct failure mode, hence a separate ticket.

## What's confirmed

`task_events` `blocked` payload and the worker's own comment (posted by
`default`, 529 chars) both say the same thing:

> agy CLI requires interactive Google OAuth authentication (browser-based
> login) which cannot complete in background/headless execution mode.
> Tried: (1) `-p` non-interactive with credentials present, (2)
> `--sandbox` flag -- both timed out after 60s waiting for browser auth
> URL to be manually entered.

## This may or may not be the same issue central-brain already investigated and found NOT to be a permissions/auth gap

`hermes-agent`'s own git history has a prior investigation of an AGY
worker-lane `BLOCKED` state:
`docs/plans/HERMES-AGY-HEADLESS-PERMISSIONS-001.md` (2026-08-18, status
`DESIGN_ONLY_NOT_IMPLEMENTED`), which was explicitly walked back the next
day -- commit `d61eb8bcc3`, "docs(agy): root cause is a global skill, not
a permissions gap": running that ticket's actual agy-lane prompt directly
under `--print --output-format stream-json` invalidated the original
permissions-gap premise. **Today's block reason (explicit OAuth
browser-auth timeout) reads like a different, more literal symptom than
"a global skill config problem"** -- but this has not been reconciled
against that prior finding. It's possible (a) this is a genuinely new/
different failure than what d61eb8bcc3 diagnosed, (b) the earlier fix
regressed, or (c) both investigations are looking at the same underlying
gap from different angles and the "global skill" framing was incomplete.
**Not established which -- read the full prior ticket + its two follow-up
commits (`d942868cb2`, `d61eb8bcc3`) before assuming this is new.**

## A concrete, checkable lead: the OAuth token file's timing

`agy`'s stored OAuth token lives at
`~/.gemini/antigravity-cli/antigravity-oauth-token` (mode 0600, owned by
`cwliao`) and was independently confirmed present and working in an
**interactive** SSH session on this same host earlier in this multi-day
effort (`central-brain`'s `environments/55-0940189-03.linux.md` §7:
"this table already has a real, logged-in `agy` ... verified via `agy -p`
... device-code login flow, also supports `GEMINI_API_KEY` purely-
non-interactive mode"). If the token is genuinely valid and on disk,
a dispatcher-spawned worker subprocess should in principle be able to
read it too -- `_default_spawn` (`hermes_cli/kanban_db.py`) does
`env = dict(os.environ)` before its explicit overrides, so `HOME` is
inherited unless something later in the worker's own startup path
(profile activation, a sandboxed/restricted exec environment, a
different effective `$HOME` under `agy`'s own config resolution) points
`agy` at a different, tokenless location. **Not checked yet**: whether
the worker subprocess's actual resolved `HOME` (or whatever env var `agy`
itself uses to find its token file, if different from `$HOME`) matches
the interactive session's, and whether the specific worker process here
ever got far enough to attempt reading the token file at all versus
failing before that point.

## Suggested next steps

1. Read `HERMES-AGY-HEADLESS-PERMISSIONS-001.md` and its two follow-up
   commits in full before doing anything else -- avoid re-deriving
   already-established (or already-refuted) findings.
2. Reproduce directly: run the dispatcher's actual `agy` invocation
   command (whatever `--sandbox`/`-p` flags the worker skill assembles --
   check `optional-skills/devops/kanban-worker/SKILL.md` or wherever the
   agy lane's actual CLI invocation is templated) manually, in a shell
   with the *exact* env a dispatcher-spawned worker would have (same
   `HOME`, no TTY / `agy < /dev/null` to simulate headless), and see if it
   reproduces the OAuth prompt or successfully picks up the existing
   token.
3. If the existing token is genuinely inaccessible to the worker
   specifically (not to an interactive shell), that points at an
   environment/config-resolution gap in how the worker subprocess is
   spawned -- narrower and more actionable than "agy needs interactive
   OAuth," and would mean the fix is env-plumbing, not asking the user to
   re-authenticate.
4. If the existing token really is expired/invalid even interactively
   (check its actual expiry, not just its mtime -- the file was touched
   very recently, at 22:07, ~16 minutes before this ticket was written,
   which could mean it's actively being refreshed by something else, not
   necessarily proof of validity), then the fix is genuinely "the user
   needs to re-run agy's login flow," as the worker's own block comment
   already suggests, and central-brain's non-interactive `GEMINI_API_KEY`
   mode (already documented as supported) might be a better fit for
   headless dispatcher use than OAuth entirely.

## Process notes

- This ticket is evidence-gathering only; no code or config changed to
  produce it.
- Needs cross-review of whatever step 1-2 above establishes before any
  fix (env plumbing vs. re-auth vs. switching to API-key mode) is
  implemented, per this effort's established working rule.

## Resolution (2026-08-21)

Completed "Revised suggested next steps" #1 -- got the exact `process`/
`terminal` tool invocation(s) worker session `20260820_214527_170e5c`
actually made -- **without needing a log-verbosity change**. The full
tool-call arguments and results are already persisted per-turn in
`~/.hermes/state.db`'s `messages` table (`tool_calls` and `content`
columns), independent of what `agent.log` shows at INFO level:

```
sqlite3 ~/.hermes/state.db "SELECT id, role, timestamp, tool_name, content, tool_calls
FROM messages WHERE session_id='20260820_214527_170e5c' ORDER BY timestamp;"
```

**What the transcript shows, in order:**

1. First `agy` attempt failed on a wrong `cd` path (`~/.hermes/kanban/workspaces/t_727e2096`,
   missing the `boards/kanban/` segment the real workspace path needs) --
   a real, ordinary path mistake by the worker model. `agy` itself never
   ran; bash's `cd ... && agy ...` short-circuited on the failed `cd`.
2. Second attempt, corrected path, `agy --dangerously-skip-permissions -p
   '...' --print-timeout 15m`: **`agy` itself printed a real Google OAuth
   authorization URL** (`https://accounts.google.com/o/oauth2/auth?...`),
   then genuinely waited ("Waiting for authentication (timeout 60s)...")
   and exited with code 1 after that wait elapsed
   ("Error: authentication timed out."). Captured verbatim in the
   `process` tool's persisted result -- see the full excerpt below.
3. Third attempt, `--sandbox` variant: same real "Authentication
   required" response from `agy`, same OAuth URL, same 60s wait, same
   timeout, exit code 1.

**Verdict: NOT fabrication.** The worker's core claim -- that `agy`
required interactive OAuth and could not proceed headless -- is
genuine and directly reproduced from real command output in the
transcript, not invented by the worker's model (`ornith:35b`). The
`fabrication-guard` mechanism (PR #68) does not need extending to cover
this `kanban_block` reason, because there was nothing to catch here.

**Correction (caught by independent cross-review, not by the original
pass of this investigation):** an earlier draft of this section claimed
the worker's "timed out after 60s" framing was a minor inaccuracy,
reasoning that the transcript showed a clean exit rather than a hang.
That claim was itself wrong -- it was produced from a truncated read of
the persisted `content` column (`substr(content, 1, 500)`, which cut off
before the relevant text). The **full**, untruncated `content` for both
`process` tool results (message ids 5262 and 5290) ends with:

```
Waiting for authentication (timeout 60s)...
Or, paste the authorization code here and press Enter:
Error: authentication timed out.
Error: authentication failed or timed out
```

`agy` itself reports a genuine 60-second authentication wait that timed
out, on both attempts. **The worker's original `kanban_block` reason and
comment were accurate in every respect that could be checked** -- not
just "not fabricated" but not even mildly imprecise. This is recorded as
its own lesson: verify a transcript claim against the *full* field
content before drawing a conclusion from it, especially when quoting
that content as the basis for correcting someone else's (in this case, a
worker model's) self-report.

**Root cause, now separately confirmed:** the stored OAuth token
(`~/.gemini/antigravity-cli/antigravity-oauth-token`) was stale at the
time of this swarm test -- last written 2026-08-20 18:52, ~3 hours
before the worker's 21:47-21:52 run, and unchanged since. It has since
been refreshed (now dated 2026-08-21 07:51). Re-running the *exact* same
command, cwd, and headless (`< /dev/null`) invocation the worker used,
today, succeeds cleanly with no OAuth prompt:

```
$ cd ~/.hermes/kanban/boards/kanban/workspaces/t_727e2096
$ agy --dangerously-skip-permissions -p '用繁體中文創作一句關於「秋天」的簡短諧音梗或笑話，不超過30字。只回覆笑話本身。' --print-timeout 15m < /dev/null
為什麼秋天的落葉最懂時尚？因為很有「楓」格！
```

This also contradicts this document's own earlier "Follow-up finding"
section above (the "all four reproductions returned OK... using the
existing stored OAuth token" claim) -- that reproduction's success was
evidently dependent on token freshness at the time it was run, not proof
the worker's failure was impossible. **Lesson for future investigations
of this class:** a clean manual reproduction of a *different* invocation
at a *different* time does not refute a worker's specific, transcript-
backed failure at *its* specific time -- token/credential state is
time-dependent and must be checked (file mtime at minimum) before
treating a mismatch as evidence of fabrication.

**No code or config change needed.** This was a transient credential-
staleness condition, now resolved by an OAuth token refresh (whatever
refreshed it 2026-08-21 07:51 is outside this investigation's scope).
The `kanban_block`/`kanban_comment` free-text reason format was
sufficient to diagnose this after the fact once the persisted transcript
was consulted -- the "require evidence-attachment for kanban_block
reasons" idea from the original suggested next steps is not needed for
this incident, though it could still be a reasonable defensive
improvement for a future, harder-to-verify case.
