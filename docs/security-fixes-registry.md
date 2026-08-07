# Security fixes registry

This registry is the AC3, security-only content check. AC1 and AC2 are broader
deployment checks: AC1 verifies that any baked release contains exactly the
source worktree content, and AC2 verifies that a commit has provenance on the
remote. None of the three checks deploys, repairs, rolls back, or restarts a
service. This registry does not claim to audit ordinary, non-security feature
gaps.

The owner is responsible for adding every known P0/P1 security fix and for
reviewing the independent ticket-directory scan reported by the audit tool.
Fingerprints intentionally use a function or constant definition line. The
tool requires the line to be a Python code line, which avoids matching a
comment or string literal without attempting a full AST analysis.

Owner: Hermes maintainers  
Last updated: 2026-08-07

| Ticket | File | Fingerprint | Owner | Last updated |
| --- | --- | --- | --- | --- |
| T0101 | `run_agent.py` | `def _has_content_after_think_block(self, content: str) -> bool:` | Hermes maintainers | 2026-08-07 |
| T0104 | `gateway/run.py` | `_GATEWAY_SELF_IMPERSONATION_RE = re.compile(` | Hermes maintainers | 2026-08-07 |
| T0105 | `gateway/run.py` | `def _is_entirely_bracket_wrapped(text: str) -> bool:` | Hermes maintainers | 2026-08-07 |

T0101 is retained here because the ticket explicitly requires the known
T0101/T0104/T0105 case set, even though its source ticket currently labels it
P2 and its implementation is in `run_agent.py`. The registry scanner uses the
actual file and definition line above rather than assuming all three live in
`gateway/run.py`.

The independent registry-gap scan searches `T*.md` under the configured
`--tickets-dir` for P0/P1 tickets whose Objective or Why it matters mentions
security/safety (including the Chinese security terms). Its default is
`$HOME/project/klib/.ai/tickets`; pass `--tickets-dir` explicitly when the
ticket repository is elsewhere. An unregistered ticket is a report-only
warning, but an unavailable scan is `undetermined`.

