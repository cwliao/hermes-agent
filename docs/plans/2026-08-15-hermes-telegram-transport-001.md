---
title: "HERMES-TELEGRAM-TRANSPORT-001: restore Telegram polling health"
status: READY_FOR_REVIEW
date: 2026-08-15
type: reliability
ticket: HERMES-TELEGRAM-TRANSPORT-001
target_repo: hermes-agent
---

# HERMES-TELEGRAM-TRANSPORT-001: restore Telegram polling health

## Problem

The Hermes gateway process is running on DGX, but process health is not
equivalent to Telegram inbound readiness. The latest bounded DGX observation
showed `hermes-gateway.service` active with MainPID `3161529`, exit status `0`,
and `NRestarts=0`, while recent gateway logs reported:

- Telegram polling `TimedOut` during reconnect, with retry attempt `5/10`;
- repeated polling reconnect warnings;
- outbound delivery evidence exists, but inbound polling is not proven healthy.

This is a transport failure class, not an ARCH-002 runtime-state-only fix. The
repository already contains timeout and reconnect regression tests, so this
ticket must first reconcile the observed live behavior with the current code
and deployed release. It must not blindly duplicate an earlier fix.

## Goal

Restore and prove Telegram inbound polling health after transient or sustained
network/API timeout without reporting a merely running gateway process as
healthy. Preserve outbound send behavior, authorization, update ordering, and
the existing fail-closed safety boundaries.

## Scope

- Reproduce the observed polling timeout/reconnect path using bounded,
  network-free tests and read-only DGX evidence. Identify the exact current
  code path in `plugins/platforms/telegram/adapter.py` and any shared Telegram
  transport helper before changing behavior.
- Define distinct states for service/process health, inbound polling health,
  and outbound send-path health. A systemd `active` result alone must not clear
  inbound degradation.
- Fix only the confirmed root cause in the Telegram polling transport. The
  correction must keep polling start/reconnect awaits bounded, prevent
  overlapping polling generations, preserve request-pool cleanup, and retain
  bounded retry/backoff/fatal escalation semantics.
- Ensure a successful reconnect is promoted to healthy only after observable
  polling progress or an equivalent bounded readiness signal, not merely after
  `start_polling()` returns.
- Add regression coverage for the exact failure class, including hung polling
  start, `TimedOut`/network error, reconnect retry scheduling, progress-based
  recovery, fatal escalation, cancellation/cleanup, and separation of inbound
  polling from outbound send health.
- Record CI, independent review, deployment, rollback, and live Telegram
  evidence as separate gates.

## Non-goals

- Do not change Telegram credentials, bot ownership, allowlists, or webhook
  secrets.
- Do not disable TLS, bypass Telegram API errors, suppress timeout warnings,
  or claim readiness from `systemctl is-active` alone.
- Do not add an unconditional service restart/watchdog before the root cause
  and safe recovery contract are reviewed.
- Do not modify `/home/cwliao/.hermes`, the live DGX checkout, systemd units,
  release snapshots, or Telegram state during planning or review.
- Do not fold ARCH-002 runtime-state schema work or HERMES-MONITORING-001
  alerting into this ticket. Monitoring may consume the health states after
  this transport contract is accepted.

## Acceptance criteria

1. The current degradation is reproduced or otherwise line-mapped to an exact
   code path with evidence; an existing regression test is not treated as
   proof that the deployed path is healthy.
2. Inbound polling, outbound send, and process/service health have explicit,
   non-colliding states and transitions. A timeout/reconnect loop is visibly
   degraded and a successful recovery requires bounded progress evidence.
3. Reconnect behavior is bounded and single-owner: no forever-hung await, no
   overlapping polling generations, no request-pool leak across retries, and
   no silent death after a transient failure.
4. Focused hermetic tests cover timeout, retry, progress, cancellation,
   cleanup, fatal escalation, and inbound/outbound health separation without
   real Telegram calls or credentials.
5. Relevant GitHub CI checks pass, and one authenticated Claude reviewer plus
   one authenticated AGY reviewer independently PASS the same correction set;
   findings are reconciled before implementation or deployment.
6. Deployment, service restart, live inbound Telegram verification, outbound
   verification, and rollback evidence remain separately authorized and
   recorded. A green unit/CI result is not live health evidence.

## Review questions

- Is this ticket correctly separated from ARCH-002 and HERMES-MONITORING-001?
- Does the scope target the observed Telegram polling degradation without
  assuming that an existing test already proves the deployed release?
- Are the proposed health boundaries and acceptance tests sufficient to avoid
  false `healthy` status while preserving safe recovery?
- Is any part of the scope speculative or missing a required failure path?

## Current gate

`READY_FOR_REVIEW` only. No implementation, merge, deployment, DGX mutation,
or Telegram credential/network action is authorized by this plan.
