# Upstream rebase 007 (2026-10-01)

- Branch: upstream-rebase-007-20261001, base upstream/main 357f51c491, 464 fork commits + 5 kanban port commits.
- Rollback: tag backup/pre-upstream-rebase-007-20261001 (old main a4f7c2889b).
- Kanban: upstream split kanban_db into kanban_db_{connect,dispatch,workspace,notify}; fork callers repointed; 7 fork functions restored (gc/dispatch, swarm auto-subscribe, goal mode, dispatch passthrough, kanban_tools).
- Validation: kanban tests 33 failed / 847 passed (old main 55 failed). tui_gateway+doctor failures match pure upstream/main (home-io-guard worktree false positives, KeyError 'result', 'tmp' profile).
- Known: test_kanban_credential_startup_exit order-dependent when run after test_kanban_cli_dispatch_passthrough (upstream-inherent).
- Open: uv.lock not regenerated (paddlepaddle==3.2.2 pin blocks full `uv lock`); not landed or deployed. Landing requires apply pipeline + deployment-layer verification.
