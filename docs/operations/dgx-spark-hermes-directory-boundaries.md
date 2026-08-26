# DGX Spark Hermes Directory Boundaries

This document is the authoritative directory map for the Hermes installation
on host `55-0940189-03`. It exists to prevent agents from treating source,
deployment artifacts, dependencies, and live state as one interchangeable
directory tree.

Host-level recursive instruction markers are installed at:

- `/home/cwliao/.hermes/AGENTS.md`
- `/home/cwliao/.hermes/releases/AGENTS.md`
- `/home/cwliao/.hermes/venvs/AGENTS.md`
- `/home/cwliao/.hermes/worktrees/AGENTS.md`
- `/home/cwliao/.hermes/deploy-staging/AGENTS.md`
- `/home/cwliao/.hermes/deploy-worktrees/AGENTS.md`

The tracked repo copy is the recovery source if a host-level marker is lost.

## Directory map

| Role | Path | Mutable by | Must not be used as |
|---|---|---|---|
| Development repo | `/home/cwliao/.hermes/hermes-agent` | Git development and reviewed source changes | Proof of what production currently runs |
| Development worktrees | `/home/cwliao/.hermes/worktrees/<worktree>` | Isolated branch/task work | Production runtime or durable state |
| Deployment worktrees/staging | `/home/cwliao/.hermes/deploy-worktrees`, `/home/cwliao/.hermes/deploy-staging` | Deployment tooling only | Canonical source, active runtime, or durable state |
| Release snapshots | `/home/cwliao/.hermes/releases/<release-id>` | Deployment tooling creates a new snapshot | Development checkout; never edit an active snapshot in place |
| Release venvs | `/home/cwliao/.hermes/venvs/gateway-<sha>` | Deployment tooling creates/manages a SHA-pinned environment | General development venv or cross-release dependency pool |
| Live config/state root | `/home/cwliao/.hermes` | Hermes runtime and explicitly approved operations | Git repo, disposable cache, or cleanup target |
| User systemd definitions | `/home/cwliao/.config/systemd/user` | Explicitly approved deployment/operations changes | Source of truth without checking effective merged properties |

## Non-negotiable invariants

1. **Repo update is not deployment.** A fetch, merge, commit, checkout, or
   fast-forward under `hermes-agent` changes development source only.
2. **Service restart is not deployment.** Restarting a release-based service
   loads the same release again unless its effective systemd paths changed.
3. **Production identity comes from the process and systemd.** Verify
   `WorkingDirectory`, `ExecStart`, interpreter path, process cwd, and Git
   SHA/tree of the release before claiming which code is live.
4. **Release snapshots are immutable.** Never patch, cherry-pick, install into,
   or clean an active release directory. Create and validate a new release.
5. **Venvs are release-pinned.** A gateway release and its
   `gateway-<sha>` venv are one deployment unit. Do not silently reuse or
   mutate a different release's venv.
6. **Live state is not source.** `config.yaml`, databases, locks, logs,
   sessions, credentials, and provider/platform state under `~/.hermes`
   must not be moved into Git or removed by repository cleanup.
7. **Untracked does not mean unused.** Before deleting or moving any untracked
   path, inspect effective systemd units/drop-ins, timers, cron, and live
   process paths.
8. **Preserve rollback.** A deployment must retain the previous release,
   previous venv, systemd drop-in backup, and config backup until post-deploy
   validation succeeds.

## Current production identity

Recorded on 2026-08-23 (Asia/Taipei):

- Source tree published on `origin/main`:
  `d113236d36c193d36ca8a184ce139c88ba69c439`
- Production code release:
  `/home/cwliao/.hermes/releases/v2026.8.23-upstream-state-safety-cb7b23d4120d`
- Production gateway venv:
  `/home/cwliao/.hermes/venvs/gateway-cb7b23d4120d`
- Release source commit:
  `cb7b23d4120d419bb1cc2b7a096e5a4e0572004f`

The publication merge `d113236d36...` and deployed source
`cb7b23d412...` had the same Git tree when documented. This is historical
evidence, not a permanent alias: always re-check current effective paths and
tree SHAs.

## Required verification commands

Use read-only checks first:

```bash
git -C /home/cwliao/.hermes/hermes-agent status -sb
git -C /home/cwliao/.hermes/hermes-agent rev-parse HEAD origin/main
systemctl --user show hermes-gateway.service \
  -p ActiveState -p SubState -p MainPID -p WorkingDirectory -p ExecStart \
  -p DropInPaths
ps -fp <MainPID>
```

For any auxiliary service or timer, inspect the effective unit rather than a
single base file:

```bash
systemctl --user cat <unit>
systemctl --user show <unit> \
  -p ActiveState -p SubState -p WorkingDirectory -p ExecStart -p DropInPaths
systemctl --user list-timers --all
crontab -l
```

Do not print `config.yaml`, `.env`, auth files, tokens, or credential
contents while performing these checks.

## Development-repo artifacts

`.bytecode-fingerprint` and `.web_ui_build.lock` are generated in the
development checkout by update/web-build coordination code. They are
intentionally ignored. Preserve them unless the owning code's documented
recovery procedure specifically requires replacement.

Their presence does not mean the development repo is the production runtime.

## Known boundary debt

Kanban ticket `t_51218e56` (recreated 2026-08-27 after the original
`t_44c44870` was accidentally archived during an unrelated bulk
kanban-board cleanup — see the addendum below) tracks the migration of
auxiliary systemd services/timers that still reference the mutable
development checkout. Until that ticket is implemented and explicitly
approved:

- treat those references as known technical debt, not the desired topology;
- do not restart or rewrite production-facing units opportunistically;
- avoid changing the checkout while a directly coupled timer/service is
  executing;
- retain backups and cross-review every migration.

## Addendum (2026-08-27): `hermes update` can silently no-op after a direct local commit

A same-host session committed fixes directly to this development checkout
(via a dispatched agent working in-place, then `git commit`/`git push` from
this checkout), then ran `hermes update --yes` expecting it to rebuild the
release and restart the gateway. It printed `✓ Already up to date! [main @
<sha>]` and did nothing — no new release snapshot, no restart. Two
subsequent `systemctl --user restart hermes-gateway.service` calls both
restarted the *same stale release*, and this went undetected for a while
because "service active (running)" was checked without also checking
`WorkingDirectory`/`HERMES_RELEASE_SHA` against the intended commit.

**Why**: `hermes update`'s up-to-date check is based on its own `git
fetch` finding no new commits to pull — not on comparing the currently
running release's SHA to the checkout's HEAD. A commit made directly in
this checkout (not pulled in by `hermes update` itself) is invisible to
that check. `hermes update --plan` (read-only) correctly analyzes the
target SHA and affected services; the false negative is specific to the
real `--yes` run's fetch-delta shortcut.

**If you land a fix directly in this checkout, do not trust `hermes
update` to deploy it. Rebuild the release manually:**

```bash
cd /home/cwliao/.hermes/hermes-agent
FULL_SHA=$(git rev-parse HEAD)
NEW_RELEASE="/home/cwliao/.hermes/releases/v$(date -u +%Y.%-m.%-d)-<slug>-${FULL_SHA:0:10}"
venv/bin/python3 scripts/release_snapshot.py /home/cwliao/.hermes/hermes-agent "$NEW_RELEASE" "$FULL_SHA"
grep -n "<marker unique to your fix>" "$NEW_RELEASE/<path/to/changed_file.py>"   # confirm the fix landed in the snapshot BEFORE switching traffic to it

# New systemd drop-in, sorted to win: copy the current highest-sorting file in
# ~/.config/systemd/user/hermes-gateway.service.d/ (this repo's convention
# uses an increasing run of leading z's, e.g. zzzzzzzzzzzz-<slug>-<sha10>.conf),
# change only WorkingDirectory / Environment=PYTHONPATH / Environment=HERMES_RELEASE_SHA
# to $NEW_RELEASE / $FULL_SHA, then:
systemctl --user daemon-reload
systemctl --user restart hermes-gateway.service
systemctl --user show hermes-gateway.service -p WorkingDirectory -p Environment --no-pager \
  | grep -E "WorkingDirectory|HERMES_RELEASE_SHA"   # verify against the intended sha -- "active (running)" alone is not proof
```

Cross-reference: [[GLOBAL-conventions-deployment-directory-boundaries-2026Q3-final]]
in the AgentMemory global layer carries the same addendum with the full
incident narrative.
