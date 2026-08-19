
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
