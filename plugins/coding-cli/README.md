# coding-cli

Drive the real `codex` and `claude` CLIs from a Telegram (or any gateway)
chat via `/codex` and `/claude` slash commands — not routed through
Hermes' own LLM loop, just used as the messaging transport.

Off by default. Requires two things before it will run a prompt:

1. `hermes plugins enable coding-cli`
2. In `~/.hermes/config.yaml`:
   ```yaml
   external_cli:
     enabled: true
     allowed_roots:
       - /home/you/work/some-repo
   ```

## Usage

```
/codex <prompt>       Send a prompt (resumes this chat's codex session if one exists)
/codex dir <path>     Set the working directory for this chat (must be under allowed_roots)
/codex reset          Start a fresh codex session next time (keeps the dir)

/claude <prompt>       Same, for the claude CLI
/claude dir <path>
/claude reset
```

Each call is a single non-interactive turn (`codex exec [resume <id>]` /
`claude -p [--resume <id>]`) — not a live interactive terminal session.
Continuity across messages comes entirely from each CLI's own
session/thread id, persisted per chat at
`$HERMES_HOME/coding-cli-sessions.json`.

## Config (`external_cli:` in config.yaml)

| Key | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Master switch. Prompts fail closed until true. |
| `allowed_roots` | `[]` | Directories `/codex dir` / `/claude dir` may point at (and any subdirectory). Empty = feature can't run. |
| `timeout_seconds` | `180` | Per-turn subprocess timeout. |
| `codex_bin` / `claude_bin` | `"codex"` / `"claude"` | Resolved via `PATH` at call time. |
| `codex_sandbox` | `"workspace-write"` | Passed as codex's own `--sandbox` flag. |
| `claude_permission_mode` | `"acceptEdits"` | Passed as claude's own `--permission-mode` flag. |

The sandbox/permission defaults defer destructive-action gating to each
CLI's own built-in system rather than bypassing it — override only if you
understand the risk.

## Known limitations

- No live streaming — the whole CLI call finishes before Telegram sees anything.
- Turn length is bounded by `timeout_seconds`; long tasks time out and must be re-prompted.
- Explicit `/codex`/`/claude` prefix required every turn — no persistent mode-switch.
- State is per-chat, not per-user — a group chat shares one session across all members.

**Disabling:** `hermes plugins disable coding-cli` (or remove `coding-cli`
from `plugins.enabled`).
