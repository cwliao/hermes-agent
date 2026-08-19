# WATCHER-RESTART-NOISE-001 — the failed-unit watcher counts restarts as failures

Status: proposed. Not implemented. Found by enabling the watcher on the host,
not by its 25 tests.

## Defect 1 — the flap layer cannot distinguish a restart from a failure

The watcher's second detection layer counts `Failed with result` lines in the
journal, because `--failed` is a point-in-time snapshot and misses units that
fail and recover between runs. That layer is why the watcher found units
nothing else was watching.

It also counts ordinary restarts. On its first real run it reported:

```
hermes-gateway.service (5 failures in today, currently recovered)
```

All five have this shape:

```
Stopping hermes-gateway.service...
hermes-gateway.service: Failed with result 'exit-code'.
Stopped hermes-gateway.service.
Started hermes-gateway.service.
```

That is the old instance exiting non-zero on `SIGTERM` during
`systemctl restart` — a normal shutdown. The service is healthy; it was
restarted five times during a deployment session. `Failed with result` is
emitted either way, and the grep sees only that string.

Consequence: a unit that is restarted often **and exits non-zero on
`SIGTERM`** produces a standing false alert. A unit that exits 0 on shutdown
emits no `Failed with result` line and is unaffected, so this is not every
restarted unit — but it is every one that does not handle `SIGTERM` cleanly,
which is a property of the service, not of its health. That is the
alert-fatigue failure the watcher's own design notes warn against,
reintroduced by its detection method.

**Candidate fix, not yet decided:** treat a `Failed with result` as a restart
when it is bracketed by `Stopping` and `Stopped` for the same unit, and count
only the unbracketed ones.

**This rule would suppress real failures, and the cases are concrete, not
hypothetical.** `Stopping -> Failed -> Stopped` is also the shape of an
`ExecStop=` that crashes, a stop that exceeds `TimeoutStopSec`, and a unit
crash-looping through restarts under `Restart=`. Bracketing on lifecycle
position alone would mask all three permanently — a silent watcher, which is
the exact condition this watcher exists to remove, so the wrong fix here is
worse than the noise it removes.

Whatever rule is chosen needs tests built from real journal text of each shape
captured from a host, because the distinguishing detail is which lines systemd
actually emits in each case, and that is what a hand-written fixture would be
guessing at.

Note the primary `--failed` layer is unaffected: the gateway is `active`, so
it never appeared there. Only the flap layer misreports.

## Defect 2 — the shipped unit points at a mutable dev checkout

`systemd/failed-unit-watch.service` as committed:

```ini
ExecStart=%h/.hermes/hermes-agent/scripts/failed_unit_watch.sh
```

That path is the development checkout. On this host it is currently broken in
a way that is worth recording: its `HEAD` is the merge commit that added the
script, `git ls-tree HEAD scripts/` lists `failed_unit_watch.sh`, and **the
file is not on disk**. The working tree is out of sync with `HEAD`. A unit
installed from the repo as committed would have failed to start.

This is the same shape as the 2026-08-19 klib incident, where a production
service pointed at a mutable checkout and broke when that directory changed.
The klib session's resolution was an immutable release plus a stable
`current` symlink.

When enabling the watcher on the host, `ExecStart` was pointed at
`~/.hermes/scripts/failed_unit_watch.sh` instead — the established location
for host scripts (`hermes_calendar_guard.sh`, `hermes_drive_watch.py`, and
others live there). **The host and the repo therefore disagree today.** The
repo unit still names the dev checkout.

**Not yet decided:** whether the committed unit should name
`~/.hermes/scripts/`, a release path, or a `current`-style symlink. The third
matches what klib settled on and survives redeploys; the first matches what
every other host script already does.

Whichever is chosen, this needs a deployment contract, which does not exist
today: something must state where the script is installed from, when it is
refreshed relative to a release, and what keeps the committed unit and the
installed unit in agreement. The divergence introduced on 2026-08-19 is
uncommitted and undocumented outside this ticket, which is precisely the
condition that makes the next reader trust the wrong file.

## What this says about the tests

The watcher has 25 tests, including negative controls, and they pass. Both
defects surfaced within one minute of enabling it on the host.

Neither is unreachable by testing — that would be too convenient a conclusion.
A multi-line journal fixture containing a real restart sequence would catch
Defect 1, and a test asserting the committed unit's `ExecStart` path exists in
the repo would catch Defect 2. What the existing suite lacks is not the
ability but the input: its journal fixtures are single lines written to
exercise the parser, and nothing asserts anything about the unit files at all.
The lesson is about which fixtures were chosen, not about a limit of
synthetic testing.

## Not in scope

No change to the `--failed` snapshot layer, the debounce logic, the delivery
path, or the exit-0 contract. Those behaved as designed on the first real run:
three genuinely failed units reported, Telegram delivery confirmed by the
state file being written.
