# DGX Spark Runtime Security Notes

**Scope:** local deployment notes for Keven Liao's DGX Spark / remote Linux host. This is an operational record, not upstream product guidance.

## Current intent

- Hermes gateway must keep running and must not be restarted or reconfigured unless explicitly needed.
- SSH password login is intentionally retained for now. Hermes startup security audit may warn about `PasswordAuthentication`; this is an accepted operational risk.
- `docagent` currently needs to serve external clients directly and is intentionally bound to `0.0.0.0:8000`.
- Docker services for Open WebUI, Stirling PDF, and Ollama should remain bound to localhost and be exposed only through the configured host services/proxies.

## Hermes gateway

The gateway is managed by the user systemd unit:

```bash
systemctl --user status hermes-gateway.service
```

The main unit may be refreshed by Hermes gateway commands. Keep DGX Spark CA-bundle overrides in a systemd drop-in instead of editing the generated unit directly:

```bash
~/.config/systemd/user/hermes-gateway.service.d/ssl-ca.conf
```

Expected drop-in content:

```ini
[Service]
Environment="SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
Environment="REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt"
Environment="CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt"
```

Verify the live process inherited it:

```bash
pid="$(systemctl --user show hermes-gateway.service -p MainPID --value)"
tr '\0' '\n' < "/proc/$pid/environ" | grep -E 'SSL_CERT_FILE|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE'
```

### OpenAI-compatible local Ollama env

This host uses the OpenAI-compatible environment variables to point Hermes at
local Ollama for the main text model (`openai-api` provider, `ornith:9b`). The
expected `~/.hermes/.env` entries are:

```text
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://localhost:11434/v1
```

`OPENAI_API_KEY` is intentionally a dummy value for local Ollama. Do not keep a
real OpenAI API key in the Hermes gateway runtime environment while
`OPENAI_BASE_URL` points at localhost; it is unnecessary for Ollama and creates
an avoidable secret-exposure risk in logs, diagnostics, or child processes.

On 2026-07-07 the duplicate real `OPENAI_API_KEY` entries in `~/.hermes/.env`
were removed and replaced with the dummy `ollama` value. The gateway was
restarted afterward so the live process inherited the cleaned environment.

### Post-change restart rule

When committing or merging changes in this checkout while the gateway is running
from the same tree, restart the gateway before handing the system back to the
user. The calendar safety guard intentionally reports stale code when the boot
fingerprint differs from the disk checkout.

Required close-out after any Hermes repo commit/push or local upstream merge:

```bash
hermes gateway restart
~/.hermes/scripts/hermes_calendar_guard.sh
hermes gateway status
```

The guard should be silent. If it prints `Gateway is running stale code`, the
restart did not load the current checkout and must be investigated before
closing the task.

### Kanban notifier owner gate

The local gateway loads the Kanban notifier from the same checkout as the
dispatcher. After commit `ad849c39f6`, the notifier honors per-board
`dispatcher_owner` metadata when the current gateway identity can be resolved
from relay auth:

- If a board declares `dispatcher_owner` and it differs from the current gateway
  identity, the notifier skips that board.
- If a board has no owner metadata, or the current gateway identity cannot be
  resolved, Hermes preserves the legacy global notifier behavior.

Operationally, this means a post-update restart is still required before relying
on the owner gate:

```bash
hermes gateway restart
hermes gateway status
tail -n 40 ~/.hermes/logs/gateway.log
```

The expected log path is normal startup followed by `kanban dispatcher:
embedded in gateway`. If stale-code warnings continue after restart, resolve
that first; otherwise the live process may still be running pre-gate code.

### Upstream update record

2026-07-06 upstream update:

- Fetched `upstream/main` from NousResearch.
- Merged into local `main` as `16e11db432`.
- Conflict resolved in `hermes_cli/model_switch.py` by keeping the local
  `model.forbidden` guard and adding upstream's `_declared_model_ids()` custom
  provider helper.
- Updated custom provider grouping tests so config-only grouping cases pass
  `probe_custom_providers=False`; this avoids the unit tests depending on the
  live local Ollama catalog now that upstream probes custom endpoints by
  default.
- Verification run:
  `venv/bin/pytest tests/hermes_cli/test_model_forbidden.py tests/hermes_cli/test_custom_provider_model_switch.py tests/hermes_cli/test_model_switch_custom_providers.py -q`
  -> `53 passed`.
- Close-out completed with `hermes gateway restart`, silent
  `~/.hermes/scripts/hermes_calendar_guard.sh`, and `hermes gateway status`
  showing the gateway active under the new checkout.

Reminder: upstream pulls/merges are local-only. Do not push to `upstream`; the
configured upstream push URL is intentionally `DISABLE`.

## Telegram delivery targets

Known Telegram delivery targets for this host:

```text
ITRIGEL channel: -1003954447810
SPARK group:    -1004391006048
```

Use Hermes' delivery target format rather than legacy `--platform` /
`--chat` flags:

```bash
hermes send --to telegram:-1004391006048 "test message"
```

The SPARK group was manually verified on 2026-07-02 with a TAIEX/0050 test
message; `hermes send` returned `sent`.

### Telegram image OCR and translation

Telegram image OCR/translation is enabled for this host through the gateway
image enrichment path. Incoming Telegram images are pre-analyzed with the
configured local vision model before the main agent turn, so the conversation
receives text containing both OCR and Traditional Chinese translation.

Runtime config in `~/.hermes/config.yaml`:

```yaml
gateway:
  image_ocr_translate:
    enabled: true
    platforms:
    - telegram
    target_language: Traditional Chinese
    include_visual_summary: true

auxiliary:
  vision:
    provider: custom
    model: granite3.2-vision:latest
    base_url: http://127.0.0.1:11434/v1
    api_key: ollama
```

Operational notes:

- This feature is local-first on DGX Spark because `auxiliary.vision` points at
  local Ollama.
- Enabling `gateway.image_ocr_translate` intentionally forces Telegram image
  turns through text enrichment even when the active chat model supports native
  image input; OCR and translation need text injected into the turn.
- If the Telegram image has no more specific caption/request, the gateway
  first stores the image temporarily and asks the user to choose a purpose:
  `1. OCR + 整理文字`, `2. 整理名片`, or `3. 整理新聞`. The selected path then
  replies before the main agent runs. The primary engine is local Tesseract
  (`chi_tra+chi_sim+eng`). Modes 1 and 2 return deterministic OCR-centered
  output when Tesseract extracts text. Mode 3 uses a stateless local-first LLM
  post-process on the Tesseract text only, asking it to reflow news paragraphs,
  summarize key points, preserve uncertainty, avoid adding facts that are not
  present in OCR, and output Traditional Chinese only. The displayed OCR text is
  also normalized to Traditional Chinese to avoid Simplified Chinese leakage. If
  Tesseract returns no text, the gateway falls back to the local vision model.
  After the selected OCR path replies, the numeric choice message is consumed and
  must not continue into the main agent turn. This bypasses prior group-session
  context, browser/web/social-media tools, and internal cache paths.
- If OCR quality is poor, first check `ollama list` and the health of
  `granite3.2-vision:latest`, then test `vision_analyze` against the saved image
  path from the gateway log.

## TAIEX / 0050 SPARK cron

The SPARK market report is implemented as no-agent Hermes cron jobs so the
script stdout is delivered directly to Telegram without an LLM call.

This Hermes install uses the native scheduler store at
`~/.hermes/cron/jobs.json`. It does not use a traditional
`~/.hermes/cron/crontab` file, and older one-shot setup snippets that write a
`crontab` file should not be used on this host.

Scripts live under `~/.hermes/scripts/`:

```text
taiex_0050_report.sh         # shared report generator; accepts normal|open|close
taiex_0050_report_normal.sh  # wrapper: normal
taiex_0050_report_open.sh    # wrapper: open
taiex_0050_report_close.sh   # wrapper: close
```

Hermes cron resolves relative script names under `~/.hermes/scripts/` and
rejects scripts outside that directory. It also does not pass arbitrary trailing
arguments to `--script`, so wrapper scripts are used for the three report modes.

Active jobs:

```text
0,30 9-13 * * 1-5  TAIEX 0050 market snapshot to SPARK -> telegram:-1004391006048
5 9 * * 1-5        TAIEX 0050 open report to SPARK      -> telegram:-1004391006048
40 13 * * 1-5      TAIEX 0050 close report to SPARK     -> telegram:-1004391006048
```

There are three jobs, not four: the market snapshot job covers both whole-hour
and half-hour reports from 09:00 through 13:30 with one cron expression.

The market snapshot is intentionally a `--no-agent` script job. It does not use
skills, so Telegram readability must be improved in
`taiex_0050_report.sh` itself. The current output is a compact
Telegram-friendly block:

```text
📊 台股盤中快照
2026-07-03 11:17 Asia/Taipei

🔴 台灣加權指數 TAIEX
   現價 46410.09｜漲跌 -334.07 (-0.71%)
   前收 46744.16｜資料 11:17:15｜價源 即時｜量 N/A

⚪ 元大台灣50 ETF 0050
   現價 108.8000｜漲跌 +0.00 (+0.00%)
   前收 108.8000｜資料 11:17:19｜價源 即時｜量 48136
```

0050 quote handling was corrected on 2026-07-07 after the 12:30 and 13:00
reports showed the previous close as if it were the live price. The script now
uses TWSE MIS `z` first, falls back to `pz` only when `z` is unavailable, and
prints `N/A` rather than silently substituting `y` (previous close) as current
price. The output includes `價源` so missing live quotes are visible.

The live script under `~/.hermes/scripts/taiex_0050_report.sh` was updated on
2026-07-07 and manually verified at 13:56 Asia/Taipei. The verification output
matched TWSE official 2026-07-07 0050 data: close/current `106.2000`, previous
close `108.2500`, change `-2.05 (-1.89%)`, volume `109650`. The live script is
outside the repo checkout, so this document is the repo-tracked operational
record of that local cron change.

Check status:

```bash
hermes cron list
hermes cron status
```

There is no `hermes cron reload` command in this runtime. The built-in ticker
re-reads `jobs.json` on each tick; after editing jobs through `hermes cron`, use
`hermes cron status` to verify the gateway ticker is alive.

Manual non-delivery test:

```bash
~/.hermes/scripts/taiex_0050_report.sh normal
```

Manual delivery test to SPARK:

```bash
hermes send --to telegram:-1004391006048 "$("$HOME/.hermes/scripts/taiex_0050_report.sh" normal)"
```

## Ollama / GPU health guard

Ollama is currently served by the Docker container named `ollama`, bound to
`127.0.0.1:11434`. It is not managed by `ollama.service`; both
`systemctl status ollama` and `systemctl --user status ollama` may report that
the unit does not exist.

Health is watched by a user systemd timer:

```text
ollama-gpu-healthcheck.timer -> ollama-gpu-healthcheck.service
OnBootSec=2min, OnUnitActiveSec=10min
```

The service runs:

```bash
/home/cwliao/bin/ollama-gpu-healthcheck
```

The healthcheck script verifies the Docker container, NVML inside the container,
the Ollama API at `http://127.0.0.1:11434/api/tags`, and detects loaded models
running on `100% CPU`. On failure it restarts Ollama through:

```bash
docker compose -f /home/cwliao/open-webui-stack/compose.yaml restart ollama
```

Recent journal output on 2026-07-06 showed the timer running every 10 minutes
and reporting `healthy`. A separate no-agent Hermes cron guard may still be
used for SPARK-visible alerts, but the active auto-restart path is the user
systemd timer above.

## Bearer token for remote LLM clients

Remote LAN clients (Windows laptops, etc.) authenticate against the nginx
proxy on 8081/8443 with a bearer token. The token is **out of the
nginx site config** and lives in a dedicated file with restrictive
permissions, included by both server blocks:

- File: `/etc/nginx/ollama-token.conf` (mode 0600, owned by `root:root`)
- Contents: a single `set $ollama_token "Bearer <token>";` line — the
  literal token value is not in this doc; see the file or the backup at
  `/home/cwliao/.hermes-backup/hermes-secrets-<TS>.tar.gz`
- The site config in `/etc/nginx/sites-available/ollama-api.conf`
  references it via `include /etc/nginx/ollama-token.conf;` (the same
  in both the 8081 and 8443 server blocks)

Rotation:

1. Edit `/etc/nginx/ollama-token.conf` (value only; keep the `set ...` line)
2. `sudo nginx -t && sudo systemctl reload nginx`
3. Update the API key in every client
4. Old clients holding the prior value will start getting 401s

**Do not** put the token value in any repo-tracked file, in chat, or
in unencrypted cloud sync.

## TLS endpoint (8443) for remote clients

In addition to plaintext port 8081, the proxy listens on 8443 with TLS
1.2/1.3 for clients that need transport security. A self-signed
RSA-2048 cert is used; clients must install it in their local trust
store.

- Cert: `/etc/nginx/ssl/ollama-api.crt` (mode 0644, world-readable —
  public material)
- Key: `/etc/nginx/ssl/ollama-api.key` (mode 0600, owned by
  `root:root` — secret)
- Validity: 10-year self-signed, through 2036-07-06
- SAN: `IP:140.96.58.171, IP:127.0.0.1, DNS:localhost`
- Cipher list: ECDHE-{RSA,ECDSA}-{AES128,AES256}-GCM-SHA{256,384} +
  CHACHA20-POLY1305
- HTTP/2 enabled
- HSTS: `max-age=31536000`

A bearer-authenticated `/cert` endpoint serves the cert for easy client
install:

```bash
curl -H "Authorization: Bearer <token>" \
  https://140.96.58.171:8443/cert -o ollama.crt
```

The fingerprint (for client pinning) is recorded in
`/home/cwliao/.hermes-backup/secrets/README.md`. A Windows PowerShell
install script that embeds the cert and avoids the `.NET` cert-bypass
quirk is at `/home/cwliao/.hermes-backup/install-cert.ps1`.

Cert rotation:

1. Generate a new pair (RSA-2048, 10y, SAN as above)
2. Install to `/etc/nginx/ssl/ollama-api.{crt,key}`, `chmod 600` the
   key, `chown root:root` both
3. `sudo nginx -t && sudo systemctl reload nginx`
4. Distribute the new public cert to all clients; pin the new
   fingerprint

## Firewall (ufw) rules

`ufw` defaults to `deny incoming`. A rule was added so the LAN can
reach 8443:

```text
8443/tcp    ALLOW IN    Anywhere
8443/tcp    ALLOW IN    Anywhere (v6)
```

The rule mirrors the existing 8081 rule. The nginx 8443 server block
also has its own LAN-only ACL on top of this (allow 140.96.0.0/16,
172.20.0.0/16, 10.0.0.0/8, 192.168.0.0/16, deny all).

Verify with:

```bash
sudo ufw status | grep 8443
```

## server_tokens off

The top-level `/etc/nginx/nginx.conf` has `server_tokens off;` (line
21, uncommented) so the `Server:` response header is just `nginx`
(no version). This blocks distro-specific vulnerability scanning that
fingerprints `nginx/1.24.0 (Ubuntu)`.

## Ollama GPU optimizations

The ollama container is tuned for the GB10 (compute capability 12.1,
121.7 GiB unified memory). The current env in
`/home/cwliao/open-webui-stack/compose.yaml`:

```yaml
- OLLAMA_NUM_PARALLEL=8
- OLLAMA_FLASH_ATTENTION=1
- OLLAMA_KEEP_ALIVE=24h
- OLLAMA_MAX_LOADED_MODELS=4
- OLLAMA_KV_CACHE_TYPE=q8_0
- OLLAMA_LLM_LIBRARY=cuda_v13
- OLLAMA_SCHED_SPREAD=true
```

Effects measured:

- 4 concurrent requests: ~5.9 s wall-clock for all 4
- 8 concurrent requests: ~9.4 s wall-clock for all 8
- 8x concurrent runs hit 92% GPU util, 35 W draw
- Per-request generation rate unchanged (~37 tok/s on `ornith:9b` —
  hardware bound on this iGPU)
- KV cache size at default ctx drops ~20% with `q8_0` (e.g.
  `ornith:9b` 15 GB → 12 GB)

## Per-model num_ctx pinned variants

11 pinned model variants exist alongside the base tags. Each has a fixed
context length baked into the Modelfile, so KV-cache memory is
predictable and 8 concurrent requests can be served without OOMing.

| Base | Pinned | num_ctx |
|---|---|---|
| `ornith:9b`              | `ornith:9b-32k`                    | 32768 |
| `ornith:35b`             | `ornith:35b-16k`                   | 16384 |
| `llama3.3:70b`           | `llama3.3:70b-16k`                 | 16384 |
| `gpt-oss:120b`           | `gpt-oss:120b-8k`                  |  8192 |
| `gpt-oss:20b`            | `gpt-oss:20b-16k`                  | 16384 |
| `gemma4:26b`             | `gemma4:26b-16k`                   | 16384 |
| `command-r:35b`          | `command-r:35b-16k`                | 16384 |
| `minicpm-v:latest`       | `minicpm-v:latest-8k`              |  8192 |
| `granite3.2-vision:latest` | `granite3.2-vision:latest-8k`    |  8192 |
| `llama3.2-vision:latest` | `llama3.2-vision:latest-8k`        |  8192 |
| `llava:latest`           | `llava:latest-8k`                  |  8192 |

The seed script is at `/home/cwliao/bin/ollama-ctx-seed.sh` (mode 0700).
It is idempotent: re-running skips existing tags. `--force` re-creates.
The 11 base tags are unchanged; the pinned variants share weight blobs
(tiny extra disk overhead, KB-level).

To add a new pinned variant or change a context length, edit the
`CONTEXT_TABLE` in the seed script and re-run with `--force`.

## Open WebUI secret management

The Open WebUI container's `WEBUI_SECRET_KEY` was moved out of
`/home/cwliao/open-webui-stack/compose.yaml` and into a sibling `.env`
file. The compose file now uses `${WEBUI_SECRET_KEY}` substitution;
docker compose's automatic `.env` loading picks it up.

- `.env`: `/home/cwliao/open-webui-stack/.env` (mode 0600, owned by `cwliao:cwliao`)
- The compose file references it via `WEBUI_SECRET_KEY: ${WEBUI_SECRET_KEY}`
- The value is **unchanged** from the old in-compose literal (kept
  stable so existing sessions remain valid)
- The open-webui container is recreated automatically only when its
  compose file changes; for a same-value move like this, the running
  container keeps using the in-memory env until the next recreate

The `.env` is dotfile-prefixed (project convention) and not committed.

## Secret backup

A backup bundle of the secret material lives at
`/home/cwliao/.hermes-backup/`:

- `secrets/ollama-token.conf` — the bearer token file
- `secrets/ollama-api.key` — the TLS private key
- `secrets/README.md` — self-describing restore / rotation guide with
  the cert fingerprint, restore commands, and rotation procedures
- `hermes-secrets-<TS>.tar.gz` — single-file portable bundle with
  SHA-256 checksum
- `install-cert.ps1` — Windows PowerShell script to install the
  self-signed cert (embeds the cert directly; no network call, no
  `.NET` TLS plumbing to fight)

The tarball is the recommended artifact to move off-host (password
manager, secondary box, encrypted USB). It is **not** committed to git
and must not be put in unencrypted cloud sync. The directory itself is
mode 0700 with all contents 0600, owned by `cwliao:cwliao`.

## docagent

`docagent` is not managed by Hermes. It may be started from Claude/Codex shell sessions. Current external-service requirement is to bind to all interfaces:

```bash
cd /home/cwliao/dgx-workspace
source .venv/bin/activate
nohup uvicorn docagent.api.main:app --host 0.0.0.0 --port 8000 > /tmp/docagent-uvicorn.log 2>&1 &
```

Check its bind address:

```bash
ss -ltnp | grep ':8000'
```

If the external requirement is removed, reduce exposure by restarting it as localhost-only:

```bash
pkill -f 'uvicorn docagent.api.main:app.*--port 8000'
cd /home/cwliao/dgx-workspace
source .venv/bin/activate
nohup uvicorn docagent.api.main:app --host 127.0.0.1 --port 8000 > /tmp/docagent-uvicorn-localhost.log 2>&1 &
```

## SSH

Password authentication is intentionally retained for now. Do not apply `PasswordAuthentication no` unless Keven explicitly asks for key-only SSH.

Current audit warning to expect:

```text
SSH password authentication is ENABLED
```

## Docker exposure

Expected bindings:

```text
open-webui    127.0.0.1:8080->8080/tcp
stirling-pdf  127.0.0.1:8089->8080/tcp
ollama        127.0.0.1:11434->11434/tcp
```

Verify with:

```bash
docker ps --format '{{.Names}}\t{{.Ports}}\t{{.Status}}'
```

The Open WebUI stack should keep the host CA bundle mounted read-only when managed-host TLS interception or DGX Spark CA changes affect container HTTPS:

```yaml
- /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro
```

## Quick health check

```bash
hermes gateway status
SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
hermes doctor
ss -ltnp
docker ps --format '{{.Names}}\t{{.Ports}}\t{{.Status}}'

# Bearer-authenticated remote endpoints (token extracted at runtime, never
# inlined here; see /etc/nginx/ollama-token.conf and the backup at
# /home/cwliao/.hermes-backup/hermes-secrets-<TS>.tar.gz)
TOK=$(sudo grep -oP '"Bearer \K[^"]+' /etc/nginx/ollama-token.conf)
curl -sk -o /dev/null -w "  8443 /v1/models: HTTP %{http_code}\n" \
  -H "Authorization: Bearer $TOK" https://140.96.58.171:8443/v1/models
curl -s -o /dev/null -w "  8081 /api/tags:  HTTP %{http_code}\n" \
  -H "Authorization: Bearer $TOK" http://140.96.58.171:8081/api/tags
sudo ufw status | grep -E '8081|8443'

# Per-model num_ctx pinned variants (should be 11 entries, one per base)
docker exec ollama ollama list 2>/dev/null | awk 'NR>1 && $1 ~ /-[0-9]+k$/ {n++} END {print "  pinned num_ctx variants: "n"/11"}'

# GPU
nvidia-smi --query-gpu=utilization.gpu,memory.used,power.draw --format=csv,noheader | head -1
```
