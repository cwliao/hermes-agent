# KANBAN-TERMINAL-WORKER-STOP-001

Status: IMPLEMENTED_REVIEWED_NOT_DEPLOYED

## Incident

After task `t_fd0032f3` had already completed successfully, its worker
conversation continued issuing `kanban_complete` and `kanban_block` against
the terminal task. The dispatcher did not spawn a duplicate run: the same
worker session continued after the successful lifecycle mutation. With
`tool_loop_guardrails.hard_stop_enabled: false`, the repeated deterministic
failure was only warned about and could consume the remaining turn budget.

## Root cause

The Kanban tool result was appended and persisted, but the conversation loop
had no runtime receipt that a successful `kanban_complete` or `kanban_block`
was the worker's terminal handoff. It therefore asked the model for another
assistant turn. Terminal-state errors were also treated as ordinary tool
failures and depended on the optional global hard-stop policy.

## Implementation

- Successful Kanban lifecycle results (`ok=true`) set a turn-local terminal
  receipt on the agent.
- After tool-result persistence, `conversation_loop.py` exits the worker turn
  with `turn_exit_reason=kanban_terminal_success`; no follow-up model API call
  is made.
- Terminal-state `kanban_complete`/`kanban_block` errors carry a structured
  marker and unconditionally halt the tool loop, including interactive/soft
  guardrail configurations.
- The receipt is reset at the start of every conversation turn, so a cached
  gateway agent remains usable for later work.

## Acceptance criteria

- [x] A successful `kanban_complete` or `kanban_block` stops the current worker
      before another model call.
- [x] A terminal-state mutation failure halts even when
      `hard_stop_enabled=false`.
- [x] A malformed or rejected non-terminal completion does not set the
      terminal-success receipt and remains retryable.
- [x] The receipt does not leak into the next user turn.
- [x] Focused and integration tests pass.
- [x] Cross-review records no unresolved correctness issue.
- [ ] Deployment and live Telegram verification are pending a separate
      explicit deployment request.

## Cross-review record

Independent review found and the implementation corrected four boundary cases:
same-batch tool-call draining after a terminal mutation, dispatcher-owned worker
scope, budget-boundary terminal-success accounting, and terminal-state races
where a stale worker submitted completion evidence after another worker had
already finished the task. The focused runtime/guardrail suite passed, and the
broader Kanban/DB/Notifier suite passed with `98 passed, 1 skipped`.
