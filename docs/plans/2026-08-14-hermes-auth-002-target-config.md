---
title: "HERMES-AUTH-002: parameterize public DGX target metadata"
status: REVISE
date: 2026-08-14
type: security-hardening
ticket: HERMES-AUTH-002
target_repo: hermes-agent
---

# HERMES-AUTH-002: parameterize public DGX target metadata

## Problem

The public repository currently contains the operational DGX SSH host and
username in the wrapper scripts and handover material. They are not secrets,
but publishing a real host/user pair creates unnecessary reconnaissance
exposure and makes the wrapper target-specific.

## Goal

Move the DGX SSH host and user to user-owned Hermes configuration outside the
repository while preserving the existing fail-closed recovery contract.

The implementation must use the existing `~/.hermes/config.yaml` behavioral
configuration path rather than adding new user-facing `HERMES_*` environment
variables. Missing or malformed target configuration must fail closed with a
machine-detectable configuration error; it must never guess a host, accept a
host key, or fall back to a different agent.

The first independent review pass on 2026-08-15 reached a `REVISE` consensus
between the authenticated DGX Claude and WSL AGY reviewers. The correction set
below is part of this ticket contract and must be re-reviewed before any
implementation, merge, or deployment gate can advance.

## Scope

- Add a documented config shape for the DGX SSH target under the existing
  Hermes config system.
- Define the canonical user-owned YAML shape as:

  ```yaml
  dgx_ssh:
    host: "<configured-hostname-or-ipv4>"
    user: "<configured-ssh-user>"
  ```

  `HERMES_HOME` remains the existing optional home selector; it is not a new
  ticket-specific environment variable. WSL resolves
  `${HERMES_HOME:-$HOME/.hermes}/config.yaml`; native Windows resolves
  `${HERMES_HOME:-$env:USERPROFILE/.hermes}/config.yaml`. There is no fallback
  between these paths and no baked-in target.
- Add one small shared resolver (`scripts/dgx_target.py`) used by both
  wrappers. It must read the raw user config with the repository's YAML
  loader, distinguish missing/unreadable/malformed/non-mapping/partial target
  data, validate the two scalar values, and emit only a validated
  `user@host` result. It must not use `load_config()`'s defaults or
  last-known-good fallback for this security boundary. If the resolver or its
  Python runtime is unavailable, the wrapper returns the same configuration
  error and does not attempt SSH.
- Update both `scripts/dgx_ssh.sh` and `scripts/dgx_ssh.ps1` to resolve the
  same configured user/host and preserve identity, strict host-key, timeout,
  auth classification, and remote-exit-status behavior.
- Standardize configuration failures as stderr prefix `CONFIG_ERROR:` and
  exit status `78` (`EX_CONFIG`) on both wrappers. This includes missing,
  unreadable, malformed, non-mapping, empty, partial, or invalid target
  values. Host values are hostname/IPv4 only and user values are restricted to
  safe SSH username characters; neither may contain whitespace, `@`, `/`,
  `:`, shell metacharacters, or a leading `-`.
- Replace real host/user literals in public operational documentation with
  placeholders or configuration references where they are not required as
  historical evidence. This includes active runtime sections in
  `docs/HANDOVER.md` and `docs/ROADMAP-HERMES-DGX.md`, the recovery guide, the
  wrappers, and active tests. Preserve repository identity strings such as
  `github.com/cwliao/hermes-agent`; preserve completed AUTH-001 evidence only
  when it is explicitly labeled historical and non-operational.
- Add behavioral tests for configured targets, missing configuration,
  malformed configuration, non-mapping/empty/partial/invalid targets, resolver
  unavailability, and parity across WSL and native Windows paths. Tests must
  assert no network call occurs on configuration failure and must replace
  source-snapshot assertions about the old public target.

## Non-goals

- Do not store passwords, MFA values, private keys, tokens, or host keys.
- Do not weaken `StrictHostKeyChecking=yes`, add `sshpass`, or auto-accept
  unknown host keys.
- Do not change the DGX runtime, Telegram, systemd drop-ins, or release
  deployment as part of this ticket.

## Acceptance gates

1. Independent AGY and Claude review agree on the correction set.
2. Local behavioral tests cover valid, missing, and malformed target config on
  WSL and native Windows fallback paths without network access.
3. GitHub CI passes the wrapper Windows job and all required checks.
4. Public tracked source/scripts/docs contain no active hardcoded DGX target
  pair; historical evidence is either redacted or clearly non-operational.
5. Merge and deployment remain separate authorized gates.

## Reconciled review corrections

The independent reviewers agreed that the original plan was underspecified in
five material areas: the config schema and resolution bridge, native-Windows
parity, explicit fail-closed validation and exit semantics, behavioral test
coverage, and the active-versus-historical documentation boundary. The
resolver dependency is intentionally fail-closed: a missing Python/runtime or
resolver failure is `CONFIG_ERROR`/78, never an SSH attempt or authentication
classification. The implementation review must prove that this new failure
mode is documented and tested.

## Evidence boundary

The current DGX deployment is the separately merged Calendar Guard release
`v2026.8.15-hermes-calendar-guard-1b3d444955` from main SHA
`1b3d4449553433100038f38e7b58f2f2dc489fa7`. AUTH-002 must not change that
runtime as part of the plan-only review or implementation gate; it starts from
the current merged mainline and remains a separate ticket from AUTH-001 and
Calendar Guard.
