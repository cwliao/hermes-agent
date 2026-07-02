# RCA: SSL CA cert bundle corruption after `hermes update`

**Status:** resolved by `fix(ssl): surface broken CA bundles before provider calls`; DGX Spark host/container recovery documented below.
**Severity:** P2 — degrades the agent into opaque provider/client failures until the user repairs deps or CA configuration.

## Summary

A partial `hermes update`, interrupted venv repair, or stale CA-bundle environment variable can leave Python TLS configuration pointing at a missing, empty, or unloadable CA bundle. The first outbound HTTPS client creation or request can then fail with a raw `FileNotFoundError: [Errno 2] No such file or directory` or a low-level SSL error that does not name the broken CA path.

On DGX Spark and other managed Linux hosts, a system update can also refresh the host trust store while long-lived Python virtualenvs and Docker images continue to use stale bundled CA stores. The visible symptom is usually:

```text
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain
```

This commonly affects web tools (`Tavily search failed`), provider metadata lookups (`openrouter.ai`), Telegram DNS-over-HTTPS fallback discovery, and Open WebUI/Stirling-PDF containers that make outbound HTTPS requests.

## Root cause

Hermes uses OpenAI/httpx and requests-based clients for provider calls, model metadata, gateway delivery, and web tools. Those clients inherit CA bundle settings from:

- `HERMES_CA_BUNDLE`
- `SSL_CERT_FILE`
- `REQUESTS_CA_BUNDLE`
- `CURL_CA_BUNDLE`
- the bundled `certifi` package's `cacert.pem`

When the venv is partially refreshed, or when one of those env vars points at a file that no longer exists, provider client construction can fail before Hermes has enough context to produce a useful message.

## Fix

`agent/ssl_guard.py` validates CA bundle configuration before the OpenAI-compatible provider client is created in `agent/agent_init.py`. It:

1. Checks explicit CA bundle env vars and reports the exact broken variable/path,
2. Verifies `certifi` is importable,
3. Verifies `certifi.where()` points at an existing file of plausible size,
4. Builds an `ssl.SSLContext` from each checked bundle,
5. Raises a typed `SSLConfigurationError` with a repair hint before httpx/OpenAI can raise a raw low-level error.

`hermes_cli doctor` exposes the same check under `SSL / CA Certificates`, so users can diagnose the problem without starting a model session.

## Recovery

When the guard fires during agent init, the user sees a message like:

```text
Failed to initialize OpenAI client: SSL_CERT_FILE points to a missing CA bundle: C:\path\to\missing\cacert.pem
Repair: python -m pip install --force-reinstall certifi openai httpx
If you configured a custom corporate CA bundle, fix or unset the broken CA bundle environment variable.
```

For a normal corrupted Hermes venv, reinstall the affected client dependencies:

```bash
python -m pip install --force-reinstall certifi openai httpx
```

For a custom/corporate CA setup, fix the env var so it points at a real PEM bundle, or unset it if Hermes should use the bundled `certifi` store.

### DGX Spark / managed-host recovery

If the host CA bundle works but Hermes or containers still fail with `self-signed certificate in certificate chain`, point the affected processes at the host trust store:

```bash
SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
python -c "import requests; print(requests.get('https://openrouter.ai/api/v1/models', timeout=10).status_code)"
```

For the user-level Linux gateway service, prefer a systemd drop-in override instead of editing `hermes-gateway.service` directly. `hermes gateway status`, `hermes gateway restart`, and install/refresh paths may rewrite the main unit; drop-ins survive that refresh.

```bash
mkdir -p ~/.config/systemd/user/hermes-gateway.service.d
cat > ~/.config/systemd/user/hermes-gateway.service.d/ssl-ca.conf <<'EOF'
[Service]
Environment="SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
Environment="REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt"
Environment="CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt"
EOF

systemctl --user daemon-reload
systemctl --user restart hermes-gateway.service
systemctl --user cat hermes-gateway.service
```

Verify the live process inherited the override:

```bash
pid="$(systemctl --user show hermes-gateway.service -p MainPID --value)"
tr '\0' '\n' < "/proc/$pid/environ" | grep -E 'SSL_CERT_FILE|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE'
```

### Docker containers

Docker images carry their own CA bundle and do not automatically inherit the host's updated trust store. For containers that make outbound HTTPS calls, mount the host CA bundle read-only and set the common CA env vars:

```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    environment:
      SSL_CERT_FILE: /etc/ssl/certs/ca-certificates.crt
      REQUESTS_CA_BUNDLE: /etc/ssl/certs/ca-certificates.crt
      CURL_CA_BUNDLE: /etc/ssl/certs/ca-certificates.crt
    volumes:
      - /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro
```

For list-form `environment` blocks:

```yaml
environment:
  - SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  - REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
  - CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
volumes:
  - /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro
```

Recreate affected containers after changing Compose:

```bash
docker compose up -d open-webui stirling-pdf
```

Then verify from inside the container:

```bash
docker exec open-webui sh -lc 'python3 - <<PY
import requests
for url in ["https://api.tavily.com", "https://openrouter.ai/api/v1/models"]:
    print(url, requests.get(url, timeout=10).status_code)
PY'
```

## Environment escape hatch

Set `HERMES_SKIP_SSL_GUARD=1` to bypass the preflight check. This is intended only for sandboxed or managed-trust environments where the Python CA path looks unusual but downstream clients are known to work.
