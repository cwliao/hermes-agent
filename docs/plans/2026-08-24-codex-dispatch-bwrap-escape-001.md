# CODEX-DISPATCH-BWRAP-ESCAPE-001

Status: DEPLOYED_SMOKE_VERIFIED
Date: 2026-08-24
Type: ticket
Target repo: hermes-agent
Priority: P1

## Incident

Codex CLI dispatches on the DGX had failed repeatedly since mid-July with:

bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted

bwrap: setting up uid map: Permission denied

The failure occurs before authentication or model execution. Codex exec with
workspace-write asks bubblewrap to create user/network namespaces that the host
container policy denies. Repeating the same command cannot repair an
environment-level namespace denial.

The live Hermes external_cli configuration also referenced a stale Codex binary
path, /home/cwliao/.hermes-coding-cli-tools/bin/codex, while the installed
binary is /home/cwliao/.local/bin/codex.

## Fix

- Keep the general codex_sandbox: workspace-write default for ordinary
  installations.
- Configure the DGX external Codex pool with codex_sandbox: danger-full-access,
  which avoids the failing bwrap sandbox path.
- Pin the live binary to /home/cwliao/.local/bin/codex.
- Add a profile-scoped flock single-flight lock at .codex-dispatch.lock, so the
  pool has one active Codex process at a time.
- Preserve allowed_roots and the private profile_home gate; the bypass is not a
  reason to broaden the Telegram working-directory allowlist.
- The Hermes Codex App-Server runtime remains available for native
  model.openai_runtime: codex_app_server turns, but this ticket fixes the
  existing external /codex dispatch bridge without changing that separate
  runtime.

## Acceptance criteria

- [x] Codex bridge serializes external Codex turns per profile.
- [x] Regression test covers lock creation.
- [x] Live config uses the installed Codex binary and danger-full-access.
- [x] A DGX smoke task reached Codex model execution without bwrap namespace
      errors.
- [ ] Claude external-cli behavior remains unchanged.

## Verification evidence

- Live profile and workspace were created with mode 700 because the prior
  configured paths did not exist.
- Codex 0.149.1 ran with danger-full-access and executed /bin/bash -lc true.
- The model returned SMOKE_OK.
- No bwrap namespace error occurred. Separate Codex model-refresh and Apps MCP
  timeout warnings were observed after the turn and did not prevent completion.
