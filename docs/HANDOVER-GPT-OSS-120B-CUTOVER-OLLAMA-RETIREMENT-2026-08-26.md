## Handoff: gpt-oss-120b production cutover + Ollama retirement + kanban swarm bugfix

- Date: 2026-08-26
- Project/Repo: multi-project — vLLM host `55-0940189-03` (`/home/cwliao/project/vLLM`), Hermes (`/home/cwliao/.hermes/hermes-agent`), docagent (`/home/cwliao/dgx-workspace`), KMDaily (`/home/cwliao/project/KMDaily`), DocHelper (`/home/cwliao/DocHelper-repo` + deployed `/home/cwliao/DocHelper`), DocuBot (`/home/cwliao/project/DocuBot`), `agentmemory` (`/home/cwliao/agentmemory`), `open-webui-stack` (`/home/cwliao/open-webui-stack`)
- Goal: (1) switch shared `drafter-active` vLLM model from Nemotron to `gpt-oss-120b` in production across all 5 consumers — **done**; (2) retire Ollama as an always-on daemon, make it on-demand, and ensure every dependent service fails gracefully — **partially done**; (3) fix a real Hermes kanban swarm bug found while testing dispatch post-cutover — **designed, cross-reviewed, not yet implemented**.

### Current State

- Status: in progress — imminent host reboot interrupted work mid-stream. **Docker containers with `restart: unless-stopped` will come back automatically after reboot; on-demand containers (Ollama, vllm-vision) will NOT.**
- Completed:
  - gpt-oss-120b production cutover: `vllm-production` container live on port 18000, all 5 consumers (Hermes, docagent, KMDaily, DocHelper, DocuBot's compatibility layer) verified independently.
  - Ollama made on-demand (`~/open-webui-stack/compose.yaml`, `restart: "no"`, `~/bin/ollama-start`/`ollama-stop` scripts created).
  - `OLLAMA_NUM_PARALLEL` lowered 8→2 in the same compose file (real fix for a memory-blowup incident — verified working).
  - `agentmemory` and `open-webui` both fixed to point directly at `vllm-production` instead of Ollama (both were broken/misconfigured before today, unrelated pre-existing bugs found along the way — see Changes Made).
  - Design doc for a real Hermes kanban swarm bug (goal-judge scoping) written and cross-reviewed by 4 engines (claude/codex/agy/groq), revision incorporated (status DESIGN_REVISED).
- Pending:
  - **DocuBot's Ollama→vLLM migration** — prompt already sent to user, they're relaying it to "klib agent" (a separate session) to execute. Not started by this session. See Blockers.
  - **The Ollama "graceful failure" audit for docagent/DocHelper is only partially done** — docagent's fallback path was confirmed already-safe (proper `ConnectionError` handling, no code change needed). DocHelper's `system_health.py` remediation-code behavior was NOT yet verified live with Ollama actually stopped (Ollama happened to be running again by the time this was checked — see Open Questions).
  - **Kanban swarm goal-judge bug fix**: design is DESIGN_REVISED (cross-reviewed, scope corrected), but **zero code has been written or dispatched**. This still needs implementation dispatch (per standing practice: never write code directly, dispatch to codex/agy/grok via `dispatch.js`) and a post-implementation code-diff cross-review.
  - Hermes's own broken fallback config (`fallback_providers: openai-api` pointing at a nonexistent `gpt-oss:20b` model on the real OpenAI API — pre-existing bug, unrelated to today's other work) was found but not fixed.

### Decisions and Rationale

- **Full replacement, no Nemotron fallback** (user's explicit choice, overriding this session's own recommendation): Nemotron's `vllm-production` container was deleted, not preserved. Known accepted risk: Ticket 70's already-evaluated NO-GO findings (fabricated dates, tool-safety violation, long-doc recall gap) are real and unfixed — accepted deliberately by the user, do not re-litigate.
- **Ollama on-demand scope = "ensure graceful failure everywhere," not "delete all Ollama code paths"** (explicit user decision via AskUserQuestion): keep docagent's fallback code, keep DocHelper/DocuBot's provider-switch code, just make sure nothing crashes/hangs badly when Ollama is off by default.
- **DocuBot's active-model migration handed to a separate "klib agent" session**, not this session and not dispatched via agentpool — user's own workflow choice, not to be second-guessed.

### Changes Made

- Files touched:
  - `/home/cwliao/project/vLLM/ticket-71-gpt-oss-120b-production-cutover.md`: full design + cross-review + completion record for the production cutover. **Authoritative reference for cutover specifics.**
  - `~/.hermes/config.yaml`: `vllm-local` provider now points at real `vllm-production`, stale Nemotron `chat_template_kwargs` removed, temporary test provider removed.
  - `~/open-webui-stack/compose.yaml`: `ollama` service → `restart: "no"`, `OLLAMA_NUM_PARALLEL` 8→2; `open-webui` service → `OLLAMA_BASE_URL` removed, `ENABLE_OLLAMA_API: "false"` added, `DEFAULT_MODELS` fixed from stale `drafter-active` → `gpt-oss-120b` (this was a real cutover-day regression found and fixed today).
  - `~/bin/ollama-start`, `~/bin/ollama-stop` (new): on-demand lifecycle scripts, mirroring the existing `~/bin/vllm-vision-start`/`-stop` pattern. **Important**: `ollama-start` uses `docker compose up -d`, NOT `compose start` — `start` does not pick up compose.yaml env changes (verified the hard way today).
  - `/home/cwliao/agentmemory/deploy.compose.yml`: `OPENAI_BASE_URL`/`OPENAI_MODEL`/`OPENAI_API_KEY`/`OPENAI_REASONING_EFFORT` changed from a broken Ollama config (was pointing at `host.docker.internal:11434`, which can NEVER reach a `127.0.0.1`-bound service on this host — confirmed broken since before today, not caused by today's changes) to `vllm-production:8000` via direct docker-network join (`docker network connect agentmemory_default vllm-production`).
  - `/home/cwliao/.hermes/hermes-agent/docs/plans/2026-08-26-kanban-swarm-worker-goal-judge-scope-001.md` (new): design doc for the goal-judge bug, DESIGN_REVISED status.
  - Two new agentmemory lesson entries (`memory_lesson_save`) + one file-based memory (`~/.claude/projects/-home-cwliao/memory/`) covering Ollama/docker-compose/kanban-swarm lessons from today — already saved, no action needed.
- Commits/PRs: none — all changes are live docker-compose config; nothing has been git-committed in any of the affected repos today for these specific changes (Ticket 71's underlying per-project code changes WERE committed earlier by their respective sessions — see ticket-71.md for those commit hashes).
- Commands run (most consequential):
  - `docker network connect open-webui-stack_default vllm-production` — fixed open-webui's connectivity (was silently broken after a container rebuild).
  - `docker network connect agentmemory_default vllm-production` — fixed agentmemory's connectivity.
  - Real 4-lane Hermes kanban swarm test (`t_20805c8d`, triggered by user via Telegram) — 3/4 lanes (`native_hermes`/`claude`/`agy`) succeeded; `grok` timed out twice (caller error in the test prompt — `worker-max-runtime=300` overrode the correct 600s external-lane default, NOT a bug); manually completing the timed-out worker was rejected by the goal-judge bug described above. Test swarm archived after diagnosis.

### Validation

- Checks run:
  - `vllm-production` health + real chat completion: pass.
  - `open-webui`/`agentmemory` container health + real `curl` connectivity to `vllm-production` from inside each: both pass.
  - `hermes-gateway.service`, `dochelper.service`, `docagent-api.service`, `kmdaily-api.service`: all confirmed `active`/`NRestarts=0` at various points today.
  - Kanban swarm goal-judge design doc: cross-reviewed by claude/codex/agy/groq, all 4 converged (see the doc's Revision note).
- Not run:
  - DocHelper's `system_health.py` remediation-code behavior with Ollama *actually* stopped (it kept getting restarted by other activity before this could be cleanly tested).
  - Any real implementation/test of the kanban swarm goal-judge fix — design only.
  - DocuBot's Ollama→vLLM migration — prompt sent, execution status unknown (separate session).

### Blockers and Risks

- Blockers:
  - DocuBot Ollama migration: owned by a separate "klib agent" session the user is driving directly — this session has no visibility into its progress. Prompt content: `/tmp/claude-1000/-home-cwliao/3e833213-54f0-4299-9970-27dd71a425e5/scratchpad/docubot-ollama-migration-prompt.md` (may not survive reboot — recreate from this handoff's Background facts if gone; see the session transcript's DocuBot section for the full text if still accessible).
  - Kanban swarm goal-judge fix: not yet dispatched for implementation — needs a `dispatch.js` call to codex (per standing practice, never code it directly) using the DESIGN_REVISED doc as the spec.
- Risks:
  - **Reboot will stop `ollama` and `vllm-vision` containers** (on-demand, `restart: "no"`) but NOT `vllm-production`/`vllm-embed`/`open-webui`/`agentmemory` (`unless-stopped`, will auto-restart). After reboot, verify `vllm-production` actually came back healthy before assuming production is fine — a fresh boot means a full cold model-load again (~6-8 min observed today).
  - No Nemotron fallback exists anywhere (deleted). If gpt-oss-120b has an incident, there is no automatic recovery path — see Ticket 70's known accepted risks.
  - Hermes's `openai-api` fallback provider is broken (points at a nonexistent model on the real OpenAI API) — not a new risk from today, but now documented; low priority unless Hermes's primary path fails.

### Next Steps (ordered)

1. **After reboot, verify `vllm-production` is healthy**: `docker ps --filter name=vllm-production`, then `curl http://127.0.0.1:18000/health` — expect a multi-minute cold-load wait. Also verify `open-webui`/`agentmemory` reconnected correctly (`docker exec agentmemory curl http://vllm-production:8000/health`).
2. Check in on the DocuBot Ollama migration ("klib agent" session) — ask the user for status if not already reported back.
3. Dispatch the kanban swarm goal-judge fix implementation via `dispatch.js` (codex, `capability: "edit"`), using `/home/cwliao/.hermes/hermes-agent/docs/plans/2026-08-26-kanban-swarm-worker-goal-judge-scope-001.md` as the spec — cover all 3 call sites per the Revision note (not just `hermes_cli/kanban.py` — also `tools/kanban_tools.py`'s duplicate implementation and `cli.py`'s Ralph/goal-mode driver loop).
4. After implementation, run a code-diff cross-review before treating it as done (standing practice this session).
5. Verify DocHelper's Ollama-stopped remediation-code behavior live (stop Ollama cleanly first with `~/bin/ollama-stop`, then check `GET /api/v1/system/health`).
6. Optionally fix Hermes's broken `openai-api` fallback provider config (low priority, pre-existing, unrelated to today's main work).

### Open Questions

- Was DocHelper's Ollama getting restarted today by real automated activity (e.g. its own health-check triggering it), or by a person? Needs to stay stable (stopped) for a clean remediation-code test — investigate before retesting.
- Does the user want Hermes's broken `openai-api` fallback fixed (point at a real model), removed entirely, or left as-is?

### Startup Prompt for Next Conversation

Continue this work using only this handoff. Assume no access to prior chat history. Start with: verify `vllm-production` is healthy after reboot (`docker ps` + `curl http://127.0.0.1:18000/health`), then check `open-webui`/`agentmemory` reconnected to it correctly.
