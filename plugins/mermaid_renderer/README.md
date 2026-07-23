# Mermaid Renderer Plugin

`mermaid_renderer` is an offline Hermes plugin that renders bounded Mermaid text
to a PNG and returns it through the existing Telegram `MEDIA:` delivery path.

## Runtime contract

- Tool: `render_mermaid(source, width?, height?)`.
- Output: one final-root PNG directive and `status=rendered` on success.
- Per task: only the first successful render creates a PNG. Later calls in the
  same task return `status=skipped` without `MEDIA:`. Request another version
  in a new Telegram message.
- The task gate is process-local. It uses an in-memory HMAC key, a 24-hour
  monotonic TTL, and a 4096-task bound. A gateway restart resets this gate.

## Security boundaries

- Mermaid uses `securityLevel: "strict"`; callers cannot override renderer
  configuration, Chromium arguments, output paths, or HTML.
- The local HTML uses a strict CSP and loads the pinned local Mermaid asset;
  it uses no CDN, HTTP server, or network access.
- Chromium writes only to the Snap-accessible staging root. Hermes validates
  the PNG with Pillow, checks containment and same-device atomic move, then
  publishes to `/home/cwliao/.hermes/media/mermaid-renderer/`.
- The renderer rejects executable HTML, links, callbacks, external resources,
  unsafe paths, invalid PNGs, and empty white renders.

## Operator commands

```bash
./venv/bin/hermes mermaid-renderer status
./venv/bin/hermes mermaid-renderer cleanup
./venv/bin/hermes mermaid-renderer cleanup --apply
```

Artifacts are retained for 24 hours. `cleanup` is dry-run by default.
`--apply` is a deliberate operator action and is not invoked by the gateway,
Telegram, tool calls, or cron.

## Verification

```bash
./venv/bin/pytest -q \
  plugins/mermaid_renderer/tests/test_renderer.py \
  plugins/mermaid_renderer/tests/test_plugin.py \
  plugins/mermaid_renderer/tests/test_artifacts.py \
  tests/gateway/test_media_extraction.py
```

Before a Telegram smoke test, verify the plugin/tool opt-in and runtime health:

```bash
./venv/bin/hermes plugins list --plain
./venv/bin/hermes tools list --platform telegram
systemctl --user is-active hermes-gateway.service
./venv/bin/hermes cron status
```

## Rollback

```bash
./venv/bin/hermes tools disable --platform telegram mermaid_renderer
./venv/bin/hermes plugins disable mermaid_renderer
systemctl --user restart hermes-gateway.service
```

Do not delete private config, sessions, credentials, or existing media
artifacts as part of plugin rollback.
