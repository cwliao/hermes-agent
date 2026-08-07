# Deployment integrity tools

These tools are local/DGX-triggered, report-only deployment checks. They do
not modify business logic, switch systemd services, deploy, or roll back.

- `bake_release.py SOURCE RELEASE` — AC1, general bake content verification
  plus `.release-manifest.json` creation.
- `check_push_provenance.py COMMIT` — AC2, general origin push provenance only.
- `audit_security_fixes.py` — AC3, security-only registry and running-process
  content audit.

Run each command with `--help` for the scope and exit-code details. The
mandatory trigger points and evidence-retention rules are in
`docs/deployment-sop.md`.

