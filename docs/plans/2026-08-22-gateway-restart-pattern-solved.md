# The "old-PID reprints banner then status=1/FAILURE" restart pattern: solved

Status: solved, high confidence, two independent mechanisms, both intentional
existing behavior — no code change needed. Follow-up to
`docs/plans/2026-08-22-notify-sub-root-cause-lead-startup-self-reload.md`,
which left this pattern as an open question after ruling out the gateway's
on-boot systemd self-heal as its necessary cause. This document answers the
two "suggested next steps" #1 that document left open. It does **not** touch
the still-unsolved notify-subscription root cause — that remains genuinely
unknown, per the same document's other findings.

## The pattern, restated

On every genuine restart of `hermes-gateway.service` observed on
2026-08-21, journalctl shows, for the SAME numeric PID:

```
12:05:54 Stopping hermes-gateway.service...
12:05:54 python[3184]: WARNING gateway.run: Shutdown context: signal=SIGTERM ...
12:06:04 python[3184]: ⚕ Hermes Gateway Starting...          <- looks like a restart
12:06:14 systemd: Main process exited, code=exited, status=1/FAILURE
12:06:14 systemd: Started hermes-gateway.service...
```

Two separate questions were open: why does the OLD pid appear to reprint its
own startup banner ~10s after receiving SIGTERM, and why does that exit
carry `status=1/FAILURE`.

## Finding 1: the banner reprint is a 4-hour-old buffered `print()`, not a restart

**Confirmed by cross-referencing the log for PID 3184 across its whole
lifetime**, not just the shutdown window:

```
08:08:54.333  systemd: Started hermes-gateway.service...        (this PID's real birth)
08:09:03.214  python[3184]: WARNING [Telegram] Discovering...   (first adapter log)
   ... nothing on stdout for the next ~4 hours ...
12:06:04.922  python[3184]: ⚕ Hermes Gateway Starting...         (the "reprint")
12:06:14.290  systemd: Main process exited, status=1/FAILURE
```

`run_gateway()` (`hermes_cli/gateway.py:4780-4786`) prints the banner box via
plain `print()` to `sys.stdout` **once**, at the very top of the function,
before any adapter connects. For this PID that print() call actually
executed at real process start, ~08:08:5x, several seconds before its first
adapter log line at 08:09:03. It did not print again at 12:06 — the journal
entry at 12:06:04 is that SAME original write, finally reaching the journal
almost 4 hours late.

**Why the delay**: CPython's `sys.stdout`, when the underlying fd is not a
tty (true here — systemd captures it as a pipe into the journal), defaults
to full block buffering, not line buffering. Nothing in this codebase
overrides that default for stdout — confirmed no `PYTHONUNBUFFERED` is set
anywhere in the live unit or any of its systemd drop-ins on this host. By
contrast, `logger.warning(...)` calls (like "Shutdown context: SIGTERM",
via the `logging.StreamHandler` built at `gateway/run.py:22162-22165`) are
flushed to the journal immediately, because CPython's `sys.stderr` is
unbuffered/write-through by default regardless of tty — independent of
`hermes_logging.py:99-120`'s `_safe_stderr()` wrapper, which only kicks in
when `sys.stderr.encoding` is not already UTF-8 (a Windows legacy-codec
fallback); on this host `sys.stderr.encoding == "utf-8"`, so that wrapper
returns the raw stream unmodified and plays no part here. So every
`logger.warning(...)` call reaches the journal immediately, while the one
`print()` banner on stdout sits in an in-process C buffer indefinitely,
because the banner is short and nothing else writes to stdout afterward to
fill and auto-flush that buffer.

The buffer is finally drained by an **explicit, deliberate flush** at
shutdown: `gateway/run.py:22612-22648`'s `_exit_after_graceful_shutdown()`
does

```python
for stream in (sys.stdout, sys.stderr):
    try:
        stream.flush()
    except Exception:
        pass
```

right before calling `os._exit()` (chosen over `sys.exit()` specifically to
skip `Py_FinalizeEx`'s non-daemon-thread join, per that function's own
docstring, #53107). That `stream.flush()` call is what pushes the
4-hour-old banner bytes into the journal, timestamped at flush time —
which lands seconds before the process's real exit, right where "a second
startup" would appear if you didn't check the PID's own history.

**Confidence: very high.** The evidence isn't circumstantial — the same PID's
own first-ever log line (08:09:03) postdates the banner's necessary print()
call (which happens synchronously before that adapter connect code runs),
and the only place stdout is deliberately flushed on this path is the
shutdown backstop. No code change is suggested: this is harmless (the
banner text is cosmetic), and forcing `PYTHONUNBUFFERED=1` or wrapping
stdout with line buffering would only cosmetically move where in the log
timeline the banner appears — worth doing only if the confusing timestamp
itself is judged worth fixing, not because anything is actually broken.

## Finding 2: `status=1/FAILURE` is intentional, documented behavior — not a crash

`gateway/run.py:22540-22545`, inside `start_gateway()`, right after normal
shutdown teardown (cron/housekeeping stopped, MCP connections closed):

```python
if _signal_initiated_shutdown and not runner._restart_requested:
    logger.info(
        "Exiting with code 1 (signal-initiated shutdown without restart "
        "request) so systemd Restart=on-failure can revive the gateway."
    )
    return False  # → sys.exit(1) in the caller
```

`_signal_initiated_shutdown` is set True by the SIGTERM handler
(`gateway/run.py:22261-22268`) specifically on the branch that is **neither**
a planned `--replace` takeover **nor** a planned `hermes gateway stop`
(those two write a marker file first and are detected via `planned_takeover`
/ `planned_stop`, landing in different branches at lines 22250-22259 that
never set this flag). A bare `systemctl --user restart hermes-gateway.service`
sends SIGTERM directly via systemd's own stop transaction, with no
`hermes`-level planned-stop marker ever written, so from inside the
process that SIGTERM is indistinguishable from an unexpected external kill,
`_signal_initiated_shutdown` is True, `runner._restart_requested` is False,
and the code deliberately exits 1 by design — logging exactly that
reasoning to `agent.log` one line before it happens (the `logger.info(...)`
right above the `return False`).

Note this is *not* usually what `hermes gateway restart` itself does:
`systemd_restart()` (`hermes_cli/gateway.py:3286`) first tries a graceful
SIGUSR1-based restart (a genuinely planned path, exiting with code 75, kept
out of this branch entirely), and only falls back to a plain
`systemctl restart` — the bare-SIGTERM path this section describes — if
that graceful attempt doesn't complete in time. So the pattern's usual
real-world trigger on this host is more likely a bare `systemctl restart`,
an external kill, or a container/OOM signal than the everyday
`hermes gateway restart` CLI command, which mostly takes the graceful
exit-75 route instead.

This is intentional, working as designed: the comment block right above it
(`gateway/run.py:22532-22539`) explains the goal — covering `hermes update`
killing the gateway mid-work, external kills, and container/WSL2 signal
quirks, where the process wants systemd's `Restart=` to definitely fire.
`RestartForceExitStatus=75` and `Restart=always` in the unit file mean this
exit code isn't strictly required for the restart to happen — `Restart=always`
would revive it on any exit code — but the code doesn't know that; it's
written defensively assuming the more conservative `Restart=on-failure`.
The `status=1/FAILURE` line in journalctl is systemd faithfully reporting
that exit code, and reads alarming, but every single occurrence checked
this session (8/8 genuine restarts) is this exact designed path, not a
crash.

## Bottom line

Neither half of the pattern is a bug:

1. The "reprint" is a ~4-hour-stale buffered `print()` on stdout, released
   by an intentional flush during the fast-exit shutdown backstop.
2. The `status=1/FAILURE` is a deliberate `return False` /
   `sys.exit(1)` whenever a SIGTERM arrives without a recognized
   `hermes`-level planned-stop/restart marker — which is exactly what a
   plain `systemctl restart` produces, since it never goes through
   `hermes gateway stop`'s marker-file path.

This closes the restart-pattern side of
`docs/plans/2026-08-22-notify-sub-root-cause-lead-startup-self-reload.md`
(its "Suggested next steps" #1). Its notify-subscription connection
(orphan-dispatcher WAL race, `_dispatch_tick_lock`) is **unaffected by this
finding either way** — that theory was already framed as speculative on two
levels independent of the restart-banner mystery, and remains exactly that
speculative. The notify-sub root cause itself is still not found.
