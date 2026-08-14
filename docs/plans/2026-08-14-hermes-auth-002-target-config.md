---
title: "HERMES-AUTH-002: parameterize public DGX target metadata"
status: READY_FOR_REVIEW
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

## Scope

- Add a documented config shape for the DGX SSH target under the existing
  Hermes config system.
- Update both `scripts/dgx_ssh.sh` and `scripts/dgx_ssh.ps1` to resolve the
  same configured user/host and preserve identity, strict host-key, timeout,
  auth classification, and remote-exit-status behavior.
- Replace real host/user literals in public operational documentation with
  placeholders or configuration references where they are not required as
  historical evidence.
- Add behavioral tests for configured targets, missing configuration,
  malformed configuration, and parity across WSL and native Windows paths.

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

## Evidence boundary

The current AUTH-001 deployment remains on DGX release
`v2026.8.14-hermes-auth-001-63bcd7ac` and is not changed by this plan. This
ticket starts from merged main SHA `63bcd7acbbb93d2c797090800ac1e4677b590449`.
