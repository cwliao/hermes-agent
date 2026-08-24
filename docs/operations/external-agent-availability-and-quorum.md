# External agent availability and MoA quorum

**Scope:** DGX Spark host `55-0940189-03` and Hermes runtime operations
**Last verified:** 2026-08-23 (Asia/Taipei)

This is operational memory for future agents. It records how to select real
external agents; it is not a claim that every installed binary is currently
authenticated or reachable.

## Agent inventory

Always probe the current runtime before claiming an agent is available. A
binary on `PATH` is only an installation signal, not a successful agent.

Known candidate classes on this host:

| Candidate | Route | Current probe result | Notes |
|---|---|---|---|
| Codex CLI | `/home/cwliao/.hermes-coding-cli-tools/bin/codex` | `CODEX_READY` | Real read-only probe succeeded; version `0.145.0`. |
| Claude CLI | `/home/cwliao/.hermes-coding-cli-tools/bin/claude` | unavailable | Binary is installed, but the configured CLI profile reported `Not logged in`. |
| Grok CLI | `/home/cwliao/.local/bin/grok` | `GROK_READY` | Real no-tools probe succeeded. |
| AGY / Antigravity CLI | `/home/cwliao/.local/bin/agy` | `AGY_READY` | Real plan-mode probe succeeded. |
| Groq API | `groq:openai/gpt-oss-120b` | unavailable | Hermes route exists, but `GROQ_API_KEY` was not configured. |
| OpenRouter API | `openrouter:<model>` | unstable | Some catalog routes answered in a one-shot probe; a later 10-round, 40-call fan-out returned `Connection error` for every call. Do not assume stability from one success. |
| OpenAI Codex API route | `openai-codex:gpt-5.5` | unavailable | No `OPENAI-CODEX_API_KEY` in the Hermes runtime. |
| Hermes | acting aggregator | available locally | Hermes itself is not an external reference vote and must not be counted in the external quorum. |

The candidate pool is therefore larger than five when API routes and local
CLIs are included. The scheduler should choose from currently healthy routes,
not hard-code the two historical MoA defaults.

## Operator availability declaration

On 2026-08-24 (Asia/Taipei), Keven confirmed that after the DGX Spark reboot
the following agents are available for project review and coding work, subject
to the probe discipline below:

- Claude CLI
- Codex CLI
- AGY CLI
- Grok
- Groq

This is an operator-provided capability/authorization note, not a substitute
for a live health or authentication probe. Do not record or copy credentials
when probing these routes.

## Quorum rule

For `N` enabled external reference slots, require:

```text
required_successes = 0                       when N = 0
required_successes = max(1, N - 1)            when N >= 1
```

Examples:

| Enabled external references | Required usable references |
|---:|---:|
| 4 | 3 |
| 3 | 2 |
| 2 | 1 |
| 1 | 1 |

The acting Hermes aggregator is separate and is never counted as one of these
references. An empty response, failed request, timeout, missing credential,
or recursion-guard skip is not a usable reference.

When the quorum is not met, Hermes may still answer using its acting
aggregator, but it must not inject the minority reference advice into the
aggregator prompt. It must disclose the degraded state under the default
`loud` policy. This prevents one stale or hallucinated advisor from dominating
the answer.

## Probe discipline

Before a real multi-agent test or production MoA turn:

1. Enumerate installed CLI candidates and configured API routes.
2. Run a short, no-tools, read-only probe for each candidate.
3. Record success, empty response, authentication failure, transport failure,
   and timeout separately.
4. Dispatch only healthy candidates, while retaining the configured slot count
   for quorum calculation and reporting skipped/failed slots.
5. Report every failure; never convert a failed route into a synthetic success.

The MoA progress callback must reach `N/N` even when individual references
fail, so operators can distinguish a completed fan-out from a hung fan-out.

## Credential and runtime boundary

Do not copy API keys into this document, repo files, or global memory. CLI
profiles and provider credentials remain in their existing runtime stores.
The development checkout and deployed release are separate; changes to this
document or MoA code are not deployed until a new immutable release is built
and the gateway systemd drop-in is switched and verified.
