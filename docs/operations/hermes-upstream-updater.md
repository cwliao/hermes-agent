# Hermes upstream updater runbook

> **UPDATE 2026-09-10:** Two upstream syncs on 2026-09-09 and 2026-09-10
> (43 commits, then 329 commits) were done **manually** — plain `git fetch
> upstream main` + `git rebase upstream/main` + hand-resolved conflicts +
> `git push --force-with-lease origin main` + manual
> `scripts/release_snapshot.py` + a systemd drop-in swap — bypassing this
> runbook's tool entirely. Both syncs are recorded in AgentMemory
> (`projects/LLMINFRA/design-notes/LLMINFRA-design-hermes-deploy-procedure-202609-final.md`,
> `LLMINFRA-design-hermes-upstream-sync-202609-final.md`,
> `LLMINFRA-design-hermes-rebase-conflict-patterns-202609-final.md`).
>
> Separately, and predating both of those manual syncs: the tool's own
> daily preflight (run by the "Hermes upstream update guard" cron job,
> 04:30 Asia/Taipei) has been reporting `status: BLOCKED` /
> `STALE_REVIEW_CANDIDATE` since at least 2026-09-09 20:30 UTC, over an
> orphaned `refs/upstream/review/20260906-050337` ref with no matching
> candidate metadata found by its own scan. The tool does have a real
> prior track record (see `~/.hermes/hermes-upstream-state/candidates/*.json`,
> e.g. an applied candidate from 2026-09-06) — it isn't unused or
> decorative, just currently blocked and now also two manual force-pushes
> behind whatever state it last modeled. **Before relying on this tool
> again:** clean up the orphan ref per the "State and recovery matrix"
> below, then run a fresh `--mode review` preflight to re-baseline it
> against the current `origin/main`/`main` before trusting its next
> candidate.

## Contract

The updater uses **rebase-based review candidate + snapshot promotion**.
Review-only never deploys, restarts, or pushes. Apply never runs unless one
specific candidate is `APPROVED` and the operator supplies the matching
`HERMES_UPSTREAM_APPROVAL_TOKEN`.

The live release is immutable. A deploy must create a new release snapshot,
retain the previous release and drop-in, switch the effective systemd paths,
then verify both service health and the expected candidate SHA.

## Daily Telegram review alert

The Hermes cron job named "Hermes upstream update guard" runs every day at
04:30 (Asia/Taipei) and delivers its stdout to the configured Telegram chat.
It always performs review-only checks. When upstream has advanced, the message
only includes the upstream/candidate SHA and diff size, then reminds the
operator to use the code workflow manually.

Telegram is reminder-only: it never accepts approval, deploys, restarts, or
pushes. When there is no new upstream commit, the daily message reports that
state and confirms that no deployment action occurred.

## Inspect and review

Run the read-only gate first:

```bash
python scripts/hermes_upstream_preflight.py \
  --repo "$HOME/.hermes/hermes-agent" \
  --state-dir "$HOME/.hermes/hermes-upstream-state" \
  --mode review --json
```

Then create a candidate:

```bash
python scripts/hermes_upstream_review.py \
  --repo "$HOME/.hermes/hermes-agent" \
  --state-dir "$HOME/.hermes/hermes-upstream-state" \
  --json
```

The JSON output and
`$HERMES_HOME/hermes-upstream-state/candidates/<run_id>.json` are the source of
truth. Record `candidate_sha`, `release_id`, `source_sha`, `parent_sha`, and
`review_branch` before approving. Inspect the diff from `source_sha` to
`candidate_sha`; do not approve a candidate by branch name alone.

## Approve and apply

Approval is explicit and candidate-specific. Store only the SHA-256 hash of
the approval token in metadata as `approval.approval_token_sha256`; never put
the raw token in logs or files. The apply command defaults to dry-run:

```bash
export HERMES_UPSTREAM_APPROVAL_TOKEN='provided-out-of-band'
python scripts/hermes_upstream_apply.py \
  --repo "$HOME/.hermes/hermes-agent" \
  --state-dir "$HOME/.hermes/hermes-upstream-state" \
  --run-id <run_id> \
  --release-root "$HOME/.hermes/releases" \
  --systemd-dropin "$HOME/.config/systemd/user/hermes-gateway.service.d/<new-dropin>.conf" \
  --previous-release <known-good-release-path> \
  --previous-dropin <known-good-dropin-path>
```

Review the dry-run output, then add `--execute` only after the operator has
confirmed the exact SHA, release id, rollback paths, and service unit. The
successful output must show the immutable `release_path` and verified service
identity.

## State and recovery matrix

| State/code | Meaning | Operator action |
|---|---|---|
| `READY` | Read-only gates passed | Continue only in the requested phase |
| `LOCKED` | Another updater owns the lease | Wait, or inspect owner/pid; never delete a live lock |
| `DIRTY_WORKTREE` / `NON_MAIN_BRANCH` | Source is unsafe to rebase | Inspect and repair source, then rerun preflight |
| `REBASE_CONFLICT` | Candidate could not be replayed | Resolve intentionally or retry review; no apply is allowed |
| `STALE_REVIEW_CANDIDATE` | Candidate/ref/metadata is stale or incomplete | Inspect and regenerate; do not reuse old marker |
| `PUSH_REJECTED` | Remote rejected or recorded rejection | Reconcile `origin/main`, regenerate candidate, retry explicitly |
| `MARKER_SIGNATURE_MISMATCH` | Apply marker does not match metadata/SHA | Stop and rebuild the candidate/marker |
| `FAILED` | Fetch, build, restart, or health operation failed | Preserve logs and rollback to the previous known-good release |

On conflict or preflight failure, the runner leaves no temporary review
worktree, active lock, or new review ref. On apply failure, the runner restores
the previous drop-in and restarts the known-good service; the failed snapshot
and candidate metadata remain for audit.

## Verification evidence

Use the artifact path returned by the runner and verify the effective unit:

```bash
systemctl --user show hermes-gateway.service \
  -p ActiveState -p SubState -p MainPID -p WorkingDirectory \
  -p ExecStart -p DropInPaths
```

Never infer the live version from the development checkout's `HEAD`. A restart
without a changed effective release path is not a deployment.
