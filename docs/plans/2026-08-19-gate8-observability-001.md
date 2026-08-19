# GATE8-OBSERVABILITY-001 — one-shot CLI runs are not logged, which is why the gate 8 diagnostic could not finish

Status: proposed. Not implemented. Prerequisite for
[GATE8-ISOLATION-RESULT-001](2026-08-19-gate8-isolation-result-001.md)'s stated
next step.

## Why this is the next ticket rather than a gate 8 fix

The isolation diagnostic established that the gate 8 failure is not
Telegram-specific: on the CLI the agent used `terminal` successfully, then
ended the turn after announcing the decisive command, and created nothing.
That record named the single most informative missing datum — the turn-end
reason — and could not obtain it. It recorded the absence as a confound.

The absence has a cause, and it is not log rotation.

## Established

A one-shot run logs nothing at turn level. Measured directly:

```
Turn ended count in agent.log:  144  ->  144
```

across a `hermes -z` invocation that completed successfully and returned its
answer. `agent.log` covered 09:14–21:09 that day, so the earlier isolation run
at 20:19–20:25 was inside the retained window; its plugin registration lines
are present and no `conversation turn` or `Turn ended` line follows them.
Cron sessions in the same window logged normally.

## Cause

`hermes_cli/oneshot.py`:

```python
    # Silence every stdlib logger for the duration.  AIAgent, tools, and
    # provider adapters all log to stderr through the root logger; file
    # handlers added by setup_logging() keep working (they're attached to
    # the root logger's handler list, not affected by level), but no
    # bytes reach the terminal.
    logging.disable(logging.CRITICAL)
```

The comment states the file handlers keep working. They do not.
`logging.disable(level)` is a module-global threshold checked in
`Logger.isEnabledFor`, before any handler is consulted — it is not a logger
level and handler attachment is irrelevant to it. Confirmed in isolation: with
a handler attached and the root level at INFO, an `info()` call under
`logging.disable(CRITICAL)` delivers **0 bytes** to that handler.

So the one-shot path silences the file log as well as the terminal, which is
the opposite of what the comment claims and, judging by that comment, the
opposite of what was intended.

**The obvious alternative was checked rather than assumed.** A reviewer raised
it: the measurement would look identical if the one-shot path simply never
reached the code that logs `Turn ended`, and in that case this fix would
change nothing. It does reach it — `hermes_cli/oneshot.py:425` calls
`agent.run_conversation(prompt)`, and `Turn ended` is emitted from
`turn_finalizer.py` under the `agent.conversation_loop` logger during that
call. Same path, suppressed record.

This is the same shape as the other defects recorded today: a comment
asserting behaviour the code does not have, with nothing testing the claim.

## Proposed fix

Suppress the terminal, not the record. Raise the stderr handler's level to
above `CRITICAL`, or detach it for the duration, and leave the file handlers
alone. That matches the stated intent of the existing comment rather than
changing it.

The level matters and should not be left vague: `logging.disable(CRITICAL)`
today suppresses everything, so anything short of above-`CRITICAL` on the
stderr handler would newly leak `WARNING`/`ERROR` text to the terminal — a
behaviour change beyond restoring the file log, and one that would show up in
piped output.

**Not yet decided:**

- Whether to adjust the stderr handler's level or detach it. Detaching is
  cleaner to reason about; adjusting is easier to restore on exit paths that
  raise.
- Whether `--usage-file` consumers depend on the current total silence. The
  usage report is written separately, so probably not, but this was not
  checked.
- Whether any other entry point calls `logging.disable`. Not surveyed.
- **How the fix is verified.** Rerun the same probe: invoke one-shot and
  confirm the `Turn ended` count in `agent.log` increments. The mechanism
  above is evidenced, but "the diagnosis was complete" is exactly the kind of
  assumption that should be measured rather than trusted — the fix exists to
  produce a datum, so shipping it without confirming the datum appears would
  repeat the failure this ticket documents.

## What this does and does not do

It does **not** fix gate 8. It makes the next diagnostic capable of producing
the datum the last one could not, which is the difference between repeating
the experiment and repeating it usefully.

## Related, and deliberately not bundled

The failure shape — a turn that announces an action and then ends without
taking it — is not covered by the tool-outcome footer shipped for
`FABRICATED-TOOL-SUCCESS-001`. That guard fires when tool calls fail; here no
call failed, so it stayed silent, correctly. Whether that shape should be
detectable at all belongs with option A2 in
[FABRICATION-REMEDY-001](2026-08-19-fabrication-remedy-001.md), which is
recorded there as not currently specifiable.
