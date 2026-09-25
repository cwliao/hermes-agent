# Upstream rebase 005 (2026-09-25)

Status: **Deployed and verified live.** `candidate_sha 530cd6ff91` (rebase-005's
393 commits + 66 additional upstream commits absorbed by the automated
review's own rebase) is running in production as of 2026-09-25 21:52 CST.
Priority: High (394-commit rebase; see "Deploy incident" below for what it
took to actually get this live)
Repository: `~/.hermes/hermes-agent`
Related: `docs/plans/2026-09-18-upstream-rebase-1356-commits-002.md`,
`docs/plans/2026-09-22-upstream-rebase-003.md`,
`docs/plans/2026-09-23-upstream-rebase-004.md`

## Scope and starting point

- Worktree: `upstream-rebase-005-20260925` at
  `/home/cwliao/.hermes/worktrees/upstream-rebase-005-20260925`
- Base before rebase: `main` @ `212b575954` (already includes rebase-004's
  result, `93fb12971e`, plus subsequent local work through the
  orchestrator-claude kanban lane fix)
- `git fetch upstream main`: `749220ef00..c16e8037d2`
- At the start: upstream ahead by **3265** commits; local-only commits to
  replay: **394**
- Rollback tag: `backup/pre-upstream-rebase-005-20260925` (on the old `main`
  tip, `212b575954`)
- New `main` tip after `git reset --hard`: `aaabdb2c3d`

## Conflict density and resolution summary

38 real conflicts across the 394 replayed commits (roughly one every ~10
commits), each resolved by reading the actual diff in context — never a
blind `--ours`/`--theirs`. Full per-conflict detail lives in this session's
transcript; summary by category:

| Category | Count | Representative example |
|---|---|---|
| Pure additions (empty side, keep both/incoming) | ~18 | New test methods appended to a class both sides also touched |
| Real logic merges (keep both changes, not one or the other) | ~6 | `gateway/run.py` `__init__` multiplex decision + ARCH-001 `_runtime_state` assignment; `_start_gateway_claim_pid_file` bug-fix (`force=force`) + new `runtime_state.close()` cleanup |
| Self-healing gaps (symbol missing now, defined by a still-pending commit) | ~4 | `_thread_metadata_for_event_data` (fixed by `5325a015dc`, itself later in this same rebase); `_auto_post_swarm_handoff` (fixed by `d698dc6ae8`, confirmed pending+ancestor-of-main, not yet reached) |
| Superseded-by-later-upstream (commit's whole point already undone) | 2 | SSL cert bootstrap-on-import (`92cbf461e5`, superseded by upstream's `3071d0bec8` "stop exporting fallback CA bundles on import" — **skipped via `git rebase --skip`, operator-executed**); `legacy_enabled` parametrize (superseded by real upstream commit `b3d4f67b20`, a genuine NousResearch test-pruning pass) |
| Genuinely corrupted historical commit (not a rebase artifact) | 1 | `bc55dda2f8` — its own diff replaces 196 lines of real test code with a literal captured git error message (`fatal: path 'tests/test_code_skew.py' is in the index, but not at stage 2`); title promised a feature the diff never delivers. **Skipped via `git rebase --skip`, operator-executed.** |
| Lockfile conflicts (uv.lock / package-lock.json / pyproject.toml) | 7 | `uv.lock`'s python-3.14-only resolution-marker restructuring made a 47-block markitdown conflict unsafe to hand-merge in full; took `--ours` wholesale there (see "Known residual gaps" below). Smaller, well-scoped lockfile conflicts (paddleocr, cactus-needle, electron-version reverts) were hand-merged after confirming package.json/pyproject.toml's resolution as source of truth. |

### The two `git rebase --skip` calls

Both were **blocked by the auto-mode classifier** ("Irreversible Local
Destruction") and executed by the operator on request, after a full
diagnosis was presented:

1. `92cbf461e5` "fix: prefer managed system CA bundles for Hermes gateway" —
   superseded by upstream.
2. `bc55dda2f8` "fix: expose gateway boot fingerprint for watchdogs" —
   corrupted at the source (see table above); the real
   `_boot_fingerprint`/`record_boot_fingerprint()` feature this commit's
   title describes is already implemented correctly elsewhere in the fork's
   history (unaffected by the skip).

## Known residual gaps (documented, not silently swallowed)

1. **`markitdown[pptx]` has no matching `uv.lock` entry.** The T0109 commit
   (`5f1eaf3736`) added `markitdown[pptx]==0.1.7` to `pyproject.toml`
   (kept), but its `uv.lock` conflict (47 blocks, reflecting a lockfile
   snapshot from a since-heavily-restructured dependency universe —
   `daytona`/`mistralai`/`modal`/`playwright` etc. that no longer apply) was
   judged unsafe to hand-merge. Resolution: kept `main`'s `uv.lock` wholesale
   for that conflict, accepting it is momentarily stale for this one extra.
   **A real `uv lock` run is needed** before `markitdown[pptx]` will
   actually install correctly — blocked in this sandbox by an unrelated
   pre-existing issue (see next point).
2. **`uv lock` cannot fully regenerate in this sandbox.** `hermes-agent[kittentts]`
   → `spacy==2.0.17` fails to build under Python 3.14 here
   (`ImportError: cannot import name 'msvccompiler' from 'distutils'`,
   an ancient package's `setup.py` incompatible with modern `setuptools`).
   This is pre-existing (already present before this rebase) and unrelated
   to today's conflicts. Confirmed via a bare `uv lock` attempt.
3. **`node_modules/electron`'s resolved lockfile entry still says `40.10.6`**
   even though `package.json`'s `devDependencies` (and the intent of the
   already-applied revert commit `bb8280b753` "revert(desktop): roll
   Electron back to 40.10.2") say `40.10.2`. This inconsistency was found
   in **already-merged, non-conflicted** content — i.e., it predates this
   rebase and isn't something introduced by any conflict resolution here.
   Flagged, not fixed (out of scope for a rebase conflict pass; needs a
   real `npm install`/lockfile regen to resolve cleanly).

## Pre-existing production identity issue (found during handoff prep, unrelated to this rebase)

`hermes-gateway.service`'s **currently live** process is not running from
any `~/.hermes/releases/<id>/` directory. The effective (last-applied)
systemd drop-in is:

```
~/.config/systemd/user/hermes-gateway.service.d/zzz...-goal-reply-resume-78259f40a7.conf
ExecStart=/home/cwliao/.hermes/venvs/gateway-78259f40a7/bin/python -m hermes_cli.main gateway run
WorkingDirectory=/home/cwliao/.hermes
```

`78259f40a7` is the tip of `feat/goal-reply-resume`
(`~/.hermes/worktrees/goal-reply-resume-20260921`), a branch that has
**completely diverged** from `main` (neither is an ancestor of the other —
`main` is 2208 commits ahead of their merge-base including all of today's
upstream sync; that branch has 376 commits `main` never absorbed). This
predates today's session and is unrelated to this rebase, but it means:

- Production is **not** currently running any code from the rebase lineage
  at all — today's rebase changes nothing about what's live until a real
  deploy happens.
- `hermes_upstream_apply.py`'s `--previous-release`/`--previous-dropin`
  rollback parameters don't have a clean, standard-shaped "previous release"
  to point at. **This needs an operator decision before running `--execute`**:
  either treat this ad-hoc drop-in itself as the previous state to roll back
  to (non-standard but honest), or first do a separate, smaller "adopt
  `feat/goal-reply-resume` into a real release" step before layering
  rebase-005 on top.

## Post-rebase verification (capped, scoped — no blind full-suite sweep)

```
grep, full repo, all conflict-marker patterns: 2 hits, both confirmed false
  positives (a test that deliberately embeds conflict-marker text as fixture
  data; a reST section-underline `=======` in a docstring)
ast.parse sweep, whole repo: 7781 files, 0 syntax errors
```

Scoped re-run of every test file touched across all 38 conflicts (`ulimit -v
4194304`, single batched invocation):

```
957 passed, 63 failed, 2 skipped, 5 errors
```

All 63 failures + 5 errors trace to one of two already-diagnosed,
non-regression causes, confirmed individually per-conflict during resolution
(not assumed post-hoc):

1. **Worktree-path test-isolation artifact**: `tests/home_io_guard.py`'s
   guard fires on `pm/environments.py`'s `payload_venv()` root-detection
   walking up to the shared `~/.hermes/worktrees/` parent directory when the
   test process's cwd is nested under it (any test transitively importing
   `run_agent`→`hermes_bootstrap`→`pm.environments` hits this). Confirmed
   location-dependent, not content-dependent: the identical test content
   passes cleanly when run from `~/.hermes/hermes-agent` instead of the
   worktree. Not a rebase regression — a pre-existing sandbox limitation of
   running tests from inside a nested worktree.
2. **Genuinely pre-existing failures**, confirmed identical on unrebased
   `main` before any of today's conflict resolution touched the relevant
   files (e.g. `test_image_input_routing_runtime.py`'s
   `test_expired_image_choice_replies_instead_of_falling_through`,
   `test_provider_parity.py` — explicitly called out as pre-existing in
   commit `93fb12971e`'s own message too).

No failure signature outside these two categories was found in the scoped
sweep.

## Operator hand-off — exact commands, in order

### 1. Push (blocked by auto-mode classifier — "Production Deploy"-adjacent)

```bash
cd ~/.hermes/hermes-agent
git push origin main --force-with-lease
```

Safe: rollback tag `backup/pre-upstream-rebase-005-20260925` is already in
place on the pre-rebase tip, and `main` has not diverged from what this
rebase started from (verified: `git rev-parse main` still matched the
worktree's rebase base before the `reset --hard`).

### 2. Decide how to handle the production-identity gap (see section above)

Before running `hermes_upstream_apply.py --execute`, decide:
- (a) point `--previous-release`/`--previous-dropin` at the ad-hoc
  `goal-reply-resume-78259f40a7` drop-in/venv as-is, or
- (b) first land `feat/goal-reply-resume`'s 376 unique commits into `main`
  properly (a separate, smaller merge/rebase task) so the "previous"
  release is a real, standard-shaped one.

This session did not decide this — it surfaced the fact and stopped, per
the standing rule to never fabricate or self-set approval/rollback
parameters.

### 3. Deploy (human-in-the-loop approval token required — not something an
   agent can supply)

```bash
python3 scripts/hermes_upstream_apply.py \
  --repo ~/.hermes/hermes-agent \
  --state-dir ~/.hermes/hermes-upstream-state \
  --run-id <candidate run-id> \
  --previous-release <decided in step 2> \
  --previous-dropin <decided in step 2> \
  --execute
```

Requires `HERMES_UPSTREAM_APPROVAL_TOKEN` and a pre-approved candidate
record (`candidate["approval"]["approved_by"]` +
`approval_token_sha256`) — never fabricate these fields.

### 4. Clean up

```bash
cd ~/.hermes/hermes-agent
git worktree remove --force /home/cwliao/.hermes/worktrees/upstream-rebase-005-20260925
git worktree prune -v
```

Safe once step 1 (push) has landed — `main` already includes this
worktree's tip (verified via `git merge-base --is-ancestor`).

## Blast radius

All 394-commit conflict resolution happened in the isolated worktree; `main`
in the primary checkout was untouched until the final `git reset --hard`
(a pure ref move, rollback tag already in place). The live
`hermes-gateway.service` was never restarted or touched during the rebase
itself — see "Deploy incident" below for what happened once the approved
candidate was actually applied.

## Deploy incident (2026-09-25, post-approval)

The candidate (`run-id 20260925-095317`, `candidate_sha 530cd6ff91` — my
393 rebase-005 commits plus 66 further upstream commits absorbed cleanly by
the automated review's own independent rebase) was approved and applied via
`scripts/hermes_upstream_apply.py --execute`. **This did not work on the
first two attempts**, both caused by gaps in `hermes_upstream_apply.py`
itself and in `pyproject.toml`, not in the rebase's own conflict resolution.

### Attempt 1: apply reported success but didn't change what code ran

`hermes_upstream_apply.py` only writes `WorkingDirectory=`, `PYTHONPATH=`,
and `HERMES_RELEASE_SHA=` into its systemd drop-in — it does **not** build a
dedicated venv or set `ExecStart=`/`VIRTUAL_ENV=`/`PATH=`. Because
production's live drop-in was still the pre-existing ad-hoc
`goal-reply-resume-78259f40a7.conf` (see the "Pre-existing production
identity issue" section above — still unresolved, still live at the time of
this deploy), and that file *does* set `ExecStart=`/`VIRTUAL_ENV=`, and it
sorted lexically after the apply script's thin drop-in, systemd's per-key
`Environment=` merge meant: `PYTHONPATH`/`HERMES_RELEASE_SHA` picked up the
new release's values (keys the ad-hoc conf doesn't set), but `ExecStart` and
`VIRTUAL_ENV` stayed pinned to the old ad-hoc venv. The service kept running
exactly the code it ran before, while `systemctl show` and the apply
script's own post-restart identity check both reported the new release's
`HERMES_RELEASE_SHA` — a false positive, because that check only greps for
the SHA string appearing anywhere in the merged `Environment=` output, not
whether the *executing* code actually changed. Confirmed by asking the
running venv directly to resolve `import hermes_cli` and finding it still
pointed at the old ad-hoc venv's editable-install mapping
(`~/.hermes/hermes-agent`, not the release directory).

**Fix applied:** built a real dedicated venv (`gateway-<candidate_sha
prefix>`) and wrote a full drop-in mirroring the last known-good real
release's shape (`ExecStart=`/`ExecStopPost=`/`VIRTUAL_ENV=`/`PATH=`, not
just the three env vars), with a lexical z-prefix computed from the actual
current max in the drop-in directory (not a stale/guessed one) so it's
guaranteed to sort last.

**`hermes_upstream_apply.py` still needs a real fix** so this can't recur:
it should provision (or verify the existence of) a dedicated venv per
release and write a complete drop-in, not just three environment variables,
before any future run is trusted to actually update what's live.

### Attempt 2: venv built for the wrong Python version, crash-looped production

The dedicated venv was first built with Python 3.11 (matching the ad-hoc
venv's interpreter). Restarting into it **crash-looped production** —
`ModuleNotFoundError: No module named 'ruamel'`, then after a targeted
`pip install`, `ModuleNotFoundError: No module named 'rich'`. Root cause:
`pyproject.toml`'s `[tool.uv]` section pins
`environments = ["python_version >= '3.14'"]` with an explicit comment that
Python 3.11 is a bootstrap/updater bridge only, never a runtime — a whole
block of core dependencies (`ruamel.yaml`, `rich`, `tenacity`, `jinja2`,
`certifi`, `truststore`, `python-dotenv`, `requests`, `fire`, `httpx`,
`tomli-w`, `psutil`, `cryptography`, and more) are declared **only** for
`python_version >= '3.14'`, so a 3.11 venv silently gets none of them.

**Immediate mitigation:** rolled back to the pre-existing
`goal-reply-resume`/`upstream-rebase-002` drop-in combination (the exact
state running before this deploy attempt started) to stop the crash loop.
No saved rollback drop-in existed under
`~/.hermes/hermes-upstream-state/rollback/<run-id>/` for this run (the
directory was never created) — the restore was done by hand from the known
prior config, not from an automated rollback path. **This is a second real
gap in `hermes_upstream_apply.py`**: its documented rollback-safety
mechanism did not actually engage.

**Real fix:** rebuilt the venv with `--python 3.14` (available on this host:
`cpython-3.14.7-linux-aarch64-gnu`). `uv sync`/`uv pip install -e` still
failed outright because `paddlepaddle==3.2.2` (a core, unconditioned-except-
for-3.14 dependency used for OCR fallback) has no `cp314` wheel published
yet for any platform — a genuine upstream PyPI gap, not a rebase or fork
bug. Worked around by installing from a scratch copy of the release
directory with the two `paddlepaddle`/`paddleocr` lines temporarily removed
from `pyproject.toml` (to get every other dependency installed correctly),
then re-running a `--no-deps` editable install against the *real* release
directory to repoint the editable-install mapping correctly without
touching the dependency set. Verified every reference to `paddleocr`/
`paddlepaddle` in the codebase is a lazy, in-function import (not top-level),
so excluding them cannot crash gateway boot — it only disables the
PaddleOCR fallback path for scanned-document text extraction, which already
degrades to a typed `NeedsOcrError` when the engine is unavailable.

**Verification after the real fix:** service stable (same PID, no restarts,
clean Telegram connect) for 10+ seconds after restart; independently
confirmed by asking the running venv to resolve `import hermes_cli`/
`gateway` and getting the release directory back, not the old checkout;
confirmed the live process's `/proc/<pid>/exe` is the 3.14 interpreter.

### Follow-ups not done in this session (flagged, not fixed)

1. `hermes_upstream_apply.py` needs to actually provision a dedicated venv
   (matching the running Python version target from `pyproject.toml`) and
   write a complete `ExecStart=`/`VIRTUAL_ENV=`/`PATH=` drop-in, not just
   three environment variables — this exact failure mode will recur on the
   next automated deploy otherwise.
2. `hermes_upstream_apply.py`'s rollback-backup step
   (`state/rollback/<run-id>/previous-dropin.conf`) did not create anything
   for this run — worth checking why before trusting it on a future deploy.
3. `paddlepaddle`/`paddleocr` have no Python 3.14 wheels yet; the gateway's
   OCR fallback is currently silently degraded on this host. Revisit when
   upstream publishes `cp314` wheels, or consider making the dependency
   fully optional/lazy-installed rather than a core `pyproject.toml` pin.
4. The pre-existing production-identity issue (`feat/goal-reply-resume`'s
   376 unique commits, 376 commits `main` never absorbed) is now doubly
   relevant — the ad-hoc `gateway-78259f40a7` venv and its drop-in are
   superseded by this deploy but were deliberately left in place
   (untouched, still on disk) since that branch's fate is still an open,
   separately-scoped decision.
