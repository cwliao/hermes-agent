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
