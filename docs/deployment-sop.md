# Hermes deployment integrity SOP

This SOP wraps the existing manual rsync baked-release workflow; it does not
replace it with CI/CD or a background daemon. The scripts live in this
`hermes-agent` repository and are intended to run on the DGX host, where the
release directories and the user systemd drop-in are available.

The checks have distinct scopes:

- AC1 (`scripts/deploy-integrity/bake_release.py`) is a general check for every
  deployment. It runs rsync, compares source and release content by checksum,
  and writes `.release-manifest.json` only after success.
- AC2 (`scripts/deploy-integrity/check_push_provenance.py`) is a general
  provenance check. It fetches `origin` and answers only whether a commit is
  present on an origin branch; it does not claim that production runs that
  commit.
- AC3 (`scripts/deploy-integrity/audit_security_fixes.py`) is a security-only
  check. It reads the systemd `WorkingDirectory`, cross-checks the running
  process `/proc/<pid>/cwd`, and scans the registered security-fix definition
  fingerprints. It is report-only and never repairs or restarts anything.

## Mandatory trigger points

1. **After every release bake/rsync:** run AC1, and retain its stdout and the
   release `.release-manifest.json` as evidence in the deployment ticket. Do
   not record the bake as successful if the command exits non-zero or does not
   produce the manifest.

   ```bash
   python scripts/deploy-integrity/bake_release.py \
     /absolute/path/to/hermes-agent \
     /absolute/path/to/releases/<label>
   ```

2. **After every systemd drop-in switch to a new release:** run AC3 and retain
   its JSON stdout plus stderr warnings in the deployment ticket. Exit `0`
   means every registered fix is present and systemd agrees with the running
   process; exit `1` means the result is undetermined; exit `2` means a fix is
   missing or the systemd/process directories disagree.

   ```bash
   python scripts/deploy-integrity/audit_security_fixes.py \
     --tickets-dir "$HOME/project/klib/.ai/tickets" \
     > security-audit-$(date -u +%Y%m%dT%H%M%SZ).json
   ```

3. **After any rollback or re-bake of an older version:** treat it as a new
   bake event. Run AC1 for the resulting release and AC3 after the drop-in is
   switched, retaining both pieces of evidence.

Before a KAIOS ticket is marked DONE, run AC2 for the claimed commit and attach
its output. Adding a P0/P1 security ticket also requires registering its
definition fingerprint in `docs/security-fixes-registry.md`; the AC3 tool's
independent ticket scan reports likely omissions for manual confirmation.

The KAIOS ticket directory is not part of this repository. The AC3 tool accepts
`--tickets-dir` and defaults to `$HOME/project/klib/.ai/tickets`; a missing or
unreadable directory is reported as `undetermined`, not silently treated as an
empty registry.

