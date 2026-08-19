---
title: "AUTH-EXPIRY-GUARD-001 — OAuth expiry is a recurring outage, and the tool meant to detect it reports false negatives"
status: DESIGN_ONLY_NOT_IMPLEMENTED
date: 2026-08-19
type: ticket-design
ticket: AUTH-EXPIRY-GUARD-001
target_repo: hermes-agent
related_tickets:
  - UNIT-FAILURE-BLINDNESS-001 (split out of it at reviewers' request)
base: 92b74b9fcb230d4459b7d0567850a930af3d1e7b
---

# AUTH-EXPIRY-GUARD-001

Design and decision only. No implementation is authorized by this document.

## Why this is separate

This was originally proposed direction #4 plus a secondary finding inside
`UNIT-FAILURE-BLINDNESS-001`. Both reviewers flagged it as scope creep —
that ticket is about systemd failure-state observability, and credential
lifecycle is a different subject with a different remedy and different
owners. The parent ticket had itself noted the distinction and then kept the
material anyway. It is split out here.

## Two separate problems, related by subject

### Problem 1 — OAuth expiry is a recurring outage, not a one-off

The shared `google-workspace` skill credential at
`~/.hermes/google_token.json` was revoked or expired, and every consumer
failed until a human re-consented:

```
google.auth.exceptions.RefreshError:
  ('invalid_grant: Token has been expired or revoked.', ...)
  at ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py:240
```

Known consumers observed failing on this specific cause:

- `hermes-drive-watch.service` — zero successes from at least 2026-08-14
  01:00 until re-consent on 2026-08-19 14:17.
- `kmdaily-daily-report.service` (KMDaily project) — same cause, resolved as
  a side effect of the same re-consent, confirmed by its owner with a real
  run (`exit=0`).

This has happened before, and was closed:

```
$ grep -n T0094 ~/project/klib/.ai/plan.md
107:| T0094 | 修 Google Workspace skill 共用憑證 `invalid_grant`（擋 T0037 Drive-watch） | **DONE（2026...
```

Same credential, same failure, same blocked consumer — marked DONE and
recurring anyway. That is the strongest argument in this ticket: the
previous fix was a re-consent, which resolves an instance and does nothing
about recurrence. A remedy that is itself a manual re-consent will land in
the same place.

The credential is **shared**, so a single expiry takes out every dependent
subsystem at once, across project boundaries — the two confirmed consumers
above live in different repositories with different owners.

Nothing warns before it happens. The failure mode is: token silently stops
working, every consumer starts failing, and the first signal is whatever
notices the downstream breakage — which in this case was nothing, for five
days (see the parent ticket).

### Problem 2 — the tool meant to detect this reports false negatives

`setup.py --check-live` exists to catch exactly this class of problem with a
real API call. On this host it fails for a reason unrelated to
authentication:

```
$ setup.py --check-live
LIVE_CHECK_FAILED: <HttpError 403 ...
  "Google Calendar API has not been used in project 731497879976 before or
   it is disabled."
```

The token was valid and Drive worked, verified independently:

```
$ python -c "from google_api import build_service; \
    print(build_service('drive','v3').files().list(pageSize=1).execute())"
-> 1 file returned
```

The live check probes Calendar. Calendar is not enabled in Google project
`731497879976`. So for any deployment that uses Drive, Gmail, Docs or Sheets
but not Calendar, this diagnostic reports authentication failure permanently
and unconditionally.

That is worse than having no check. A check that always fails trains its
readers to ignore it, and anyone triaging a real credential outage with this
tool is told "auth broken" whether or not it is — which is precisely the
situation it was built to disambiguate. During this incident it produced
exactly that confusion: it reported failure immediately after a successful
re-consent.

## Proposed direction (for review, not authorized)

1. **Make the live check probe an API the deployment actually uses.** Either
   derive the probe from the granted scopes in the stored token, or let the
   caller specify. The token records its scopes; Calendar being among them
   does not mean the API is enabled in the project.
2. **Distinguish "not authenticated" from "API not enabled" in the output.**
   These are different faults with different fixes — one needs re-consent,
   the other needs a Google Console change. A 403 `accessNotConfigured` is
   not an auth failure and should not be reported as one.
3. **Pre-warn on approaching expiry.** A periodic check that validates the
   credential *before* a consumer needs it converts an outage into a notice.
   This is the piece that would have prevented the five-day gap regardless
   of whether anything was watching unit failures.
4. **Record who depends on the shared credential.** Two consumers were found
   by following failures; there is no inventory. A single expiry affecting
   multiple repositories with different owners is a coordination problem as
   much as a technical one, and today it was resolved only because two
   agents happened to be talking.

## Open questions

- Is a shared credential the right design at all? The blast radius of one
  expiry currently crosses project boundaries. Splitting it per-consumer
  trades blast radius for more re-consent events, which is a real cost given
  each requires a human with a browser.
- What is the actual expiry behaviour — a fixed lifetime, an inactivity
  timeout, or a revocation triggered by something else? "Expired or revoked"
  is what Google returns for both, and this ticket does not establish which
  occurred. That matters for whether a pre-warning is even possible: a
  timed expiry can be predicted, a revocation cannot.
- Which other consumers exist? No inventory was taken. The two named above
  were found by following observed failures, not by enumeration, so the set
  is a lower bound.
- Does KMDaily's tooling share the Calendar-probing pattern? Its owner said
  they would check before trusting any future "auth broken" report from it;
  the answer is not recorded here.

## Evidence boundary

The Google project ID appears in an API error message and is non-sensitive.
No tokens, authorization codes, refresh tokens, or credential file contents
are reproduced. The authorization code used during the 2026-08-19 re-consent
was single-use and is not recorded anywhere in this repository.
