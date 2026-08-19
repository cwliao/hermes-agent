---
title: "UNIT-FAILURE-BLINDNESS-001 — a timer failed 120+ times over 5 days and nothing reads systemd's own failure state"
status: DESIGN_ONLY_NOT_IMPLEMENTED
date: 2026-08-19
type: ticket-design
ticket: UNIT-FAILURE-BLINDNESS-001
target_repo: hermes-agent
related_tickets:
  - PROD-DRIFT-DETECTION-001 (same failure class, different invariant)
  - AUTH-EXPIRY-GUARD-001 (split out of this ticket at reviewers' request)
base: 92b74b9fcb230d4459b7d0567850a930af3d1e7b
---

# UNIT-FAILURE-BLINDNESS-001

Design and decision only. No implementation is authorized by this document.

## The incident

`hermes-drive-watch.service` — the Google Drive → DocuBot ingestion path —
failed on every hourly run from at least 2026-08-14 01:00 until 2026-08-19
14:17. Zero successes in the entire journal retention window. Nothing
reported it. It was found only because a peer agent ran an unrelated health
sweep across three projects and read the journal by hand.

```
$ journalctl --user -u hermes-drive-watch.service --since 2026-08-14 | grep -c Succeeded
0
$ systemctl --user is-active hermes-drive-watch.service
failed
$ systemctl --user show hermes-drive-watch.service -p Result -p ExecMainStatus
Result=exit-code
ExecMainStatus=1
```

Earliest failure visible in the journal:

```
Aug 14 01:00:21 <host> systemd[2859]: hermes-drive-watch.service: Failed with result 'exit-code'.
```

That is the retention boundary, not necessarily the first failure — the true
start may be earlier.

### Root cause of the failures themselves

A revoked Google OAuth token, not a transport problem:

```
google.auth.exceptions.RefreshError:
  ('invalid_grant: Token has been expired or revoked.', ...)
  at ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py:240
```

The count of `invalid_grant` occurrences in today's journal was 24 when
measured before the fix, and reads 12 afterwards:

```
$ journalctl --user -u hermes-drive-watch.service --since today | grep -c invalid_grant
12        # after the 14:17 fix; the pre-fix reading was 24
```

The discrepancy is not an error in either reading — it is a rotating
journal being queried at two different times, and it is recorded here
precisely because an unqualified "24 times" would have been unreproducible
by anyone re-running the command later. Time-varying counts need their
measurement point stated or they become claims that cannot be checked.

The SSL timeouts visible at the tail of each log are produced by the retry
path *after* the refresh fails; reading the last line of a run gives the
wrong cause. The peer agent initially made exactly that error and corrected
it by looking at the error distribution rather than the tail.

Fixed 2026-08-19 14:17 by re-consent through the skill's existing headless
flow (`setup.py --auth-url` / `--auth-code`, localhost-redirect paste, no
browser or tunnel required on the host). Verified by a live Drive API call
and then by an actual service run:

```
Result=success   ExecMainStatus=0
"no new files"
```

**The credential fix is not what this ticket is about.** It is about the five
days.

## The actual defect: nothing reads systemd's failure state

The invariant "scheduled units should succeed" was never asserted anywhere,
so 120+ consecutive failures produced no signal.

```
$ grep -rlE "systemctl.*is-failed|--failed" ~/.hermes/scripts/ \
      ~/.hermes/hermes-agent/scripts/ ~/.hermes/cron/
(no matches)
```

Nothing on this host reads `systemctl is-failed`, `systemctl --failed`, or
`Result=`. systemd had already recorded the failure in a machine-readable
field, on every run, for five days. No code ever looked at it.

This is worse than the sibling incident in `PROD-DRIFT-DETECTION-001`, where
the missing invariant at least had to be invented ("the deployed release
contains the guard"). Here the invariant is pre-recorded by the init system
and needed only to be read.

### Monitoring that exists and why it did not cover this

`hermes-mcp-health-guard.timer` is active, which makes it easy to assume
something is watching. It is not watching this:

```
$ systemctl --user cat hermes-mcp-health-guard.service | grep Description
Description=Hermes MCP server health guard (klib connection watchdog)
```

It watches one klib MCP connection. Its `token` references are
`TELEGRAM_BOT_TOKEN`, used to send its own alerts — unrelated to Google
credentials. So the host has an alerting mechanism that works, wired to
exactly one subject.

Other active timers (`hermes-gateway-recovery`, `ollama-gpu-healthcheck`,
`kmdaily`, `klib-*`) are each scoped to their own subject. None is a
general failure watcher.

## Why this is the same class as PROD-DRIFT-DETECTION-001

That ticket names the class as: *an invariant everything depended on was
never asserted anywhere, so a silent state change had no surface on which to
appear.* This is a second instance with a sharper edge — the state change
was not even silent. systemd announced it 120+ times in a structured field.
The absence was of a reader, not of a signal.

Both tickets therefore point at the same remedy shape: assert the invariant
somewhere a machine checks, rather than relying on a person to look.

## Proposed direction (for review, not authorized)

1. **A general failed-unit watcher.** Periodically run
   `systemctl --user --failed` and alert on any non-empty result. This is
   the smallest possible fix and would have caught this on 2026-08-14 at
   01:00. It requires no per-service configuration and no list of things to
   watch, which is what makes it robust — a new unit is covered the moment
   it exists.
2. **Alert on repeated failure of a scheduled unit even when it self-clears.**
   A oneshot that fails and is retried next hour never reaches a persistent
   `failed` state in some configurations. Counting consecutive non-zero
   `ExecMainStatus` values catches that variant.
3. **Reuse the existing alert path.** `mcp_health_check.sh` already sends to
   Telegram via `TELEGRAM_BOT_TOKEN`/`TELEGRAM_HOME_CHANNEL`. The delivery
   mechanism works; only its subject is narrow. Widening it is cheaper than
   building a second one.
Not proposed: per-service bespoke health checks. That approach is what
produced a host with one watchdog covering one subject while everything else
went unobserved.

## Deliberately out of scope

Two credential-lifecycle findings surfaced during this investigation and are
**not** in this ticket: that OAuth expiry is a recurring cross-project
outage, and that `setup.py --check-live` reports false negatives by probing
an API the deployment does not use. Both reviewers flagged them as scope
creep — correctly, since this ticket had already noted the distinction and
kept the material anyway. They are now `AUTH-EXPIRY-GUARD-001`.

The boundary is: this ticket is about not reading failure state that already
exists. That one is about a credential failing and the tool meant to detect
it lying. Fixing either does not fix the other — a perfect credential
pre-warning still leaves 120+ unread `Result=exit-code` records, and a
failed-unit watcher still would not have told anyone the token was days from
expiring.

## Open questions

- Was the 2026-08-14 date the true first failure, or just the journal
  retention edge? Determining this needs archived journals or the Drive
  ingest destination's last successful write.
- ~~Are other units currently in a failed state that nobody has noticed?~~
  **Answered — and it is not one unit, it is four.** The census this ticket
  recommends was run:

  ```
  $ systemctl --user --failed --no-legend
  kmdaily-daily-report.service
  kmdaily-digest.service
  trend-mail-remote-auth-handoff.service
  xdg-desktop-portal-gtk.service
  xdg-desktop-portal.service
  ```

  Successes since 2026-08-14, from the journal:

  ```
  kmdaily-daily-report              0    first failure 08-14 08:00
  kmdaily-digest                    0    first failure 08-14 08:00
  trend-mail-remote-auth-handoff    0    first failure 08-14 10:31
  hermes-drive-watch                0    first failure 08-14 01:00  (fixed 08-19)
  ```

  Four scheduled units began failing on the same day and none was reported
  for five days.

  **The census returned five rows, not four.** The two not discussed above
  are excluded with a reason rather than dropped:

  ```
  $ systemctl --user show xdg-desktop-portal.service -p Result -p ExecMainStatus
  Result=timeout
  ExecMainStatus=15
  Aug 11 15:38:04  xdg-desktop-portal.service: start operation timed out. Terminating.
  ```

  `xdg-desktop-portal` and `xdg-desktop-portal-gtk` are desktop-session
  portal services that timed out on 2026-08-11 starting on a headless host
  with no desktop session. They are not scheduled work, predate the 08-14
  cluster, and are expected to fail here. They are named rather than
  silently omitted because a census that quietly discards rows is not a
  census — a reviewer caught exactly that omission in an earlier draft.

  Separately, counting units that *failed at some point today* rather than
  units currently in a failed state gives a wider set, since a oneshot that
  fails and is retried does not stay `failed`:

  ```
  $ journalctl --user --since today | grep -oE "[a-z0-9@.-]+\.service: Failed with result" | sort | uniq -c | sort -rn
      154 trend-mail-remote-auth-handoff.service
       53 trend-mail-auth-watch.service
       25 klib-drive-ingest.service
       15 hermes-drive-watch.service
  ```

  `trend-mail-auth-watch` and `klib-drive-ingest` appear only here and not in
  `--failed`, which is the distinction: both recover between runs, so a
  watcher polling `--failed` alone would miss them entirely. Both are real
  units (`systemctl --user list-unit-files` confirms), and both belong to
  other projects. This is the concrete case for proposed direction 2 — the
  `--failed` snapshot is necessary but not sufficient.

  **Their causes differ, and that is the point.** Verified rather than
  assumed — the three non-Hermes units belong to the KMDaily project and
  were diagnosed by its owner, not by this author:

  - `kmdaily-daily-report` — same revoked shared credential; fixed as a side
    effect of the same re-consent, confirmed by a real run (`exit=0`).
  - `trend-mail-remote-auth-handoff` — **not** a credential failure.
    `HANDOFF_EXPIRED` (exit 34): a consumed handoff package legitimately
    expired. Correct fail-closed behaviour.
  - `kmdaily-digest` — auth path confirmed working via `--dry-run`, which
    exercises it without sending mail.

  An earlier draft of the handover message grouped all three under the
  credential cause. That was wrong for `trend-mail-remote-auth-handoff`, and
  it is corrected here. The correction reinforces the ticket rather than
  weakening it: a general failed-unit watcher would have surfaced all four
  regardless of cause, which is precisely why it beats per-service checks
  that each assume a specific failure mode.
- Should the watcher cover system units as well as user units? This
  investigation looked only at `--user`.
- What is the acceptable alert volume? A watcher that fires on every
  transient failure will be muted, which reproduces the original problem in
  a different form.

## Evidence boundary

Unit names, timestamps, exit codes, and the Google project ID appearing in
an API error message are non-sensitive operational metadata. No tokens,
credentials, message bodies, or file contents are recorded. The OAuth
authorization code used during the fix was single-use and is not reproduced
here.
