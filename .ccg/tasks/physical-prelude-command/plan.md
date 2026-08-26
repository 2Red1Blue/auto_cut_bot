# Physical prelude: implementation and ownership

Target: execute real physical detectors after a fresh generic Command claim,
commit exactly physical root / presentation probe / clock certificate, and
independently reread them without detector or ASR calls. This makes the next
local speech children possible; it does not activate the complete Runtime.
Follow task05 local-window-command-implementation-plan.md. No legacy imports,
fake Transcript/VAD, installed speech profile dependency or publication.

## Shared interface (frozen for parallel work)

Kernel owner creates `pipeline/physical_media_contract.py`:

- `PreparePhysicalMediaEvidenceRequest(parent: PrepareTimedMediaEvidenceRequest,
  physical_policy_sha256: str, max_evidence_bytes: int, max_metadata_bytes: int)`.
  Exact types/positive limits/canonical hashes. Parent is the existing typed
  Source/VLM handle container, not a succeeded future parent. Its policy hash
  need not equal physical policy; preserve full parent identity for provenance.
  No accepted ASR registry/profile resolution. Kernel-derived full-hash key.
- `ResolvedPreparePhysicalMediaEvidenceRequest(request, source:
  ResolvedPrepareTimedMediaEvidenceRequest)`; properties `request_hash`,
  `root_input_manifest_sha256`, `physical_root_id` derive from canonical request
  and exact reread probe with distinct physical-prelude-v1 domain. These values
  never reference their own future output/Receipt. Source fields use `.source`.
- `ProducedPhysicalMediaEvidence(physical_root: PhysicalRootMediaEvidence,
  calibration_bindings: tuple[CalibrationBinding, ...], producer_policy_json:
  str, producer_provenance_json: str)` with strict canonical bounded-at-command
  JSON. Policy hash is derived, never a second disagreeing caller value.
- Producer `.prepare(resolved, source: VerifiedMaterializedBlob)` returns that
  value. Exceptions reuse public `TimedMediaEvidenceProducerError`.

Provenance has exact fields: schema_version=`local-physical-producer-provenance-v1`,
source_provenance_sha256, producer_identities (frame/audio/shot/scene/visual/
subtitle in that order, existing ProducerIdentity mapping), tool_invocations
(existing ToolInvocationTrace mapping), tool_trace_sha256. Kernel verifies
closed types/order, trace hash, all six context/calibration/identity matches,
committed frame/audio detector hashes and source provenance. Physical policy
canonical JSON must hash to the frozen physical_policy_sha256. Physical root
must exactly match resolved root id/input hash/source/manifest/frame/audio.

## Kernel ownership — Claude worker B

Only new `physical_media_contract.py`,
`prepare_physical_media_evidence_command.py`, `committed_physical_media.py` in
Kernel pipeline, and `tests/pipeline/test_physical_media_prelude.py`.

Reuse `resolve_committed_timed_media_request` and generic Store APIs, including
`artifact_set_hash`, `read_committed_artifact_set` and materialization lease.
Do not copy private helpers or modify old commands/Store/migrations. Use only
needed structural Store Protocol methods; no speech resolver requirement.
Claim key derives from full canonical request identity. Source byte bound is
checked before materialization. Nonfresh returns before detector or source
materialization. Failed/running/unknown cannot silently become a fresh retry.

Stage one bounded root Blob; root member contains exact request mapping,
BlobRef/hash, producer policy/provenance and calibration bindings. Probe and
certificate are inline members. Enforce explicit metadata/member and evidence
byte caps before storage and before reads. Three ordered members have unique
full request-hash-based logical IDs and revision 1, canonical Job scope. No
parent future ref, episode-only collision, duplicated earlier revision, fourth
member or speech admission. Reuse existing certificate compiler (root owner
adds exact PhysicalRootMediaEvidence support; no dummy eight-set value).

Success commit is outside rejection catches. Known validation/materialization/
producer failures may write Receipt-only outcomes; ambiguous success commit
must propagate and reconcile on same-key replay, never overwrite success with
failed. Source leases close on every branch; no retry loop or provider recovery
journal claims here. Unexpected cancellation propagates leaving claim unknown.

Reader accepts exact request + succeeded outcome and explicit bounds, rereads
Source/VLM, generic exact Receipt/slot/set, verifies ordered three-member shape,
scope/IDs/revision and complete payload, reads only the root Blob under its Job,
revalidates policy/provenance/evidence and recomputes certificate from exact
probe. No latest lookup, source materialization or provider calls on read.
Refuse oversize metadata before parse; refuse declared root length before I/O;
verify actual length/hash after bounded read. Test real resolver with synthetic
Store records rather than monkeypatching ownership validation away.

Tests: success/replay/concurrent nonfresh; lease cleanup; oversize pre-I/O;
invalid root/probe/policy/calibration/source; rehashed foreign/tampered members;
ambiguous commit observed committed vs still-running without re-invocation;
unknown/failed same-key no redispatch. Pure fake Store only on Mac; actual
PostgreSQL race/restart acceptance remains desktop work.

## Producer ownership — Claude worker A

Only `auto_cut_bot/pipeline/media_preflight/port.py`, new
`physical_models.py`, new `physical_adapter.py`, and
`tests/pipeline/test_physical_media_preflight.py`.

Create a genuinely speech-free physical policy/request/result (six existing
ProducerCalibrationIdentity records); do not fill dummy speech-model/profile
fields or instantiate an eight-set request/bundle. Policy contains only explicit
physical analysis/detector thresholds, execution bounds and six calibrations.
Its hash excludes ASR/VAD endpoints/models/calibrations and candidate expansion.
Keep physical producer generation-policy hashes from their actual calibrations.

Factor the current physical region into one shared helper; old `prepare` uses
the same physical results then actual speech and final source rehash. New
`prepare_physical` runs physical only, verifies source before/after, builds new
root and exactly six identities. Reuse existing algorithms, no copied detector
pipeline. Broaden only physical helper annotations to exact old/new type unions.
Make default speech client construction lazy so physical-only execution needs
no speech endpoint/configuration. Existing old policy/request/value behavior
and hashes must remain unchanged; do not inspect unreachable old parsers.

`ClaimOwnedPhysicalMediaProducer(port, policy)` implements the frozen Kernel
interface above. It verifies lease BlobRef and frozen policy, constructs the
new physical request with resolved root id/hash, maps local typed errors, and
returns ProducedPhysicalMediaEvidence. No source ownership authorization here;
Kernel owns reread/claim/lease. Test synthetic runner and fake executable bytes,
speech spy must never be constructed/called on new path; detector errors stop,
source mutation rejected, old path regression. No actual FFmpeg/model/DB on Mac.

## Root ownership

`media/stage4_predecessor.py` and a new focused media test: allow exactly old
RootMediaEvidenceBundle or new PhysicalRootMediaEvidence for probe/certificate
derivation/replay, without relaxing any identity/clock/calibration checks.
Old speech admission continues requiring the old root. Same certificate schema
binds a genuinely different physical root hash; no acceptance conferred by type.
Root owns task metadata, docs, integrations/reviews, commits and push. Workers
must not edit each other's files, spawn workers, touch private config or commit.

Root also narrows the existing resolver's parameter annotation to the public
`CommittedMediaInputsStore` Protocol (only Source/VLM reads) in
`prepare_timed_media_evidence_command.py`. This changes no execution behavior;
the physical Store must not implement a fake speech-registry API to type-check.

## Validation and remaining scope

Scoped pytest, Ruff, BasedPyright; old preflight/root/certificate regression and
import firewall; independent read-only review then scoped commit/push. No new
framework or governance gate. Task05 stays in progress: local speech Commands,
mapped candidate admission, calibration bootstrap and Runtime activation remain.
