# Hermes Ollama TLS + Secret Hygiene Handover

**Date:** 2026-07-09
**Operator at handoff:** cwliao (out of MiniMax tokens, handing off to local Ollama)
**Pickup by:** the next operator (human or local LLM) reading this file

This document is the consolidated state-of-the-box for everything done to
the DGX Spark / Ollama / nginx stack in the 2026-07-09 session. It is
self-contained — read it from top to bottom and you have the full
operating picture. For the canonical reference, see
`docs/dgx-spark-runtime-security.md` (this file is the operational summary;
that file is the per-topic deep dive).

## TL;DR

The nginx + Ollama stack now serves the same OpenAI-compatible API on
three endpoints (loopback, LAN plaintext, LAN TLS), with secrets out of
config files, with the Ollama container optimized for the GB10 GPU, and
with 11 per-model context-length variants seeded for predictable memory.
The cert+token+key are backed up at `/home/cwliao/.hermes-backup/`.
A Windows cert-install script lives next to the backup.

## What changed in this session

| Area | Change | File(s) |
|---|---|---|
| nginx | Bearer token out of site config | `/etc/nginx/ollama-token.conf` (new, 0600) |
| nginx | TLS listener on 8443 with self-signed cert | `/etc/nginx/sites-available/ollama-api.conf` (extended) |
| nginx | `/cert` endpoint serves the public cert (bearer-gated) | same |
| nginx | `server_tokens off;` (no nginx version leak) | `/etc/nginx/nginx.conf` |
| firewall | ufw ALLOW for 8443/tcp | ufw ruleset |
| ollama | GPU optimizations (parallel, KV cache, library, sched spread) | `/home/cwliao/open-webui-stack/compose.yaml` |
| ollama | 11 per-model `num_ctx` pinned variants | `/home/cwliao/bin/ollama-ctx-seed.sh` (new) |
| open-webui | `WEBUI_SECRET_KEY` out of compose.yaml into sibling `.env` | `/home/cwliao/open-webui-stack/.env` (new, 0600) |
| backup | Single-file tarball + per-file SHA-256 + restore README | `/home/cwliao/.hermes-backup/` (new) |
| windows | Cert install script with pre-flight + verification | `/home/cwliao/.hermes-backup/install-cert.ps1` (new) |
| housekeeping | Killed a stray native `ollama serve` that was holding ~55 GB GPU and clashing with the Docker one | n/a |

## Current state

### Endpoints (from the LAN)

| URL | Use | Auth | Notes |
|---|---|---|---|
| `http://127.0.0.1:11434/v1` | hermes CLI → local Ollama | dummy `api_key: ollama` | loopback, no TLS, what the gateway itself uses |
| `http://140.96.58.171:8081/v1` | LAN client → proxy (plaintext) | `Authorization: Bearer <token>` | bearer-gated by nginx |
| `https://140.96.58.171:8443/v1` | LAN client → proxy (TLS) | same | TLS 1.2/1.3, self-signed cert |
| `https://140.96.58.171:8443/cert` | cert distribution | same | downloads the public cert for client install |
| `http://140.96.58.171/` | open-webui (LAN) | none | proxied via open-webui site |

The bearer token value is **not** in this doc. It is in
`/etc/nginx/ollama-token.conf` (mode 0600, `root:root`) and in the
backup tarball at `/home/cwliao/.hermes-backup/`.

### Secret locations

| Secret | Where it lives | Where the backup is |
|---|---|---|
| nginx bearer token | `/etc/nginx/ollama-token.conf` (0600) | tarball + `secrets/ollama-token.conf` |
| TLS private key | `/etc/nginx/ssl/ollama-api.key` (0600) | tarball + `secrets/ollama-api.key` |
| TLS public cert | `/etc/nginx/ssl/ollama-api.crt` (0644, public) | n/a (public material) |
| Open WebUI secret | `/home/cwliao/open-webui-stack/.env` (0600) | not backed up — only used inside compose via `${WEBUI_SECRET_KEY}` |
| cert fingerprint | `~/.hermes-backup/secrets/README.md` | same |

### Active configuration values (no secrets — locations and shapes only)

`/etc/nginx/ollama-token.conf`:
```
set $ollama_token "Bearer <token>";
```

`/etc/nginx/sites-available/ollama-api.conf` (two server blocks, both
listen on `server_name 140.96.58.171`):
- Block 1: `listen 8081` (plaintext)
- Block 2: `listen 8443 ssl http2` (TLS 1.2/1.3, `ssl_protocols TLSv1.2
  TLSv1.3`, HSTS enabled)
- Both: LAN-only ACL (140.96.0.0/16, 172.20.0.0/16, 10.0.0.0/8,
  192.168.0.0/16, deny all)
- Both: same bearer-token check via `include /etc/nginx/ollama-token.conf`
- Block 2 extra: `location = /cert { alias /etc/nginx/ssl/ollama-api.crt; ... }`

`/etc/nginx/nginx.conf`:
- `server_tokens off;` on line 21 (was commented out)

`/home/cwliao/open-webui-stack/compose.yaml` (ollama service environment):
```yaml
- OLLAMA_NUM_PARALLEL=8
- OLLAMA_FLASH_ATTENTION=1
- OLLAMA_KEEP_ALIVE=24h
- OLLAMA_MAX_LOADED_MODELS=4
- OLLAMA_KV_CACHE_TYPE=q8_0
- OLLAMA_LLM_LIBRARY=cuda_v13
- OLLAMA_SCHED_SPREAD=true
```

`/home/cwliao/open-webui-stack/compose.yaml` (open-webui service):
- `WEBUI_SECRET_KEY: ${WEBUI_SECRET_KEY}` (loaded from sibling `.env`)

ufw: `8443/tcp ALLOW IN Anywhere` (and v6), plus the pre-existing
`8081/tcp ALLOW IN` and `80/tcp ALLOW IN` (open-webui).

## Per-model `num_ctx` pinned variants

11 base models, 11 pinned variants. The base tags are unchanged; each
pinned variant shares the weight blob (tiny extra disk) but has a
fixed `num_ctx` baked into the Modelfile.

| Base tag | Pinned tag | num_ctx | KV cache (q8_0) |
|---|---|---|---|
| `ornith:9b`              | `ornith:9b-32k`                    | 32768 | ~2.5 GB |
| `ornith:35b`             | `ornith:35b-16k`                   | 16384 | ~1.0 GB |
| `llama3.3:70b`           | `llama3.3:70b-16k`                 | 16384 | ~2.5 GB |
| `gpt-oss:120b`           | `gpt-oss:120b-8k`                  |  8192 | ~0.3 GB |
| `gpt-oss:20b`            | `gpt-oss:20b-16k`                  | 16384 | ~0.4 GB |
| `gemma4:26b`             | `gemma4:26b-16k`                   | 16384 | ~0.9 GB |
| `command-r:35b`          | `command-r:35b-16k`                | 16384 | ~1.3 GB |
| `minicpm-v:latest`       | `minicpm-v:latest-8k`              |  8192 | ~0.1 GB |
| `granite3.2-vision:latest` | `granite3.2-vision:latest-8k`    |  8192 | ~0.3 GB |
| `llama3.2-vision:latest` | `llama3.2-vision:latest-8k`        |  8192 | ~0.3 GB |
| `llava:latest`           | `llava:latest-8k`                  |  8192 | ~2.0 GB |

Re-seeding is idempotent: `/home/cwliao/bin/ollama-ctx-seed.sh` skips
existing tags, accepts `--force` to recreate. To change a context length,
edit `CONTEXT_TABLE` in the script and re-run with `--force`.

## Operating procedures

### Health check (one command)

```bash
# Stack state
docker ps --format '{{.Names}}\t{{.Status}}'
sudo systemctl status nginx --no-pager
sudo ufw status | grep -E '8081|8443'

# End-to-end (replace $(cat ...) with the actual token; the sed extracts
# only the value between quotes)
TOK=$(sudo grep -oP '"Bearer \K[^"]+' /etc/nginx/ollama-token.conf)
curl -sk -o /dev/null -w "8443 /v1/models: HTTP %{http_code}\n" \
  -H "Authorization: Bearer $TOK" https://140.96.58.171:8443/v1/models
curl -s -o /dev/null -w "8081 /api/tags:  HTTP %{http_code}\n" \
  -H "Authorization: Bearer $TOK" http://140.96.58.171:8081/api/tags
```

### Restart everything

```bash
# nginx (graceful; in-flight streams on old workers complete)
sudo nginx -t && sudo systemctl reload nginx

# Ollama (Docker; restart only if unhealthy)
docker compose -f /home/cwliao/open-webui-stack/compose.yaml restart ollama

# open-webui (only if needed; preserve the named volume)
docker compose -f /home/cwliao/open-webui-stack/compose.yaml restart open-webui

# Re-seed per-model num_ctx (only after a `docker compose down -v`
# or if a pinned variant is missing)
/home/cwliao/bin/ollama-ctx-seed.sh
```

### Add or change a pinned model

1. Pull the base model into the container first: `docker exec ollama ollama pull <base>`
2. Edit `CONTEXT_TABLE` in `/home/cwliao/bin/ollama-ctx-seed.sh` — add or change a row
3. Run `/home/cwliao/bin/ollama-ctx-seed.sh --force`
4. Verify with `docker exec ollama ollama list | grep <base>`

### Rotate the bearer token

1. `sudo nano /etc/nginx/ollama-token.conf` — change the value only
2. `sudo nginx -t && sudo systemctl reload nginx`
3. Update the API key in every client
4. Old clients holding the prior value will start getting 401s
5. Update the backup tarball: `tar -czf /home/cwliao/.hermes-backup/hermes-secrets-<TS>.tar.gz -C /home/cwliao/.hermes-backup/secrets ollama-token.conf ollama-api.key README.md` (and update the SHA-256)

### Rotate the TLS cert

1. Generate a new self-signed pair (RSA-2048, 10-year validity, SAN =
   `IP:140.96.58.171, IP:127.0.0.1, DNS:localhost`)
2. Install to `/etc/nginx/ssl/ollama-api.{crt,key}`, `chmod 600` the
   key, `chown root:root` both
3. `sudo nginx -t && sudo systemctl reload nginx`
4. Update the backup tarball
5. Distribute the new public cert to all clients; pin the new
   fingerprint (`openssl x509 -in /etc/nginx/ssl/ollama-api.crt -noout
   -fingerprint -sha256`)

## Pending items (not done in this session)

| Item | Why it's pending | What's needed |
|---|---|---|
| Retire 8081 plaintext | Windows client confirmed on 8443 first | `sed` to remove block 1, `nginx -t && systemctl reload nginx` |
| Narrow `NOPASSWD: ALL` sudo rule | Need the user's preferred shape | Edit `/etc/sudoers.d/90-ollama-nginx` with narrow or broader-but-explicit rule |
| Open WebUI auth/TLS | Separate, larger conversation | Add bearer auth to the `open-webui.conf` site, mirroring what 8081/8443 do |
| The stray native `ollama serve` (PID was 585748) | Killed in this session; children are zombies waiting for the original `pts/0` to reap them | Harmless; the parent shell (PID 585725) will reap on exit. Or `kill 585725` to force |

## Known issues / gotchas

1. **PowerShell `Invoke-WebRequest` and `WebClient` both ignore
   `[Net.ServicePointManager]::ServerCertificateValidationCallback` on
   modern .NET.** They use `HttpClient` under the hood, which has its
   own `HttpClientHandler.ServerCertificateCustomValidationCallback`.
   The simplest workaround for the cert install on Windows is to embed
   the cert in the script and skip the network call entirely (the
   `install-cert.ps1` in the backup dir does this).
2. **Open WebUI container is not auto-recreated when only the `.env`
   value moves.** Same-value moves (like the WEBUI_SECRET_KEY one) leave
   the running container with the in-memory value intact; nothing breaks.
   The new `.env` takes effect on the next manual recreate.
3. **`docker compose config` shows `WEBUI_SECRET_KEY` resolved to its
   actual value.** This is by design — the substitution is at render
   time, not runtime. If you `grep` the rendered config for secrets,
   they'll show up. The real protection is the file mode (0600) on
   `.env`, not the substitution.
4. **`hostname -I` on the box returns multiple IPs** (140.96.58.171
   plus the two Docker bridges 172.17.0.1, 172.18.0.1). The nginx
   server_name is hard-coded to `140.96.58.171`; if the LAN IP
   changes, both the nginx site config and the cert SAN need
   regenerating.

## Audit / what was NOT changed

- Hermes-agent codebase (`run_agent.py`, `cli.py`, gateway, plugins, etc.) — untouched in this session
- The hermes gateway systemd unit — still `~/.config/systemd/user/hermes-gateway.service`, not modified
- `~/.hermes/config.yaml` — not modified (still points at `ornith:9b` via `openai-api` to `localhost:11434/v1`; the new 8081/8443 are for *remote* clients, not the gateway)
- `~/.hermes/.env` — not modified
- The Open WebUI / Stirling PDF / Ollama container images — not upgraded
- Any pre-existing secrets outside the nginx/o/open-webui paths touched here
- `upstream` remote — push disabled by config, do not push there

## File map (everything this handover covers)

```
/etc/nginx/
├── nginx.conf                                       (server_tokens off)
├── ollama-token.conf                                (bearer token, 0600)
├── sites-available/
│   ├── ollama-api.conf                              (2 server blocks, 8081+8443)
│   ├── ollama-api.conf.bak.20260709-103655          (pre-secret-migration)
│   ├── ollama-api.conf.bak.pre-tls-20260709-104636  (pre-TLS)
│   ├── ollama-api.conf.bak.20260709-154153          (pre-/cert-endpoint)
│   ├── open-webui.conf                              (unchanged)
│   └── default                                      (not enabled)
├── sites-enabled/
│   ├── ollama-api.conf -> ../sites-available/...
│   └── open-webui.conf -> ../sites-available/...
└── ssl/
    ├── ollama-api.crt                               (644, public)
    └── ollama-api.key                               (600, root:root)

/home/cwliao/
├── bin/
│   ├── ollama-ctx-seed.sh                           (mode 0700, idempotent)
│   ├── ollama-gpu-healthcheck                       (pre-existing, unchanged)
│   └── codex-ornith35b                              (pre-existing, unchanged)
├── open-webui-stack/
│   ├── compose.yaml                                 (env + .env substitution)
│   ├── compose.yaml.bak.20260709-153455            (pre-WEBUI_SECRET_KEY move)
│   └── .env                                         (mode 0600, WEBUI_SECRET_KEY)
└── .hermes-backup/
    ├── hermes-secrets-<TS>.tar.gz                   (mode 0600, sha256 in log)
    ├── install-cert.ps1                             (mode 0600, embedded cert)
    └── secrets/
        ├── ollama-token.conf                        (mode 0600)
        ├── ollama-api.key                           (mode 0600)
        └── README.md                                (mode 0600, restore + rotation guide)

ufw:
  ALLOW 8081/tcp, 8443/tcp, 80/tcp, 22/tcp, 443/tcp, 8000/tcp, 8010/tcp
  DENY  11434/tcp, 8080/tcp
  default deny (incoming)
```
