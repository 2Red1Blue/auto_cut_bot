# Desktop branch and runtime runbook

## Goal

Provide one honest desktop runbook for checking out the authoritative
`auto_cut_bot` development branch, starting isolated Podman PostgreSQL and
FunASR infrastructure, and distinguishing runnable infrastructure from the
still-blocked real Pipeline bootstrap.

## Confirmed facts

- The executable repository is `2Red1Blue/auto_cut_bot` on
  `feat/v213-contract-codegen`; `ac_auto_cut` is not a runtime dependency.
- SenseVoiceSmall and FSMN-VAD run in one FunASR service on loopback port
  18765; the filled `.env`, model snapshots and Profile are not Git content.
- The standard HTTP composition currently requires an injected verified
  `AuthorityRegistrySnapshot`; no supported desktop bootstrap command exists.
- Kernel SQL migrations are tracked, but there is no operator-facing durable
  migration ledger/runner for safely upgrading an existing database.

## Requirements

- Cover new clone and safe switch/update of an existing clone without
  overwriting local work.
- Use a dedicated `autocut` database and persistent Podman volume; never point
  to the legacy database or delete an existing volume.
- Provide new-empty-database migration commands and explicitly forbid replaying
  all SQL against an existing database.
- Provide Podman Compose commands for the tracked SenseVoiceSmall/FSMN service.
- Keep secrets, media, model weights, generated Profile and local config out of Git.
- State exactly which checks can run now and why the real HTTP video Pipeline
  remains fail closed.

## Acceptance Criteria

- [ ] Every referenced branch, path, command, port and test exists in the
  current candidate tree or is explicitly marked as a future prerequisite.
- [ ] The runbook cannot overwrite an uncommitted checkout, replay all
  migrations into an existing database, use legacy services, or expose secrets.
- [ ] The runbook is linked from the status handoff and documentation index;
  its linked FunASR probe uses the correct host port.
- [ ] An independent reviewer checks the candidate and `git diff --check`
  passes before a docs-only commit is pushed to
  `origin/feat/v213-contract-codegen`.

## Out of scope

- Implementing Authority bootstrap, Calibration, missing Stage code, Render/QC
  or external publication.
