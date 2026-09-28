---
title: "UV-LOCK-ML-CLUSTER-CORRUPTION-001 — uv.lock has several missing/swapped package entries in the ML/OCR dependency subtree"
status: IMPLEMENTED
date: 2026-09-28
type: ticket-design
ticket: UV-LOCK-ML-CLUSTER-CORRUPTION-001
target_repo: hermes-agent
base: f604a90b07 (main, migration/T0154-canonical-root as of this ticket)
---

# UV-LOCK-ML-CLUSTER-CORRUPTION-001

Implemented in commit `6930dfa80c` on branch `fix/tflite-runtime-lock`
(merged to `main`). Final approach deviated from the design-time plan in one
respect: `beautifulsoup4`/`scipy`/`scikit-learn` were **deleted**, not
hand-crafted back in from PyPI metadata — after the openwakeword block and
its stale metadata reference were removed, nothing in `pyproject.toml` or
the rest of `uv.lock` referenced these three packages at all, confirming
they were orphaned cruft from the same reintroduction, not a legitimately
still-needed dependency with corrupted metadata.

## Why this ticket

Every `hermes` invocation currently prints a noisy (but so far non-blocking)
warning:

```
error: Failed to parse `uv.lock`
  cause: Dependency `tflite-runtime` has missing `source` field but has more
  than one matching package
```

`hermes pm`'s own dependency-sync step falls back to "running with the
previous dependencies" whenever this happens, so nothing has actually broken
yet -- but `uv lock` itself cannot run at all in this state, which means the
lockfile cannot be safely updated or audited until this is fixed.

## What's actually wrong (confirmed this session, in an isolated worktree
`~/.hermes/worktrees/fix-tflite-runtime-lock`, never touching the live
checkout)

`uv.lock`'s error messages are misleading -- "has missing `source` field but
has more than one matching package" is printed even when a dependency has
**zero** matching `[[package]]` blocks, not multiple. Investigating one
surfaced error at a time (each fix only reveals the next), the following
distinct corruptions were found so far, all clustered in the ML/OCR/audio
dependency subtree:

| Symptom | Package | Root cause |
|---|---|---|
| Referenced, zero `[[package]]` block; **CORRECTED (round 3): this is NOT a separate defect from the `openwakeword` row below -- it's the same one, at a nested layer** | `tflite-runtime` | `tflite-runtime` appears exactly once in the entire 6700+ line file, nested inside `openwakeword`'s own `dependencies = [...]` array (alongside `onnxruntime`, `scikit-learn`, `scipy`). There is no independent, freestanding reference anywhere else. Round 2's causal story ("stale leftover metadata from before the 2026-09-07 migration") was itself wrong, caught in round 3 (claude) -- it isn't freestanding stale metadata predating the migration; it's a real edge inside a fully-formed `openwakeword` block that was *reintroduced* later (see the `openwakeword` row for the correct provenance, `57dec7ade9c`, 2026-09-22). Fixing the `openwakeword` row's root cause automatically fixes this row too -- purging the `openwakeword` block removes its embedded `tflite-runtime` line as a side effect; no separate deletion step needed. |
| Referenced, zero `[[package]]` block | `beautifulsoup4` | Genuinely needed -- it's a real dependency of the already-correctly-resolved `paddleocr==2.10.0` package block. Not excludable; needs an actual resolved entry. |
| Referenced, zero `[[package]]` block | `scipy` | Referenced correctly (with explicit `version=`/`source=` disambiguation) from at least `openwakeword` and `albumentations`, but **no `[[package]]` block exists for either the 1.17.1 or 1.18.1 variant anywhere in the file.** Widely depended on; this alone likely blocks a large part of the ML dependency tree from ever resolving. |
| Entry exists, but contents belong to a DIFFERENT package | `scikit-learn` (declared version 1.9.0) | The `[[package]] name = "scikit-learn"` block's `sdist`/`wheels` are **all `safetensors-0.8.0` files** (urls, hashes, filenames) -- a real content swap, not a missing-entry gap. Caught by uv's own wheel-filename consistency check (`UV_SKIP_WHEEL_FILENAME_CHECK=1` bypasses the check but does not fix the underlying wrong data). |
| Referenced with a dead marker | `neutts` | `marker = "python_full_version < '3.14'"`, but this project's `environments` is scoped exclusively to `python_version >= '3.14'` -- this requirement can never actually apply within this lock's own scope. **CORRECTION (round 2): do NOT hand-delete this one** -- see the neutts-handling note below. |
| Confirmed real, a genuine third failure pattern (orphaned-but-internally-valid, distinct from "missing block" and "swapped content") | `openwakeword` | The **entire `[[package]] name = "openwakeword"` block still exists** (with its own real `dependencies = [{ name = "onnxruntime" }, { name = "tflite-runtime", ... }, ...]`), **and** the root `hermes-agent` package's own `[package.metadata] requires-dist` still lists `{ name = "openwakeword", marker = "extra == 'wake'", specifier = "==0.6.0" }` -- even though current `pyproject.toml`'s `wake` extra (directly read and confirmed) lists only `pyopen-wakeword`, `sherpa-onnx`/`sherpa-onnx-core`, `sentencepiece`, `pypinyin`, `pvporcupine`, `sounddevice`, `numpy` -- **no `openwakeword` at all**. Not a "missing block" or "swapped content" bug -- the block itself is internally self-consistent and valid, it's **orphaned cruft**: a fully-formed, correctly-resolved package current `pyproject.toml` no longer declares anywhere.

  **Provenance, corrected in round 3** (round 2 attributed this to the wrong commit -- `3cbd595c84`, 2026-09-14 -- caught by codex and independently re-verified directly): commit `7f1ddc70ce` (2026-09-07) genuinely dropped `openwakeword` first. The correct reintroduction point is `57dec7ade9c` (2026-09-22, "feat(gateway): swap Tesseract for PaddleOCR as primary image OCR engine" -- the SAME commit that added the paddlepaddle/paddleocr pins), confirmed by directly checking that commit's own `uv.lock`, which already contains `openwakeword`. `3cbd595c84`'s own `uv.lock` does **not** contain `openwakeword` at all -- that original attribution was wrong. The real mechanism is still the same general shape (a `uv lock` regeneration accompanying an unrelated feature commit reintroduced a stale sub-graph alongside the intended change), just pinned to the correct commit.

  **CORRECTION, round 3 (codex, verified directly against the repo)**: `onnxruntime==1.29.0` does **NOT** lack a cp314 wheel -- direct inspection of its `uv.lock` block shows cp311/cp312/cp313/**cp314** wheels for Linux aarch64/x86_64 and Windows. More importantly, `onnxruntime` is **not exclusively used by the orphaned `openwakeword` block** -- it has 5 total reference sites in the lock, 4 of them from genuinely live, currently-declared packages: `faster-whisper`, `kittentts` (x2), and `piper-tts`. **`onnxruntime`'s own package block must NOT be removed or treated as expected-to-disappear** -- only `openwakeword`'s block and its root metadata entry should be purged; `onnxruntime` stays exactly as-is, still needed by those other three consumers. |

**Completeness (round 2, narrowed to be accurate)**: a two-pass audit script
(pass 1: every `{ name = "X" }` reference vs. every `[[package]] name = "X"`
block, flagging references with none; pass 2: for every block, check whether
its sdist/wheel filename prefix matches its declared name) was independently
re-run (round 1 review, claude) across all 356 `[[package]]` blocks and
confirms the missing-block list (`beautifulsoup4`, `neutts`, `scipy`,
`tflite-runtime`) and the swapped-content list (`scikit-learn` only,
`ruamel-yaml`/`ruamel.yaml` being an expected dot-vs-dash naming artifact,
not corruption) are **exhaustive for those two specific failure patterns**.
**This does NOT cover the third pattern found in round 2** (an internally
self-consistent block/metadata entry that's simply orphaned -- no longer
declared by current `pyproject.toml` at all, like `openwakeword` above) --
that requires a different check entirely: cross-referencing every
`extra == 'X'`-marked entry in the root package's own
`metadata.requires-dist` against what `pyproject.toml`'s corresponding
`[project.optional-dependencies].X` actually lists today. **This third-pattern
audit has NOT yet been run exhaustively** -- `openwakeword` was found by
one specific, manual investigation thread (tracing why `tflite-runtime` was
referenced at all), not by a systematic sweep. Whether other orphaned
extras/packages exist beyond `openwakeword` is a genuinely open question
(see Open questions below), traced to a real, identified mechanism (commit
`57dec7ade9c`'s upstream-merge reintroducing a stale sub-graph) rather than
one-off manual edits, which somewhat changes the likely SHAPE of any further
undiscovered issues (look for other extras touched by the same 2026-09-22
merge commit, not random scattered edits).

## CORRECTION (round 2, after consensus review): the paddle "force-injection" claim was wrong

The first draft of this ticket claimed `paddlepaddle`/`paddleocr` were
"force-injected" via `override-dependencies` and appeared nowhere in
`[project.dependencies]` as literal text. **This was independently checked
during consensus review and is factually wrong.** They are plain, literal,
heavily-commented entries directly inside `dependencies = [` at
`pyproject.toml:228-229` (`"paddlepaddle==3.2.2; python_version >= '3.14'"`,
`"paddleocr==2.10.0; python_version >= '3.14'"`), landed via ordinary
commits `57dec7ade9c`/`fa3d32a957` on 2026-09-22 (confirmed via `git blame`)
— the exact incident date this ticket already cites for why the pin exists.
`override-dependencies` has zero paddle mentions. There is no
force-injection mechanism; `override-dependencies` genuinely cannot
override a **direct** project dependency (only transitive ones), which is
why replacing the pin with a never-true marker there had no effect — not
because of some special force-inject behavior, just because that mechanism
doesn't apply to direct dependencies at all. **The decision itself (leave
this pin alone) is still correct** — only the causal story about *why* it
looked stuck was wrong. Fixed here so a future reader doesn't go looking in
the wrong place and either miss the real pin or reintroduce the 2026-09-22
OCR bug.

More importantly: **paddleocr is not a dormant/unused feature.**
   `gateway/run_inbound.py`'s `_extract_images_text_with_paddleocr` is the
   **primary OCR engine for real inbound image messages** (business
   cards etc.), specifically added to fix a real 2026-09-22 incident where
   Tesseract (the current fallback engine) silently dropped legible printed
   text. Excluding paddlepaddle/paddleocr would not crash anything (Tesseract
   fallback exists), but would silently **regress OCR quality back to the
   exact bug this pin was added to fix**. The owner (this session,
   2026-09-28) explicitly decided to keep OCR working and NOT touch this
   pin -- any future work on this lockfile must leave paddlepaddle/paddleocr
   exactly as currently pinned (`paddlepaddle==3.2.2`, `paddleocr==2.10.0`,
   both `; python_version >= '3.14'`).

## Proposed fix (revised after consensus rounds 1 AND 2 -- not yet executed)

1. **`neutts`: do NOT hand-delete (round 2 final decision, both reviewers
   independently converged on this).** Its reference lives in the root
   `hermes-agent` package's own `metadata.requires-dist`, mirroring a real,
   still-current `pyproject.toml` optional-dependency declaration (just
   unreachable in this lock's `environments` scope) -- hand-editing it away
   would be removing accurate metadata, not fixing a bug. Leave this line
   exactly as-is; it is not part of what actually blocks `uv lock --check`
   once `tflite-runtime` and `openwakeword` (below) are handled, and
   whatever regenerates the rest of the metadata correctly (a real `uv lock`
   pass, once the file parses) is the right place for it to be
   reconciled, not a manual edit in this pass.
2. **Purge the orphaned `openwakeword` package block AND its root
   `metadata.requires-dist` entry** (this single step also removes the
   embedded `tflite-runtime` line automatically, per the round-3
   correction above -- no separate deletion step for it). Both are stale
   cruft from commit `57dec7ade9c` (2026-09-22), which reintroduced a
   sub-graph `7f1ddc70ce` (2026-09-07) had already correctly dropped;
   current `pyproject.toml`'s `wake` extra does not declare `openwakeword`
   at all (confirmed by direct read). Removing the metadata line here is
   different from the `neutts` case: `neutts` mirrors something
   `pyproject.toml` STILL declares, while `openwakeword` mirrors something
   `pyproject.toml` no longer declares at all -- so removing this specific
   metadata entry brings the lock back into sync with reality, rather than
   away from it. **`onnxruntime==1.29.0`'s own package block must NOT be
   touched** (round 3 correction, codex, verified directly): it has real
   cp314 wheels and is still genuinely required by 4 other lock entries
   (`faster-whisper`, `kittentts` x2, `piper-tts`) independent of
   `openwakeword` -- leave it exactly as-is.
3. For `beautifulsoup4` and `scipy` (both genuinely needed, zero existing
   resolution) and `scikit-learn` (real entry exists but its content is
   swapped-in `safetensors` data, and per consensus round 1 (claude) has
   **no `dependencies` array at all** -- its real deps, numpy/scipy/joblib/
   **threadpoolctl**, are completely absent from the lock): once (1)-(2)
   let the file parse far enough, run a **scoped**
   `uv lock --upgrade-package beautifulsoup4 --upgrade-package scipy
   --upgrade-package scikit-learn`. This is **not** a low-risk, fully
   isolated change -- per consensus round 1 (claude), because
   scikit-learn's real dependency edges don't exist in the lock today,
   this scoped upgrade is the **first time** those edges (numpy floor
   constraints, joblib, a brand-new threadpoolctl entry) enter the graph,
   not merely a version bump of something already resolved. Explicit
   acceptance gate: after running this, diff the *entire* lockfile against
   the pre-change version and treat any changed/added block outside
   `{beautifulsoup4, scipy, scikit-learn, numpy, joblib, threadpoolctl}
   removed (openwakeword, tflite-runtime)` as requiring explicit
   justification before accepting -- a new `threadpoolctl` block appearing
   is expected, not a red flag; **`onnxruntime` must NOT appear as removed
   or changed** (round 3 correction -- it's still needed elsewhere); if it
   does move, that's a bug in the fix, not an expected outcome.
4. Pre-flight audit -- **verified exhaustive for two of three known failure
   modes only** (consensus round 1, claude, independently re-ran this): the
   two-pass regex audit (pass 1: every `{ name = "X" }` reference vs every
   `[[package]] name = "X"` block, report references with none; pass 2:
   for every block, check whether its sdist/wheel filename prefix matches
   its declared name) confirms **exactly** `beautifulsoup4`, `neutts`,
   `scipy`, `tflite-runtime` have zero blocks, and **only** `scikit-learn`
   has swapped content, across all 356 `[[package]]` blocks. **Does NOT
   cover the third pattern** (orphaned-but-internally-valid entries like
   `openwakeword`) -- see the "Still open" item below for that. Run this
   audit script again after the fix to confirm it still holds.
5. **Still open, not yet resolved (consensus round 2, both reviewers)**:
   given `openwakeword` was found to be orphaned cruft from a specific,
   identified merge (`57dec7ade9c`, 2026-09-22), is anything ELSE touched by
   that same merge commit also orphaned in the same way? This needs a
   *different* audit than step 4's (cross-reference every `extra == 'X'`
   entry in root `metadata.requires-dist` against what `pyproject.toml`'s
   corresponding `[project.optional-dependencies].X` currently lists) --
   not yet run exhaustively. Not treated as a blocker for executing steps
   1-4 (which are narrowly scoped and independently justified), but must
   be documented as a known follow-up, not silently dropped.
6. Re-run the audit in (4) plus a real `hermes` invocation and a live
   OCR + wake-word (`pyopen-wakeword`) smoke test after the fix, not just
   an import check (consensus round 1, codex).
7. Do NOT do a full fresh `uv lock` (deleting the file and regenerating
   from scratch) -- confirmed this session that a full fresh resolve
   cannot reuse paddlepaddle's existing (aarch64-compatible but
   no-cp314-wheel) pin and hard-fails on it, which would force the
   OCR-regression trade-off the owner explicitly declined.

## Open questions for reviewers (round 2)

- **Resolved in round 1**: "is there a safer audit than one-error-at-a-time"
  -- yes, the two-pass script described in the Proposed fix's step 3;
  independently re-run and confirmed exhaustive for the missing-block and
  swapped-content patterns specifically.
- **Resolved in round 2**: "is hand-deleting `neutts` safe?" -- No, both
  reviewers converged: leave it alone, it mirrors a real, still-current
  `pyproject.toml` declaration and isn't actually part of what blocks
  parsing once `tflite-runtime`/`openwakeword` are handled.
- **Resolved in round 2, but with a real finding attached**: "is the REST
  of this lock trustworthy?" -- confirmed NOT fully: `openwakeword`
  (package block + root metadata) is genuinely orphaned cruft from commit
  `57dec7ade9c` (2026-09-22). Not treated as a blocker for the narrowly-
  scoped fix in steps 1-5 above, but tracked as step 6, a real, open,
  documented follow-up (not speculative anymore).
- **Still open**: is scoped `--upgrade-package` actually safe given
  scikit-learn's dependency edges (numpy/scipy/joblib/threadpoolctl) don't
  exist in the lock at all today, meaning this is really "first resolution
  of a whole subgraph," not "bump three existing pins"? What's the
  concrete blast-radius check beyond "diff and eyeball it"? (Not yet
  executed/verified as of round 2 -- next session's first real task.)
- **Still open (round 2, both reviewers)**: is there other orphaned cruft
  from the SAME `57dec7ade9c` merge, beyond `openwakeword`? Needs the
  reachability audit described in step 6 above; not yet run.
- Should `scikit-image`, `opencv-python`, and other ML-adjacent packages be
  proactively spot-checked for the same swapped-content pattern, even
  though the round-1/2 audits found none currently -- as a defense against
  a *future* recurrence of this same corruption class, not because one is
  currently suspected?

## Out of scope

- Touching `paddlepaddle`/`paddleocr`'s pin in any way (owner decision,
  2026-09-28: keep OCR working as-is).
- A full fresh `uv lock` regeneration (confirmed this session to force the
  OCR-regression trade-off; declined).
- Any other lockfile drift unrelated to the ML/OCR/audio cluster identified
  above.
