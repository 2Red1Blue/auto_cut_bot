# Durable local measurement requirements

## Goal

Execute the already frozen shadow-local measurement corpus once per durable
command identity, stage original response bytes and independently replayed
projection atomically, then expose an exact succeeded-set reader. This remains
unaccepted evidence: it cannot activate a normal profile or mutate an authority
calibration anchor.

## Required properties

- New command, protocol, Store plan/methods and additive migration are siblings
  of the old full-source route; old grammar and migrations remain unchanged.
- The durable plan closes ordered manifest, pre-calibration local service
  profile, request/case hashes, exact source BlobRef owner/identity and byte
  limits before dispatch.
- Recovery is fail-closed: lease-before-dispatch, atomic stage of raw BlobRef
  plus derived result, no re-dispatch after stage, unknown invoking is
  indeterminate, and retry needs explicit authorization.
- Exactly two succeeded artifacts are finalized in one set: local manifest and
  local results. Their reader checks command/job/receipt/set/scope/member
  identities, ordered coverage, BlobRef metadata and every local-domain hash.
- Invalid raw evidence yields a terminal denial without a result set;
  unavailable/unknown execution is not rewritten as denial.
- No migration or code path writes `calibration_record*`, registry anchors,
  installed profiles or a publish decision.

## Required verification

- Pure/fake store recovery and reader tests cover replay, concurrency, stage
  crash, invoking unknown, explicit retry, race, source/profile/case/raw/owner
  substitution and no authority-side effect.
- Migration has static/integration checks; a later desktop/Postgres run proves
  real CAS/transaction behavior before production activation.
