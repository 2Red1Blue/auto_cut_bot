# Calibration Phase-2 slice review

Date: 2026-08-26

## Measurement-v3 predecessor — ALLOW

The committed manifest now preserves the complete canonical raw context and
expected-anchor reference beside each invocation and immutable raw BlobRef.
Finalization compares canonical serialized plan/member values, rejecting
bool/int and float/int equality drift. The results member binds the v3 manifest
hash. Native invocation and recovery transitions are unchanged.

Independent reviewer: `review_calibration_contract`, separate context/run.
Verification: 13 focused measurement tests passed; Ruff and basedpyright passed.
Eight configured PostgreSQL tests skipped because `127.0.0.1:5433` was
unavailable. This is predecessor-code approval, not real-DB acceptance.

## CalibrationRecord contract — ALLOW

The first review denied canonical-byte ambiguity, accepted-record construction
from public caller claims, reused anchor/observation/evidence identities, and
source vocabulary/model mismatches. Repairs were independently verified:

- Exact canonical input bytes and closed payloads.
- Candidate-only public facade; internal accepted assembly grants no authority.
- Individual anchor/observation and evidence identity uniqueness.
- `registry_snapshot_sha256`, `producer_kind`, exact SenseVoiceSmall/FSMN IDs.
- Kernel-owned bound-algorithm hash and distinct ASR/VAD identities.
- Nonnegative range construction matching the decoder, including 90 round trips.
- Exact four-member order/scope/logical IDs/revision and positive-bound/hash closure.

Independent reviewer: `review_calibration_contract`, separate context/run.
Verification: 50 focused tests passed; full media suite 159 passed, 1 skipped;
Ruff and basedpyright passed. This is contract-slice approval only. Independent
raw-byte validation, protected Store writer and database closure remain pending.

## Migration 0017 — ALLOW after repair

Independent PostgreSQL probes found nullable JSON checks, rejected/orphan sets,
Job-finalization incompatibility, nested identity mismatch and invalid test
fixtures. The migration now checks nested identity fields with null-safe
comparisons, rejects orphan/later sets, freezes authority Job identity and
implements a narrow validator finalization rule without changing ordinary
Pipeline finalization. Parent independently inspected the SQL and executed
25 migration/run-migration checks on disposable PostgreSQL 16. The migration
suite includes 19 cases; no real application schemas were dropped.

Commit: `a9f98f96`.

## Persisted raw mapping and independent derivation — ALLOW

The media layer now decodes closed persisted invocation/context mappings,
exposes a provider-free raw derivation API and retains the old strict claimed
projection wrapper. Canonical mapping round trips and equality with the
measurement producer's existing encoding are covered. Parent reviewed the
diff; the combined media/measurement/validator check at this checkpoint ran
247 passed, 1 skipped; Ruff and source basedpyright passed.

Commit: `47a50f58`.

## Independent validator command — ALLOW for code/fixture integration

Independent reviewer `calibration_contract` identified and then verified fixes
for deployment byte ceilings, unavailable-vs-invalid classification and typed
profile grammar validation. All metadata/request closure precedes raw reads.
The validator reconstructs the entire measurement request, decodes original
SenseVoice/FSMN bytes, compares projections and aggregate statistics, and
qualifies source-local match IDs. No native/provider port is available to it.

Verification: 21 tests passed, including PostgreSQL measurement persistence →
independent validation → four-member set/anchor/terminal Job → exact receipt
replay. Ruff and source basedpyright passed. The independent reviewer also ran
10 targeted negative probes. Raw inputs in these tests are synthetic fixtures;
this is not evidence of a new real-native calibration or ready authority
deployment. Those remain unfinished.

## Store integration and expanded recovery regression

Dedicated writer/reader implementation has a separate read-only review by
`calibration_migration`. The wider database run exposed an existing denial
result mismatch caused by PostgreSQL JSONB formatting. The command now returns
the same transaction's durable outcome, retaining complete result equality.
Recovery fixtures no longer accidentally expire both member and recovery
leases; they still assert the active-recovery rejection before expiring the
specific verification lease. Native recovery behavior was not relaxed.

Merged verification: **310 passed, 1 skipped**, including media contracts,
measurement/validator commands, Store persistence, migration, recovery and
ordinary-run migration regressions. Database cases ran against disposable
PostgreSQL 16, not skipped. Ruff and explicit source basedpyright passed.

Commit: `5ce28fb6` (Store + independent validator + recovery correction).

## Consumer-read integration — repair in progress

A follow-up integration check found the first public anchor reader required
`CalibrationValidationBinding`, including measurement Receipt/Set UUIDs that
the local-run profile does not contain. The writer's full binding remains
mandatory for write/replay, but consumers must use only exact accepted
aggregate/validation references and the expected profile-source/registry
hashes. The Store must still revalidate the four members, successful validator
provenance and immutable anchor. It must not ask consumers to invent missing
measurement refs or fall back to a logical head. This API adjustment is in
progress; it changes no persisted schema or authority acceptance rule.
