---
title: "DROPIN-COLLAPSE-001 — 26 superseded systemd drop-ins are an active trap, not clutter"
status: DESIGN_ONLY_NOT_IMPLEMENTED
date: 2026-08-19
type: ticket-design
ticket: DROPIN-COLLAPSE-001
target_repo: hermes-agent
base: 92b74b9fcb230d4459b7d0567850a930af3d1e7b
---

# DROPIN-COLLAPSE-001

Design and decision only. No implementation is authorized by this document.

## The problem

`~/.config/systemd/user/hermes-gateway.service.d/` holds 27 `.conf` files.
26 of them declare `PYTHONPATH`. systemd applies drop-ins in lexicographic
order and the last one wins, so exactly one of those 26 has any effect. The
other 25 are wrong answers sitting in the same directory as the right one,
in the same format, with no marking to distinguish them.

```
$ ls .../hermes-gateway.service.d/*.conf | wc -l        27
$ grep -l PYTHONPATH .../*.conf | wc -l                 26
```

The 27th file is `ssl-ca.conf`. It sets only `SSL_CERT_FILE`,
`REQUESTS_CA_BUNDLE`, and `CURL_CA_BUNDLE`, declares no `PYTHONPATH`, and is
therefore not part of the pin question at all. It is the one file in the
directory that is unambiguously still doing its job.

This is not untidiness. It caused a real misdiagnosis on 2026-08-19.

## The incident it caused

The peer session `klib-0f`, investigating what production was running, ran:

```
systemctl --user cat hermes-gateway.service | grep -iE "WorkingDirectory|ExecStart|Environment" | head -10
```

`systemctl cat` concatenates the unit file and every drop-in in order. The
`head -10` truncated inside `20-upstream-v2026.8.3.conf`, which names
`releases/v2026.8.11-t0160-8889a75e9d`. That value was reported as the
effective configuration.

It is named rather than anonymised at that agent's explicit request. Their
reasoning is worth recording, because it is an argument about evidence
rather than about ownership: an anonymous "another agent misread the
directory" cannot be checked by a reader, and reads like a hypothetical
constructed to support the conclusion — the exact criticism this ticket
would otherwise deserve. The incident is also already recorded under their
identity in the klib repo's ticket history, and two records disagreeing
about who did what is a worse problem for whoever reconciles them later
than any embarrassment avoided. They also asked that it not be softened,
on the grounds that the argument's force comes from the reader being
competent and the command being normal. It was not — `43-...-dd7a0164.conf` sorts after it
and had overridden it. The correct value was roughly 217 lines further down
in output the agent had already invoked.

The consequence was not academic. It produced a claim that a P0 security
control had been dropped by a specific restart at a specific time, which was
wrong on cause, duration, and attribution, and was on its way to that
agent's operator before a peer disputed it. Several message exchanges were
spent establishing which of two files in the same directory was real. (An
earlier draft said "three exchanges"; the exact count was not recorded at
the time and is not reconstructed here — the point is the cost, not the
integer.)

Correcting it required reading `/proc/<pid>/environ` — the kernel's record
of what the process actually received. That should not be the only reliable
way to answer "what is deployed".

### A second trap, which directory hygiene does not fix

The same agent compounded the error afterwards in a way worth recording
separately, because collapsing the drop-ins would not prevent it. Asked
whether the guard was gated on the pending deploy, they ran
`git grep impersonat origin/main` — **after their own PR #50 had merged the
guard into main** — found it, and reported "already on main independently,
so not gated on this deploy". They had used their own change as evidence
that the change was unnecessary.

That is a different failure from reading a superseded file: the artifact was
current and correct, and the reasoning error was temporal. It is included
here so that a reader who fixes the directory does not conclude the class of
error has been eliminated.

## Why the accumulation is the defect

The drop-in mechanism is working exactly as designed; no bug is being
reported against systemd. The defect is that the *directory* is a
high-confidence source of false answers:

- Every superseded file is syntactically valid and semantically plausible.
- Nothing in a file indicates whether it is live. Only its name's sort
  position relative to 26 others does.
- The tools people reach for first — `systemctl cat`, `grep`, opening a file
  — all present superseded values with the same authority as current ones.
- Truncation (`head`, a pager, a scrolled terminal) reliably lands on a
  superseded file, because 25 of 26 are superseded.

A second instance of the same hazard exists at smaller scale: `.bak-pre-*`
files kept alongside live config. systemd ignores them; a person or an agent
grepping the directory does not.

```
$ ls .../hermes-gateway.service.d/ | grep -c bak
5
$ ls .../hermes-gateway.service.d/*bak* | head -3
20-upstream-v2026.8.3.conf.bak-pre-codex-path-20260808124325
20-upstream-v2026.8.3.conf.bak-pre-t0105
20-upstream-v2026.8.3.conf.bak-pre-t0131
```

All five are backups of `20-upstream-v2026.8.3.conf` — the same file whose
superseded value caused the misdiagnosis above.

## Proposed direction (for review, not authorized)

1. **Collapse to one active drop-in.** A single file expressing the current
   pin. "What is deployed" becomes a one-file question with no ordering to
   reason about.
2. **Move the rest out of the drop-in directory entirely** — not renamed in
   place. The archive must not be readable as current configuration by
   anyone grepping the directory, which rules out `.bak` suffixes in the
   same folder. That distinction is the lesson from the `.bak-pre-*` files:
   the failure mode is not systemd loading the wrong file, it is a reader
   believing one.
3. **Record the release SHA in a comment inside the active drop-in**, so it
   can be cross-checked against `/proc/<pid>/environ` in one step. The gap
   between declared and running configuration is what the 2026-08-19
   incident turned on.

## Risks and what must not be done

- **Do not delete the release directories the archived drop-ins point at.**
  Two of them are registered git worktrees:

  ```
  $ git worktree list | grep releases
  ~/.hermes/releases/v2026.8.3-1c177e43b5         1c177e43b5 (detached HEAD)
  ~/.hermes/releases/v2026.8.3-t0123-edafc108ed   edafc108ed (detached HEAD)
  ```

  and a release directory is a rollback path. This is `PROD-DRIFT-DETECTION-001`'s sibling failure —
  deleting something because nothing appears to reference it, without
  checking what is running. Archiving a drop-in and pruning a release are
  separate decisions with separate evidence requirements.
- **The collapse must be verified from the live process, not the files.**
  After collapsing, `/proc/<pid>/environ` must show the same
  `HERMES_RELEASE_SHA` and `PYTHONPATH` as before. If it differs, the
  collapse changed behaviour and must be reverted.
- **Do this while the service is healthy and a rollback target exists**,
  not alongside another change. Currently drop-in 44 (`547f82d812`) is the
  rollback target for the live pin at 45 (`92b74b9fcb`); collapsing must
  preserve an equivalent path.

## Correction to an earlier statement

In a message to the peer agent, this author stated "45 files, 44 of them
wrong". That was wrong: 45 is the *index* of the current drop-in, not the
count. The count is 27 files, 26 declaring `PYTHONPATH`, 25 superseded. The
peer repeated the wrong figure back. It is corrected here because a ticket
about a directory of plausible-looking wrong answers should not itself
contain one.

## Open questions

- Should the archive live under `~/.hermes/deploy-backups/` (which already
  exists) or somewhere new? It should not be under
  `~/.config/systemd/user/`.
- Is there any consumer that reads the drop-in directory expecting the full
  history — a rollback script, a deploy tool, an audit? None was found, but
  none was searched for exhaustively, and that search is a precondition for
  moving anything.
- Do the `.bak-pre-*` files belong in the same sweep, or are they a separate
  cleanup with different owners?
