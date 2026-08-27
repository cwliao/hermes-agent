---
title: "auxiliary.vision (vllm-vision, 127.0.0.1:18003) is now an on-demand Docker service; the vision tool call path has no health-check/wake logic and no Python precedent for this pattern exists in the repo"
status: DONE
date: 2026-08-27
type: design-proposal
target_repo: hermes-agent
---

## Implementation record (2026-08-27)

Implemented and committed. 5 total cross-review rounds: design round 1
(CHANGES-NEEDED, claude/agy disagreed on trusting the start script's
exit code, resolved in agy's favor by reading the script directly),
design round 2 (CHANGES-NEEDED, claude/codex found the video_analyze_tool
gap), design round 3 (APPROVE, claude/agy), implementation round 1
(CHANGES-NEEDED, codex found a real cancellation race plus 3 config
edge cases), implementation round 2 (CHANGES-NEEDED minor, codex found
one remaining OverflowError gap after the round-1 fixes landed).

64 targeted tests pass (`tests/tools/test_vision_tools.py`,
`tests/tools/test_video_analyze.py`), independently re-run and verified
outside the dispatched agents' own reports before commit at every round.

## Revision note (post cross-review, 2026-08-27)

claude and agy both reviewed and found real gaps; on one point they gave
*opposite* recommendations, resolved below by reading the actual script
(not trusting either review's paraphrase):

**Resolved disagreement — do NOT trust the start script's exit code as a
readiness signal.** claude recommended awaiting
`/home/cwliao/bin/vllm-vision-start`'s own exit code (0 = healthy, 1 =
timeout) instead of a separate Python-side poll, to avoid two
independent timeouts drifting apart. agy found a live race in the same
script and recommended the opposite: Python must always poll `/health`
itself regardless of the script's exit code. Read the script in full to
settle this — **agy is correct**:

```bash
if [[ "$(docker inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null || echo missing)" == "running" ]]; then
  echo "vllm-vision already running."
  exit 0
fi
```

If the Docker container status is already `running` (true within ~1s of
`docker start`, well before the vLLM process inside has finished loading
weights), the script exits 0 **immediately, without ever checking
`/health`**. Concrete failure: two concurrent `vision_analyze_tool` calls
both find the service down; call A triggers `docker start` (status flips
to `running` almost instantly) and begins its own poll; call B, running
moments later, sees status already `running`, hits this branch, and
exits 0 having never queried `/health` at all. If Python treated call
B's exit-0 as "ready," it would proceed to the real request while the
model is still loading.

**Fixed**: the start script is a trigger only, not a completion signal.
Python always polls `/health` itself until 200 (or its own timeout),
regardless of what the script's exit code says. The script is still
invoked (so `docker start` actually happens and its stdout/stderr is
available for logging), but its exit code is not the readiness gate.

Also incorporated, both reviewers agreed:
- No progress/status callback is reachable from `vision_analyze_tool`'s
  call site (verified exhaustively by both reviews independently) —
  `logger.info`/`logger.warning` is the only mechanism; the doc no
  longer asks the implementer to search for one.
- Must be fully async: `asyncio.create_subprocess_exec` for the script
  (not `subprocess.run`, not `create_subprocess_shell`), `httpx.AsyncClient`
  for the health polls (already imported in `tools/vision_tools.py:42`
  per agy's citation), `asyncio.sleep` between polls — a blocking call
  here stalls the shared gateway event loop for up to ~150s.
- Hook placement refined: after image validation/download/encoding,
  immediately before the `async_call_llm` call (~`tools/vision_tools.py:1500-1508`),
  not at the very top of `vision_analyze_tool` — a malformed/invalid
  image URL should still fail fast in milliseconds, not after waiting up
  to 150s for a service the request was never going to use successfully
  anyway (agy's point).
- Script path config value must go through `os.path.expanduser`/`os.path.expandvars`
  before use with `asyncio.create_subprocess_exec` (which does no shell
  expansion), and should be validated with `os.path.isfile`/`os.access(..., os.X_OK)`.
- Add an optional `on_demand_timeout` field (default 150.0) instead of a
  hardcoded 150s constant.
- `_is_provider_unhealthy`/`_mark_provider_unhealthy` citation corrected:
  it has 9 call sites (`agent/auxiliary_client.py:2889,2908,2940,2952,3004,5286,5389,10015,10713`),
  covering auth/rate-limit/missing-credential cases too, not only HTTP
  402 as originally stated — the bottom-line conclusion (a stopped-container
  connection error never touches this cache, so don't reuse it) is
  unaffected by the correction.
- An `asyncio.Lock` to deduplicate concurrent wake attempts within one
  process is a worthwhile addition (avoids redundant script invocations
  and duplicate poll loops) but is an optimization, not a correctness
  requirement — correctness now comes entirely from each call
  independently polling `/health` to 200 before proceeding, per the
  resolved disagreement above.

Scope below is revised accordingly.

## Revision note 2 (post round-2 cross-review, 2026-08-27)

claude and codex both found the same real functional gap round 1's
framing implied but Scope never actually specified: the doc's opening
sentence says the helper should be "reusable by both `vision_analyze_tool`
and `video_analyze_tool`," but Scope steps 1-7 only describe wiring it
into `vision_analyze_tool`. An implementer following Scope literally
would leave `video_analyze_tool` exactly as broken against a stopped
`vllm-vision` container as it is today. Fixed below: Scope now explicitly
requires wiring the same helper into both call sites, with a matching
Validation criterion.

Also incorporated, codex's round-2 findings:
- **Hook fires once per tool invocation, not once per internal retry.**
  `vision_analyze_tool` has size/empty-content retry logic after the
  first `async_call_llm` call; the pre-flight wake/health-check must run
  before the *first* attempt only, not be re-triggered on internal
  retries within the same tool call.
- **Subprocess and polling run concurrently, not sequentially.** Do not
  `await` the start script's full completion (which can itself take up
  to ~120s per its own `WAIT_SECONDS`) before beginning the Python-side
  `/health` poll loop — start the subprocess, then poll concurrently
  (e.g. `asyncio.create_task` for the subprocess, poll in the main
  coroutine, reap/cancel the subprocess task on the overall timeout or
  on cancellation of the outer call).
- **Partial config is a configuration error, not a silent no-op.** If
  exactly one of `on_demand_health_url`/`on_demand_start_script` is set
  (not both, not neither), that's a misconfiguration — log a warning and
  either treat as unset (no-op, safest) or fail closed with a clear
  error; do not silently proceed as if the on-demand feature were fully
  configured when it's only half-configured. Pick one behavior explicitly
  during implementation and document it in a code comment.
- **`on_demand_timeout` is a hard wall-clock budget, not a per-poll
  timeout.** Use a monotonic clock (`asyncio.get_event_loop().time()` or
  `time.monotonic()`) to track total elapsed time across the whole
  wait-for-ready sequence; each individual health-check HTTP timeout and
  each `asyncio.sleep` between polls must be bounded by the *remaining*
  budget, not allowed to independently add up past
  `on_demand_timeout` in aggregate.
- Citation fixed: `_mark_provider_unhealthy` has 9 call sites (not 8 as
  Revision-note-1 stated — the 9 line numbers listed were already
  correct, only the count label was off by one).
- Validation criterion 5 needs explicit assertion mechanics (see revised
  criterion below) so it actually fails against a naive
  exit-code-trusting implementation rather than merely describing the
  right scenario without pinning down what's asserted.

Scope and Validation criteria below are revised accordingly.

## Motivation

`vllm-vision` (serving the `vision-active` model, currently
`HuggingFaceTB/SmolVLM2-2.2B-Instruct`) was changed today from an
always-on container to an on-demand one — idle, it holds ~13.4GB of the
host's shared memory pool, and this DGX host is a shared
desktop-workstation-plus-multiple-AI-services box, so it now starts only
when needed via `/home/cwliao/bin/vllm-vision-start` (idempotent) and
stops only via an explicit, human-or-scheduled
`/home/cwliao/bin/vllm-vision-stop` call — there is no auto-idle-shutdown.
Cold start takes a measured ~110 seconds until `GET
http://127.0.0.1:18003/health` returns 200.

Investigated (Explore agent, full trace, not guessed) what happens today
if `hermes-agent`'s vision tool call path is used while the container is
stopped, and whether any health-check/wake logic already exists anywhere
in the repo to reuse.

## Root cause / current state (confirmed via direct code read)

**Call path** (`~/.hermes/config.yaml`'s `auxiliary.vision: {provider:
custom, model: vision-active, base_url: http://127.0.0.1:18003/v1, ...}`
through to the real HTTP call):

1. `tools/vision_tools.py:1299` `vision_analyze_tool(...)` — the tool
   entry point. Builds `call_kwargs` at `tools/vision_tools.py:1500-1505`
   and calls `await async_call_llm(**call_kwargs)` at
   `tools/vision_tools.py:1511`.
2. `agent/auxiliary_client.py:8044` `_resolve_task_provider_model()`
   reads `auxiliary.vision.provider/model/base_url/api_key` via
   `_get_auxiliary_task_config("vision")` (`:8071-8083`).
3. `agent/auxiliary_client.py:9351` `_call_llm_impl(...)` dispatches to
   `resolve_vision_provider_client(...)` (`:9424`) for `task == "vision"`.
4. `agent/auxiliary_client.py:7269` `resolve_vision_provider_client(...)`
   — because `auxiliary.vision.base_url` is explicitly set, takes the
   direct-override branch (`:7300-7315`) and builds a plain
   `OpenAI(base_url="http://127.0.0.1:18003/v1", api_key="vllm")` client.
5. `agent/auxiliary_client.py:9058` (or `:9065` for streaming) —
   `client.chat.completions.create(**kwargs)` — the actual wire call.

**No health-check or wake logic exists anywhere in this path.** Confirmed
by grep across `tools/vision_tools.py` and `agent/image_routing.py` for
`health`/`/health` — zero hits in the call path itself.

**The existing `_is_provider_unhealthy`/`_mark_provider_unhealthy` cache**
(`agent/auxiliary_client.py:4059-4144`) is a poor fit and must NOT be
extended for this — confirmed by reading it fully:
- Only populated from a confirmed HTTP 402 payment-error branch
  (`agent/auxiliary_client.py:10015-10017`), never from a connection
  error — a stopped container's connection-refused never touches this
  cache at all.
- Its purpose is "skip to an alternate provider," not "wake and retry the
  same target" — no hook anywhere runs an action when an entry is marked.
- Its 600s TTL doesn't match a ~110s cold start and nothing would clear it
  early even if it did apply here.
- `provider: custom` here is explicit and pinned — there is no alternate
  vision-capable provider to fall back to, so "skip this provider" is not
  the desired behavior; "wake it and use it" is.

**What happens today if the container is stopped** (traced fully):
`client.chat.completions.create` raises `openai.APIConnectionError`.
`_is_transient_transport_error`/`_is_connection_error`
(`agent/auxiliary_client.py:4296`/`:4258`) classify this as transient and
retry the **same dead endpoint** 2 more times
(`auxiliary.transient_retries`, default 2) with backoff — all doomed,
adding real wall-clock delay for nothing. After retries exhaust, the
fallback-chain logic (`agent/auxiliary_client.py:9975-10113`) tries
`_try_configured_fallback_chain` then `_try_main_agent_model_fallback`;
both return `None` (no vision fallback configured). The original
exception is re-raised (`:10113`), caught by `vision_analyze_tool`'s
outer `try/except` (`tools/vision_tools.py:1564`), and turned into
`{"success": False, "error": ..., "analysis": "There was a problem with
the request and the image could not be analyzed..."}` (`:1601-1605`).

**Net effect today**: no hang, no silent failure — but no wake either.
The model/user just gets told the image couldn't be analyzed, after a
few seconds of pointless same-endpoint retries, even though the service
would have been perfectly usable ~110 seconds later if woken.

**No Python precedent exists** for "run a start script, poll a health
endpoint, then proceed" anywhere in this repo or the adjacent `docagent`
project (`grep -rn "ollama-start\|ollama_start\|vllm-vision-start"
--include=*.py` across `hermes-agent`: zero matches). The pattern that
exists is entirely inside the bash scripts themselves
(`~/bin/ollama-start`, `~/bin/vllm-vision-start` — both: check container
state, `docker start`/`compose up -d` if needed, poll health every 5s up
to a timeout). `docagent`'s `ollama_client.py` `_model_admission` is a
concurrency-serialization file lock, not a start-and-wait pattern —
different problem, not reusable here.

## Scope

Add a new, self-contained async pre-flight helper (e.g.
`_ensure_vision_service_ready(...)`, reusable by both
`vision_analyze_tool` and `video_analyze_tool`, which shares the same
`async_call_llm(task="vision", ...)` sink per agy's citation
`tools/vision_tools.py:2081`), called from `vision_analyze_tool`
immediately before the `async_call_llm` call
(~`tools/vision_tools.py:1500-1508`, i.e. after image
validation/download/encoding, not at the top of the function):

1. Reads three new optional config fields under `auxiliary.vision`
   (all default to `None`/unset, so this is a no-op for any vision
   provider that isn't this specific on-demand local setup — do not
   infer from `base_url` containing `127.0.0.1`/`18003`; require
   explicit opt-in):
   ```yaml
   auxiliary:
     vision:
       # ...existing fields unchanged...
       on_demand_health_url: http://127.0.0.1:18003/health
       on_demand_start_script: /home/cwliao/bin/vllm-vision-start
       on_demand_timeout: 150.0   # optional, defaults to 150.0
   ```
2. If all required fields are unset: no-op, proceed to the existing call
   path unchanged (regression guard for every other vision config).
3. `GET on_demand_health_url` via `httpx.AsyncClient` with a short
   timeout (~5s). If 200, return immediately — proceed to the existing
   call path.
4. If not 200 (or connection error): resolve the script path through
   `os.path.expanduser`/`os.path.expandvars`, validate with
   `os.path.isfile`/`os.access(..., os.X_OK)`, then run it via
   `asyncio.create_subprocess_exec(script_path, stdout=..., stderr=...)`.
   `logger.info` that the vision service is starting (this is the only
   reachable progress signal — no callback exists on this call path, per
   both reviews' exhaustive check; do not search for one).
5. **Do not use the script's own exit code as the readiness signal** —
   confirmed by reading the script directly (see Revision note): it can
   exit 0 having never checked `/health` at all, if another caller's
   `docker start` already flipped the container to Docker-`running`
   status moments earlier. Regardless of the script's exit code or how
   long it took, poll `on_demand_health_url` independently (e.g. every
   3-5s via `httpx.AsyncClient` + `asyncio.sleep`) until it returns 200
   or `on_demand_timeout` elapses. This poll is the sole source of truth
   for "the service is actually ready" — the script is a trigger, not a
   completion signal.
6. If the health poll times out: fail **the same way this path already
   fails today** — return the existing `{"success": False, "error": ...}`
   shape (do not invent a new error shape).
7. Once healthy (step 3 or step 5), proceed to the existing, unmodified
   call path. Run this pre-flight sequence once per tool invocation, on
   the first attempt only — do not re-trigger it on
   `vision_analyze_tool`'s existing internal retries (size/empty-content
   retries after a completed `async_call_llm` call).
8. **Wire the identical helper into `video_analyze_tool` too**
   (`tools/vision_tools.py`, video validation/encoding completes by
   ~line 2036, `async_call_llm` call at line 2081 — call the helper
   between those, mirroring the `vision_analyze_tool` placement exactly).
   Both tools route through the same `async_call_llm(task="vision", ...)`
   sink and the same stopped-container failure mode, so both need the
   same fix — this is not optional/follow-up scope, it's required for
   this ticket to actually close the gap it exists to close.

**Constraints:**
- Do not touch `_is_provider_unhealthy`/`_mark_provider_unhealthy` — confirmed
  wrong tool for this job (9 call sites, none triggered by a connection
  error to a stopped container — see Revision note for the corrected
  citation), leave it alone.
- Do not touch `resolve_vision_provider_client`, `_call_llm_impl`, or any
  of the transient-retry/fallback-chain logic in `agent/auxiliary_client.py`
  — this is a pre-flight step ahead of the existing call path, not a
  rewrite of it. The existing retry/fallback behavior stays exactly as it
  is for the (now much rarer) case where the service is healthy at
  pre-flight time but becomes unavailable mid-request.
- All three new config fields default to unset/`None` — strictly
  opt-in per-config, never inferred from `base_url`.
- Do not add any auto-stop/idle-shutdown logic — confirmed explicitly out
  of scope; shutdown stays a human/scheduled decision outside this code,
  matching the current `vllm-vision-stop` script's own documented usage.
- Fully async, no blocking calls on the event loop: `asyncio.create_subprocess_exec`
  (not `subprocess.run`, not `create_subprocess_shell` — the script path
  is a fixed executable, not a shell string), `httpx.AsyncClient` for
  every health check, `asyncio.sleep` between polls. The subprocess and
  the health-poll loop run concurrently (don't await the script's own
  completion first, then start polling — the script's own internal wait
  can take up to ~120s on its own); reap/cancel the subprocess task when
  the overall `on_demand_timeout` budget is exhausted or the outer call
  is cancelled.
- `on_demand_timeout` is a hard wall-clock budget for the entire
  wait-for-ready sequence, tracked via a monotonic clock — every
  individual health-check HTTP timeout and every inter-poll
  `asyncio.sleep` must be bounded by the *remaining* budget, not summed
  independently past the configured total.
- If exactly one of `on_demand_health_url`/`on_demand_start_script` is
  set (not both, not neither): this is a misconfiguration. Log a warning
  and pick one explicit, documented behavior (treat as fully unset/no-op,
  or fail closed with a clear config error) — implementer's call, but it
  must be a deliberate choice with a code comment, not silent
  fall-through that looks like the feature is active when it's actually
  half-configured.
- An `asyncio.Lock` (module-level, keyed by health URL or simply a single
  lock since there's currently only one on-demand vision endpoint) to
  deduplicate concurrent wake attempts within one process is a worthwhile
  addition — reduces redundant script invocations and duplicate poll
  loops when multiple tool calls discover the service down at once — but
  is an optimization, not a correctness requirement, since step 5's
  independent health poll is what actually guarantees correctness
  regardless of how many callers triggered the script.

## Validation criteria

1. Unit test: `on_demand_health_url`/`on_demand_start_script` both unset
   (today's default config shape) → pre-flight step is a complete no-op,
   behavior identical to today (regression guard — most vision configs,
   including any future cloud-provider vision setup, must never trigger
   this).
2. Unit test: health URL returns 200 on the first check → no start script
   invocation, proceeds immediately (no added latency for the common
   already-warm case).
3. Unit test: health URL fails once, start script is invoked (mock the
   subprocess call, assert it was called with the configured, expanded
   script path), then health URL returns 200 on a subsequent poll →
   proceeds to the real call.
4. Unit test: health URL never returns 200 within `on_demand_timeout` →
   returns the same `{"success": False, "error": ...}` shape the
   connection-error path already produces today, not a new/different
   error shape, and does not hang past the configured timeout.
5. **Unit test (regression guard for the resolved disagreement,
   assertion mechanics pinned down per round-2 review)**: mock the start
   script to exit 0 immediately (simulating the
   already-running-per-Docker-but-not-yet-healthy race); mock the health
   endpoint to return non-200 for the first N calls, then 200 on call
   N+1; mock `async_call_llm` (or the point where the pre-flight step
   hands off to the real call) to record how many health-poll calls had
   happened by the time it was invoked. Assert: (a) the health mock was
   called at least N+1 times, (b) the real call happens only after the
   health mock's N+1-th (200-returning) call, not before — i.e. the test
   must fail if a naive implementation proceeds right after the script's
   exit 0 without waiting for the N+1-th health call to return 200.
5b. **Unit test**: opt-in config set, but the image itself is invalid
   (e.g. malformed URL) → the start script mock is never invoked at all
   — confirms the hook placement (after validation) actually protects
   the fail-fast path, not just that it's documented to.
5c. **Unit test**: `video_analyze_tool` with the container down → same
   wake-and-poll behavior as `vision_analyze_tool`, verified independently
   (not just "it shares code with the vision test" — an actual test
   exercising the video call path).
6. Confirm (read, not necessarily a new automated test) that this
   pre-flight step runs fully async and does not block the shared
   gateway event loop during the wait — verify no blocking
   `subprocess.run`/`time.sleep`/synchronous `requests` call is used
   anywhere in the new code.
7. Live smoke test after implementation (not unit-mocked): with
   `vllm-vision` actually stopped, trigger a real `vision_analyze_tool`
   call through Hermes and confirm it waits, wakes the container, and
   successfully analyzes a real test image — mirroring the direct `curl`
   verification already done manually against `vision-active` today, but
   through the actual Hermes code path this design doc adds logic to.

## Explicitly not solved here

- No auto-idle-shutdown / stop-after-use logic.
- No change to `_is_provider_unhealthy`, the fallback-chain logic, or the
  transient-retry logic in `agent/auxiliary_client.py`.
- No generalization to other auxiliary tasks (web_extract, etc.) or other
  on-demand services (Ollama) in this ticket — scoped narrowly to
  `auxiliary.vision`'s specific, concrete, already-live need. If the same
  pattern is wanted for Ollama or another on-demand backend later, that's
  a follow-up ticket that can reuse this one's config-field shape and
  pre-flight-step approach, not something to speculatively build now.
- No change to image cost/quality/model-selection logic — only the
  liveness/wake gate ahead of the existing, unmodified call.

## Required before implementation

Round 1 (claude, agy — codex busy, grok returned empty): both found real
gaps; disagreed on one point (trusting the start script's exit code),
resolved in agy's favor by directly reading the script (see Revision
note). Round 2 (claude, codex, agy — grok returned empty): claude and
codex both CHANGES-NEEDED, converging on the same real gap
(`video_analyze_tool` claimed-but-not-specified in Scope) plus several
implementation-precision points (subprocess/poll concurrency, partial
config, timeout budget semantics, criterion 5 assertion mechanics); agy
APPROVE. Revision note 2 and the updated Scope/Validation criteria above
incorporate every point from both rounds. Re-review this diff before
implementation.
