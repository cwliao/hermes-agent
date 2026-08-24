# KANBAN-COMPLETION-EVIDENCE-PATH-001

Status: IMPLEMENTED_REVIEWED_NOT_DEPLOYED

## Incident

A completed Kanban summary claimed both `/tmp/selected_jokes.txt` and
`/root/MessageRequests/SelectedAutumnJoke.txt`. The first file existed; the
second did not. The completion path accepted the prose and the gateway
silently skipped the missing attachment, allowing an unverified deliverable
claim to reach the user.

## Design decision

Completion evidence is fail-closed at the Kanban database boundary, before the
task state write:

- Common file-like paths in `summary` and `result` are checked when they have
  an absolute or home-relative form and a known file suffix.
- Existing explicit `metadata.artifacts` delivery semantics are preserved:
  managed scratch artifacts still use the existing preservation gate, while
  optional external paths may still be skipped by the notifier if they vanish
  after completion.
- A candidate must be a readable regular file. Missing paths reject the
  completion and leave the task in-flight so the worker can retry with a
  corrected handoff.
- Existing scratch-artifact preservation remains in force; this adds a
  verification gate and does not replace attachment staging.

## Acceptance criteria

- [x] Existing `/tmp/*.txt` evidence is accepted.
- [x] Missing `/root/*.txt` evidence rejects completion before the DB state
      becomes `done`.
- [x] Existing mixed artifact delivery behavior remains compatible.
- [x] Ordinary prose without a file-like path remains unaffected.
- [x] Focused Kanban tests pass.
- [x] Cross-review records no unresolved correctness issue.
- [ ] Deployment and live Telegram verification are pending a separate
      explicit deployment request.

## Cross-review record

Independent review covered the database, tool, CLI, and dashboard completion
callers. Missing prose-referenced files now fail closed without changing task
state; already-terminal tasks return a structured terminal error instead of
re-validating stale evidence; and direct CLI/dashboard callers convert the
evidence error into a recoverable response. Existing explicit artifact staging
semantics remain unchanged.
