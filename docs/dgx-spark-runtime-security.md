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

## TAIEX / 0050 SPARK cron

The SPARK market report is implemented as no-agent Hermes cron jobs so the
script stdout is delivered directly to Telegram without an LLM call.

Scripts live under `~/.hermes/scripts/`:

```text
taiex_0050_report.sh         # shared report generator; accepts normal|open|close
taiex_0050_report_normal.sh  # wrapper: normal
taiex_0050_report_open.sh    # wrapper: open
taiex_0050_report_close.sh   # wrapper: close
```

Hermes cron only accepts script filenames from `~/.hermes/scripts/`; do not
schedule absolute script paths. It also does not pass arbitrary trailing
arguments to `--script`, so wrapper scripts are used for the three report modes.

Active jobs:

```text
0 9-13 * * 1-5  TAIEX 0050 hourly report to SPARK  -> telegram:-1004391006048
5 9 * * 1-5     TAIEX 0050 open report to SPARK    -> telegram:-1004391006048
40 13 * * 1-5   TAIEX 0050 close report to SPARK   -> telegram:-1004391006048
```

Check status:

```bash
hermes cron list
hermes cron status
```

Manual non-delivery test:

```bash
~/.hermes/scripts/taiex_0050_report.sh normal
```

Manual delivery test to SPARK:

```bash
hermes send --to telegram:-1004391006048 "$("$HOME/.hermes/scripts/taiex_0050_report.sh" normal)"
```

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
```
