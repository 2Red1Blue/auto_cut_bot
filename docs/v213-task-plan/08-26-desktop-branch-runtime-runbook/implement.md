# Implementation

1. Audit current branches, remotes, deployment files, CLI entry point,
   migrations and HTTP composition.
2. Add `docs/v213-desktop-e2e-runbook.md` with safe desktop commands and
   explicit fail-closed boundaries.
3. Link it from `docs/v213-current-status-and-desktop-run.md` and
   `docs/README.md`; correct the linked FunASR host-port example.
4. Run path/command checks, Markdown reference inspection and
   `git diff --check`.
5. Obtain a read-only adversarial review focused on destructive Git/Podman
   behavior, migration replay, secrets, legacy confusion and false GO claims.
6. Repair findings, stage only the four documentation files, commit and push
   the current development branch.
