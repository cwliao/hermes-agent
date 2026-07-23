# Hermes Mermaid Renderer Handover

## Status

Mermaid PNG delivery is enabled for Telegram through the opt-in
`mermaid_renderer` plugin. The verified flow is:

```text
Telegram request -> render_mermaid -> local staged PNG -> verified atomic move
-> current-turn MEDIA append -> Telegram native photo
```

The latest acceptance sent one Mermaid request and received exactly one native
PNG photo. The prior multi-image album issue is fixed by the per-task gate:
only the first successful render returns `MEDIA:`; later calls in the same task
return a bounded skipped result with no attachment directive.

## Implementation boundary

- Plugin source and tests: `plugins/mermaid_renderer/`.
- Gateway change: `render_mermaid` is in the existing current-turn,
  producer-tool allowlist in `gateway/run.py`.
- Regression coverage: `tests/gateway/test_media_extraction.py`.
- Chromium path: `/snap/bin/chromium`; never use `--no-sandbox`.
- Staging root:
  `/home/cwliao/snap/chromium/common/hermes-mermaid-stage/`.
- Final media root:
  `/home/cwliao/.hermes/media/mermaid-renderer/`.
- Artifact retention is 24 hours and cleanup is operator-only dry-run unless
  `--apply` is explicitly supplied.

The implementation does not modify Telegram token/session, private Hermes
config, MCP, cron definitions, coding-cli, Claude CLI, Codex CLI, Gateway core
dispatch, or the Telegram adapter.

## Security and runtime contract

- Mermaid initialization is fixed to `securityLevel: "strict"`.
- Temporary `file://` HTML uses a strict CSP and local pinned Mermaid asset;
  it has no network, CDN, local HTTP server, or model-provided HTML options.
- PNG is signature-checked, decoded with Pillow, dimension/size checked, and
  moved only after staging/final same-filesystem verification.
- Render handler accepts registry-injected runtime kwargs. It uses only
  `task_id` internally, derives an HMAC key using an ephemeral process secret,
  and never returns, logs, or persists task/session/prompt content.
- Gate scope is one gateway process: restart or future multi-process deployment
  resets it. State is lock-protected, holds 4096 tasks, and expires completed
  entries after 24 hours; capacity saturation fails closed.

## Verification evidence

The focused suite passed after Gate 8:

```bash
./venv/bin/pytest -q \
  plugins/mermaid_renderer/tests/test_renderer.py \
  plugins/mermaid_renderer/tests/test_plugin.py \
  plugins/mermaid_renderer/tests/test_artifacts.py \
  tests/gateway/test_media_extraction.py
```

Expected result: `57 passed`.

The service was restarted after the Gate 8 change. At verification time:

- `mermaid_renderer` was enabled.
- Telegram `mermaid_renderer` toolset was enabled.
- `hermes-gateway.service` was `active`.
- Cron showed 7 active jobs.
- Staging root was empty after render completion.

## Safe next checks

```bash
cd /home/cwliao/.hermes/hermes-agent
./venv/bin/hermes plugins list --plain
./venv/bin/hermes tools list --platform telegram
systemctl --user is-active hermes-gateway.service
./venv/bin/hermes cron status
./venv/bin/hermes mermaid-renderer status
./venv/bin/hermes mermaid-renderer cleanup
```

For Telegram, send a single-diagram request and verify exactly one native PNG
photo is received. Do not run `cleanup --apply` without a separate explicit
operator authorization.

## Rollback

If Mermaid delivery must be disabled:

```bash
cd /home/cwliao/.hermes/hermes-agent
./venv/bin/hermes tools disable --platform telegram mermaid_renderer
./venv/bin/hermes plugins disable mermaid_renderer
systemctl --user restart hermes-gateway.service
```

Do not delete private config, session, credential, Telegram, MCP, cron, or
existing media files as part of rollback.
