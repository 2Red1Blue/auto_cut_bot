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

## Consumer-read integration — ALLOW

A follow-up integration check found the first public anchor reader required
`CalibrationValidationBinding`, including measurement Receipt/Set UUIDs that
the local-run profile does not contain. The writer's full binding remains
mandatory for write/replay, but consumers must use only exact accepted
aggregate/validation references and the expected profile-source/registry
hashes. The Store must still revalidate the four members, successful validator
provenance and immutable anchor. It must not ask consumers to invent missing
measurement refs or fall back to a logical head. The API adjustment is now
implemented and tested; it changes no persisted schema or authority acceptance
rule. Store tests cover direct `LocalRunCalibration` consumption and thirteen
reference/hash/scope substitutions.

Final combined verification with the opt-in media PostgreSQL test explicitly
enabled: **324 passed, zero skipped**, in 7.67 seconds. Ruff and explicit source
basedpyright also passed. This includes one real PostgreSQL transaction
integration using synthetic native-output fixtures, not a real new FunASR
inference or a completed real HTTP Pipeline run.

Independent reviewer: `calibration_contract`, read-only diff and shared-closure
inspection, Ruff/basedpyright/diff checks; no new blocker. The public reader
uses only existing local-run fields, while writer replay retains its full
request binding. The temporary PostgreSQL verification services are stopped
after validation; no Podman application database is stopped or reset.

## Real shadow HTTP adapter — ALLOW after focused repair

The Pipeline now implements the existing Kernel measurement port using the
FunASR raw calibration endpoint. One frozen request and the complete source
binding set are checked before I/O. The original source owner and exact BlobRef
drive bounded Store materialization; the derived measurement Job cannot
impersonate that owner. Every dispatched outcome closes the verified lease.

The shared file transport disables environment proxies and redirects, bounds
response chunks and never retries. Uncertain transport/HTTP outcomes remain
unavailable for the existing command recovery path. Valid raw output is decoded
into an untrusted comparison projection; no adapter grants record acceptance.

Independent reviewer `calibration_migration` approved the transport, then found
two adapter details: compiler JSON encoding differed from the media/service
protocol, and same-Job source-owner substitution was not rejected. Both were
fixed and independently rechecked. Tests bind Unicode wire bytes to the exact
invocation hash, preserve the media protocol integer domain, reject same-Job
owners before I/O, and prove staged recovery/succeeded replay do not dispatch
or materialize twice.

Parent merged verification: **242 passed, zero skipped**, including a real
loopback HTTP upload with poisoned proxy settings and a service-to-adapter-to-
Kernel decoder round trip. Native models and source materialization in that
round trip are fixtures; this is NOT real SenseVoice/FSMN calibration or full
Pipeline completion. Ruff, source basedpyright and diff checks passed.

Commits: `5ac56f99` (shared transport) and `3fd97fd7` (owner-bound adapter).
No Kernel schema, migration, external publication path or user
configuration was changed in this slice. The controlled committed-source/
independent-anchor resolver, actual native calibration and deployment/runtime
integration remain pending.

## Locked service profile and committed calibration inputs — ALLOW

The pure service projector revalidates the existing shadow grammar and native
identity before producing the existing FunASR service configuration bytes.
The input resolver reads exact committed source handles, verifies original
identity bytes against the frozen corpus, derives only committed audio clocks,
and binds separately supplied ASR/VAD anchors to their frozen hashes. It performs
no materialization, inference, writes, authority publication or acceptance.

Independent reviewer: `calibration_migration`, separate read-only reviews of
both slices, ALLOW with no concrete defect. The 53 new focused cases passed.
Combined regression: **284 passed, 1 skipped** (the isolated PostgreSQL
verification instance was stopped for this run). Ruff, source basedpyright and diff checks
passed. Package-qualified test fixture imports changed no test assertions.

Service projector commit: `20de2095`. Real annotation assets, native calibration
and verified deployment loading remain outstanding; fixture tests do not
provide calibration truth.

## Measurement result reader and independent validation composition — ALLOW

Implemented the exact succeeded-outcome Store reader and a deployment-only
composition function. The Store recovers complete persisted references using
Job/slot/Receipt/Set/request/profile/registry closure, shares the existing pair
decoder and never resolves a logical head. The composition invokes the existing
measurement Command, reads that pair, then constructs the existing validator
binding from persisted references. Measurement and validation outcomes remain
separate; non-success stops before validation, while an unavailable Store read
propagates. Unknown native results require existing explicit successor recovery.

Implementer `calibration_contract` changed only Store code and Store tests.
Parent implemented the execution function and its 13 composition cases.
Independent reviewer `calibration_migration` separately reviewed both frozen
changes and returned ALLOW. Store-focused PostgreSQL verification: 83 passed,
zero skipped. Parent merged verification: **440 passed, zero skipped**,
including real isolated PostgreSQL transactions, shared input/HTTP/measurement/
validator regressions and raw evidence decoding. Ruff, explicit source
basedpyright and diff checks passed. The verification PostgreSQL was stopped
after the run; no application/Podman database was reset or stopped.

The composition tests execute the real resolver and both Commands with
synthetic native bytes and an in-memory Store. They are not real FunASR
inference, actual corpus calibration or a completed real HTTP Pipeline.
No native model, source profile, authority lock, ordinary CLI, Runtime or user
configuration was modified or activated.

Next code integration gap: Git-verified deployment profile/registry compilation
and installed-resource loading under `08-25-lock-real-test-authority-profiles`.
Existing authority-task authorization covers the proposed tools/registry/test
paths; freeze a concrete slice before implementing. This is distinct from
publishing real sources or obtaining independent real annotation data.
