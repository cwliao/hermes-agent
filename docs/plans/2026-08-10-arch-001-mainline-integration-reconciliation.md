# HERMES-ARCH-001-MAINLINE-001: Reconcile release branch and mainline integration

**Status:** `NEEDS_RECONCILIATION` — ticket drafted and Codex-reviewed;
implementation and merge are not authorized by this ticket yet.

## Objective

Create a clean, reviewable path for bringing ARCH-001 into the canonical
Hermes `main` branch without merging the entire DGX release history, unrelated
branch work, or Claude's live-checkout changes.

## Canonical topology

- Repository: `cwliao/hermes-agent`.
- Canonical mainline: GitHub `origin/main`, currently `7fa1865f7`.
- DGX live checkout: `/home/cwliao/.hermes/hermes-agent` on `main`, currently
  `7fa1865f7`; it has three pre-existing untracked files and must not be
  cleaned, reset, or edited by this ticket.
- DGX release staging branch: `ticket/T0127-v2026.8.3-merged`.
- GitHub release branch currently resolves to `8ef05d5a3`, while DGX's local
  worktree/ref was observed at `058e9da17`; reconcile this drift before using
  that branch for a new bake.
- Deployed ARCH-001 snapshot remains
  `v2026.8.3-arch-001-f0dd130c8`, sourced from implementation commit
  `f0dd130c8`.

## Evidence and risks

1. `main`, DGX `origin/main`, and GitHub `main` agree at `7fa1865f7`; main is
   not itself divergent.
2. The current ARCH-001 branch is not a clean mainline-sized branch: it is
   about 5,143 commits ahead of `origin/main` because it carries the release
   history.
3. The branch also contains a later merge commit (`cf7252a52`) beyond the
   deployed ARCH-001 commit. That merge includes unrelated skill-command and
   gateway/test changes and must be classified before any integration.
4. The DGX release branch/ref mismatch means a branch name alone is not enough
   provenance for a future release bake.
5. `upstream/main` is a separate NousResearch history and must not be merged
   into Hermes `origin/main` as part of this work.

## Scope

- Freeze and record the exact GitHub and DGX refs before integration.
- Create a fresh integration worktree from Hermes `origin/main`; do not reuse
  the Claude-owned live checkout or the current release worktree.
- Isolate the ARCH-001 change set from `f0dd130c8` and its intended parent
  `ab1a40040`, then classify every file as ARCH-001, required compatibility,
  release baseline, unrelated, or excluded.
- Re-run focused tests plus the mainline-compatible regression suite in the
  clean worktree.
- Produce a path-correct review packet and a merge-ready PR targeting Hermes
  `main`.
- After merge, re-bake the DGX release from the merged `main` commit and
  re-run runtime, health, and rollback evidence.

## Non-goals and safety boundaries

- Do not reset, clean, force-push, or edit `/home/cwliao/.hermes/hermes-agent`.
- Do not merge `ticket/T0127-v2026.8.3-merged` wholesale into `main`.
- Do not merge `cf7252a52` or any unrelated branch work without classification
  and separate review.
- Do not change the running service or current release while preparing the
  integration packet.
- Do not start ARCH-002 implementation until ARCH-001 is reviewed, merged to
  `main`, and re-baked from that mainline commit.

## Acceptance criteria

- [ ] GitHub `main`, the selected base commit, the ARCH-001 commit range, and
      the release branch refs are recorded with full SHA evidence.
- [ ] A clean worktree is based directly on Hermes `origin/main`.
- [ ] The proposed diff contains only ARCH-001 and explicitly approved
      compatibility/documentation changes; no release-history flood or
      unrelated `cf7252a52` changes remain.
- [ ] ARCH-001 focused tests, mainline regression tests, lint/compile checks,
      and `git diff --check` pass in the clean worktree.
- [ ] The review packet cites existing files and exact lines in that worktree.
- [ ] Independent review reaches consensus or records an explicit blocker.
- [ ] User approves publication; a PR is merged into Hermes `main`.
- [ ] A new release is baked from the merged mainline SHA and runtime,
      fingerprint, health, and rollback evidence are recorded.

## Review result

Codex review: `NEEDS_RECONCILIATION`, not `READY`. The mainline itself is
healthy, but direct merging of the current ARCH branch is unsafe because of
release-history inclusion, the later unrelated merge, and DGX release-ref
drift. The next review must inspect the clean main-based change set rather
than the current branch summary.

Claude/AGY review: not dispatched. Dispatch requires the user's explicit
approval with the objective, scope, environment, and expected review output.
