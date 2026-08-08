# Release Bake SOP (DGX)

This documents the manual procedure used to deploy hermes-agent to the
DGX Spark production host. **It is fully manual — there is no CI/CD
pipeline or build script that does this for you.** Every deploy, someone
runs these commands by hand, in this order. (Origin: T0129 first traced
and named this mechanism; T0138 and T0140 each independently walked the
full procedure live and verified it works; this doc unifies what those
three tickets separately rediscovered.)

## Context

Production does **not** run from a git checkout directly. The systemd
unit `hermes-gateway.service` has a drop-in
(`~/.config/systemd/user/hermes-gateway.service.d/20-upstream-v2026.8.3.conf`)
that overrides `WorkingDirectory` and `Environment=PYTHONPATH` to point
at a **release directory** — a plain `rsync`d snapshot with no `.git`,
under `~/.hermes/releases/<version>-<label>-<hash>/`.

That snapshot is produced from a dedicated worktree,
`~/.hermes/worktrees/T0127-v2026.8.3`, which tracks the real production
branch: **`ticket/T0127-v2026.8.3-merged`** — not `main`, and not
`feature/klib-orchestration-integration` (a different, divergent branch
despite the similar name).

A **separate** checkout, `~/.hermes/hermes-agent`, is used only by
`hermes_calendar_guard.sh` and a few scripts to read a git fingerprint —
it is not part of the bake procedure below and must be kept in sync
separately (see "Known gaps" at the end).

## Gotcha: DGX's `origin` remote has a narrow fetch refspec

`git config --get-all remote.origin.fetch` on DGX returns only
`+refs/heads/main:refs/remotes/origin/main` (documented in T0135). A bare

```bash
git fetch origin ticket/T0127-v2026.8.3-merged
```

will **not** create a `origin/ticket/T0127-v2026.8.3-merged`
remote-tracking ref — it fails to resolve the branch with no useful
error. Always pass the explicit refspec (see step 1 below).

## Procedure

### 1. Update the worktree

```bash
cd ~/.hermes/worktrees/T0127-v2026.8.3
git fetch origin ticket/T0127-v2026.8.3-merged:refs/remotes/origin/ticket/T0127-v2026.8.3-merged
git merge --ff-only origin/ticket/T0127-v2026.8.3-merged
git log --oneline -1   # confirm you landed on the commit you expect
```

### 2. Bake a new release directory

```bash
NEW_HASH="$(git rev-parse --short HEAD)"
RELEASE_DIR=~/.hermes/releases/v2026.8.3-<label>-${NEW_HASH}
# <label> is a short, descriptive tag, e.g. a ticket number (t0140, t0138, ...)

rsync -a --exclude=.git --exclude=__pycache__ --exclude=.venv --exclude=venv \
  ~/.hermes/worktrees/T0127-v2026.8.3/ "$RELEASE_DIR/"
```

### 3. Write the `RELEASE_COMMIT` marker (added by T0140)

```bash
git -C ~/.hermes/worktrees/T0127-v2026.8.3 rev-parse HEAD > /tmp/.release-commit.tmp
mv /tmp/.release-commit.tmp "$RELEASE_DIR/RELEASE_COMMIT"
```

The `mv` (rather than writing the file directly inside `$RELEASE_DIR`)
matters: a same-filesystem `mv` is atomic, so a reader either sees no
file yet or the complete marker — never a half-written one, even if it
reads while step 2's `rsync` is still copying other files. This marker
is what makes `hermes_calendar_guard.sh`'s stale-code detection work —
without it, the check silently no-ops (see T0140 for the full history of
why the old git-checkout-based fingerprint permanently broke once
production moved to release-dir snapshots).

Verify: `cat "$RELEASE_DIR/RELEASE_COMMIT"` should print the same commit
hash you rsynced in step 2.

### 4. Back up the current drop-in conf

```bash
DROPIN=~/.config/systemd/user/hermes-gateway.service.d/20-upstream-v2026.8.3.conf
BACKUP_DIR=~/.hermes/backups/deploy-v2026.8.3-<label>-$(date +%Y%m%d%H%M%S)
mkdir -p "$BACKUP_DIR"
cp "$DROPIN" "$BACKUP_DIR/"
```

### 5. Point the drop-in at the new release

*This step's exact command is a reconstruction, not a literal quote from
any prior ticket — T0138/T0140 describe doing this but don't record the
edit command verbatim. The pattern below is the one actually used live
today (2026-08-08) for both T0138 and T0140's deploys.*

```bash
sed -i "s#$(grep -oP '(?<=WorkingDirectory=).*' "$DROPIN")#${RELEASE_DIR}#g" "$DROPIN"
cat "$DROPIN"   # confirm both WorkingDirectory= and Environment=PYTHONPATH= now point at $RELEASE_DIR
```

The single `sed` substitutes the *old* release-dir path for the *new*
one wherever it appears in the file — since both `WorkingDirectory=` and
`Environment=PYTHONPATH=` already pointed at the same old path, one
substitution updates both lines together. Verify both lines actually
changed by inspecting the file (`cat`) before proceeding; if only one
line updated, something about the old file's formatting didn't match
and the two lines are now inconsistent (the gateway would boot code from
one directory but resolve Python imports from another).

### 6. Reload and restart

```bash
systemctl --user daemon-reload
systemctl --user restart hermes-gateway.service
sleep 6
systemctl --user is-active hermes-gateway.service    # must print "active"
journalctl --user -u hermes-gateway.service --since -1min --no-pager \
  | grep -iE "error|traceback|exception"              # must print nothing
```

### 7. Verify the marker took effect

```bash
cat ~/.hermes/gateway_boot_fingerprint
```

Must show `release:<the new commit hash>` — not a frozen old value from
a previous boot.

### 8. Run the health check

```bash
bash ~/.hermes/scripts/hermes_calendar_guard.sh
```

No output = healthy (this script's convention is "silent means fine,
only print when there's a real issue"). Any "Gateway is running stale
code" line means one of steps 3-7 was skipped or done wrong.

## Rollback

Restore the drop-in conf backed up in step 4, then repeat step 6's
restart:

```bash
cp "$BACKUP_DIR/$(basename "$DROPIN")" "$DROPIN"
systemctl --user daemon-reload && systemctl --user restart hermes-gateway.service
```

The old release directory is never deleted by this procedure, so
rollback just points the drop-in back at it — no need to re-bake.

## Known gaps / non-goals

- **Fully manual.** No automation exists for any of the above; this doc
  only records the procedure, it doesn't script it (deliberately — see
  T0129, which found no existing tooling and decided documenting the
  manual steps was the right scope, not building automation).
- **`~/.hermes/hermes-agent` is a separate checkout**, used only by
  `hermes_calendar_guard.sh`/some scripts to read a git fingerprint. It
  is not touched by this bake procedure and has drifted out of sync with
  production before (T0140 found it on the wrong branch entirely). As of
  2026-08-08 it is checked out at a **detached HEAD** pointing at the
  correct commit — not a branch name — because the branch name
  `ticket/T0127-v2026.8.3-merged` is already checked out in the
  `~/.hermes/worktrees/T0127-v2026.8.3` worktree, and git does not allow
  the same branch checked out in two worktrees at once. Keeping this
  checkout current is a separate manual step, not covered here.
- **Release directory naming (`<version>-<label>-<hash>`) is a human
  convention, not enforced by anything** — nothing parses or depends on
  this exact format; it exists purely for human readability when
  listing `~/.hermes/releases/`.
