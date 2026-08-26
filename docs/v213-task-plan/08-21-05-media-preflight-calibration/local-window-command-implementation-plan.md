# Local-window Command implementation plan

This task05 plan follows [candidate-local I/O](candidate-local-io-followup.md)
and the implemented [window wire](local-speech-window-wire.md), checkpoint
`d1845f5f`. Claude drafted the initial plan; root corrected the clock,
ownership, retry and commit-reconciliation issues below before implementation.
It is a target design, not evidence of a completed local Pipeline.

## 1. Target flow and unchanged responsibilities

Committed Source/VLM + source-global physical evidence
→ Kernel plans a candidate video window
→ verified presentation map + audio sample membership derive an audio request
→ claim-owned local speech child measures exactly that request
→ immutable raw/report/projection + terminal Receipt
→ Kernel assessment either closes the candidate or commits a larger successor
→ exact episode/batch readback
→ Stage4 independently validates the final candidate's admitted evidence.

VLM remains the sole story/highlight semantic input. ASR/VAD only protect
speech cuts. Neither provider response nor this child grants physical edit or
publication permission. Preserve the explicit full-source v1 path and its old
bytes; never silently reinterpret old evidence as candidate-local.

## 2. Physical-root contract — first implementation batch

New `media/physical_root.py` defines immutable
`PhysicalRootMediaEvidence(CanonicalEvidence)`:

- `physical_root_id`, `source_id`, `source_sha256`;
- `source_manifest_sha256`, `root_input_manifest_sha256`;
- the exact existing `frame_pts_index`, `shot_boundaries`,
  `scene_boundaries`, `audio_sample_boundaries`, `visual_validity`,
  `subtitle_cues` values.

No Transcript/VAD, accepted status or fabricated silence. Require exact set
types, same source identity and complete coverage; all video sets share the
same original clock/time base/origin/duration. Shot/scene must bind the exact
frame hash and their boundaries must belong to that index. Audio keeps its own
source clock; unequal A/V tails are valid and do not imply a mapping.

Use a separate `physical_root_codec.py` with public six-set decoders and
`decode_media_evidence_json`. Reject missing/extra keys, coercion, duplicate
JSON and unbounded reads. All six set hashes remain unchanged; the aggregate
gets a genuinely new hash. The old eight-set `RootMediaEvidenceBundle` stays
unchanged. Do not construct a dummy old root with unmeasured empty speech.

Probe/certificate references belong to a dedicated physical-prelude
ArtifactSet and Command binding. This pure DTO does not authenticate them.

**Commit order:** the parent first executes a separately claimed
`PreparePhysicalMediaEvidence@2.1.3` prelude over exact committed Source/probe
facts and frozen physical detector policies. It atomically commits physical
root, presentation probe and clock-map certificate as three immutable members,
without invoking ASR/VAD. Only after that succeeded Receipt exists may window
children resolve those exact references and invoke local speech. Prelude
identity derives from the parent input/Source/policy bindings, never from a
future parent success Receipt or set. A failed/unknown prelude prevents all
window calls. Its successful replay calls no detector again.

## 3. Clock and coverage closure

`CandidateEvidenceWindow.current_range` is on the **video** source clock.
`LocalAudioWindowSpec.requested_range` is on the **audio** source clock.
Their integer values must not be compared or assigned directly.

Before a child claim can invoke the producer, Kernel resolves the exact
committed presentation probe/certificate and AudioSampleBoundarySet. Map the
whole candidate interval through all relevant certificate segments, verify
continuous proven coverage, and select containing actual audio sample
boundaries under explicit outward-rounding/extension policy. Bind the
certificate, physical-root and audio-index hashes and both ranges in the
request. An unmappable gap/tail is indeterminate, not identity or stretching.

An outward audio extraction may be wider than the mapped candidate range.
The assessment compares speech coverage/touch against the mapped interval on
the original audio clock, with calibrated margins; it does not subtract stream
origins independently or claim sentence completeness from word gaps.

The adaptive boundary-touch margin is measured on the video clock and must be
mapped using exact presentation arithmetic before adding ASR/VAD timing-error
bounds and the audio snap allowance. Audio snap calibration alone cannot stand
in for speech timestamp accuracy. Validate ASR/VAD calibration roles separately,
not merely their unordered set. Include utterance-gap protected segments as well
as word ranges. Suppress a source-edge touch only when both the video and audio
extraction endpoints equal their respective source endpoints. Policy guard
points need not be decoded frames; they are measurement intervals inside the
already proved continuous span, not newly authorized physical cut endpoints.

The current `CandidateTimedEvidenceSet` carries per-candidate speech, but
`timed_evidence.py::_coverage_contains_range` still directly compares rational
native times. Therefore nonidentity/piecewise local coverage is **not** proved
by merely replacing its Transcript/VAD fields. Introduce an explicit local
candidate representation/decoder binding the verified mapping, or an explicit
versioned extension that validates the same relationship. Update the exact
readers and physical guard consumers together. Preserve v1 validation; do not
relax it to arbitrary interval containment.

## 4. Child request and persisted members

Proposed command: `PrepareLocalSpeechWindowChild@2.1.3`. Its producer port is
one single-dispatch operation over a caller-owned verified source lease and
the existing `LocalSpeechWindowRequest`; use the current HTTP adapter result
(raw bytes + projected evidence), not another provider-specific DTO grammar.

The closed child request must persist enough canonical data for independent
reread, not just caller assertions:

| Group | Required binding |
| --- | --- |
| Job/lifecycle | exact Job, canonical scope, parent Prepare request hash, episode, full candidate hash, expansion ordinal, attempt ordinal |
| Source/VLM | original source BlobRef and exact Source receipt/set/member/slot references; `CommittedSemanticInputsRequest` and exact WindowManifest/VLM observation identity |
| Physical | exact committed physical-root, probe/certificate and AudioSampleBoundarySet references/hashes |
| Window | full `CandidateEvidenceWindow`, derived `LocalAudioWindowSpec`, prior expansion result reference or explicit null |
| Retry | exact preceding BUSY Receipt reference or explicit null; frozen maximum attempts and fixed lifecycle identity |
| Profile | installed registry/profile identities, independent ASR/VAD calibration references, actual decoder identity and `LocalSpeechWindowPolicy` |
| Limits | explicit source/extraction/response/projected-JSON/cumulative-read limits; no limit inferred from the source-file cap |

Reuse existing typed Source/VLM request/reference models from
`prepare_timed_media_evidence_command.py`. No arbitrary path, source owner,
latest-head lookup or caller-selected accepted profile. A pure resolver
rereads these exact references and derives the wire request; it never trusts
an externally supplied `binding_sha256` as authorization.

Derive the idempotency key from the full canonical lifecycle hash plus
expansion/attempt ordinals. Do not truncate candidate hashes or let callers
pick a new key to evade deduplication. The request hash additionally binds
complete predecessor/profile/limit data. The first ordinal has no expansion
predecessor; later ones require the exact previous accepted measurement and
deterministically replayed expansion transition.

One succeeded child commits exactly three members:

1. `local_speech_window_report`: complete canonical child/wire request,
   exact predecessor references and measured extraction report.
2. `local_speech_window_projection`: canonical Transcript/SpeechActivity
   values and original response hash.
3. `local_speech_window_raw_response`: exact immutable raw BlobRef,
   response hash/length and wire-request hash.

Logical IDs include the full lifecycle identity, expansion and attempt.
Projected JSON has its own explicit byte bound; it can exceed raw native JSON
size, so `max_response_bytes` alone is not a valid projection budget.

## 5. Execution, retry and transaction rules

Resolve/validate committed identity before provider work. A fresh generic
Command claim is necessary for materialization/invocation. Any existing claim
returns or resolves its existing outcome before I/O. Use the original
verified source owner and the current bounded private-file lease.

Separate the invocation, staging and terminal commit phases:

1. Invoke exactly once through the local speech port. Independently decode and
   project the returned raw bytes; reject inconsistent producer projections.
2. Stage immutable raw/report/projection Blobs with bounded allocations.
3. Atomically commit the exact member set and succeeded Receipt through the
   existing Store transaction.
4. Release the source lease on success, rejection, failure or cancellation.

A known pre-commit invalid result may commit a denied Receipt; an explicit
unavailable result may commit a failed Receipt. **Do not put the success
commit inside a broad catch that writes another rejection.** If success commit
raises with unknown transaction outcome, propagate/reconcile the authoritative
slot/Receipt on retry. It may already have committed.

Recovery meanings are distinct:

- Succeeded same-key replay reads identical committed bytes with zero HTTP.
- A concurrent non-fresh running claim returns in-progress/indeterminate;
  it need not already have the winner's terminal Receipt.
- Transport timeout/disconnect, worker death or cancellation after possible
  dispatch does not authorize a new invocation. Same key does not redispatch;
  a fresh attempt key cannot bypass the unknown state.
- Only an exact, terminal, predecessor-bound BUSY result that proves no model
  invocation may authorize the next contiguous attempt under the frozen
  budget. Derive the successor key in Kernel and verify the predecessor before
  claim. Two callers choosing that same successor obtain one fresh claim.
- A larger window is expansion, not retry. It requires a completed measurement,
  a recomputed touching/insufficient assessment and a deterministic next range.
- Expansion or retry exhaustion rejects the parent. A failed/omitted candidate
  cannot become a successful partial episode or batch.

Ordinary claim/Blob/Receipt operations can support this fail-closed first
child. They **do not** provide restart-resumable staging, durable invocation
leases or automatic reconciliation of lost native results. If those are
required, add a separately designed normal-window journal/migration, not the
calibration-only or text-generation recovery tables. Do not claim full durable
recovery from a fake-store test.

## 6. Exact readers, admission and integration changes

`read_committed_local_speech_window_child` takes the exact succeeded outcome
and expected immutable binding. Resolve its exact Receipt/slot/set/member
closure; read bounded raw bytes by exact BlobRef, never materialize the original
source or call a provider. Re-decode/re-project and compare the persisted
request/report/projection hashes and actual payloads. Enforce cumulative read
budgets across all windows, not just each Blob.

The episode reader walks every candidate/expansion/attempt in order and
replays mapping, predecessor, assessment and expansion transitions. It must
prove the complete candidate set and exactly one final admitted measurement
for each candidate. Missing or extra successors, rehashed foreign responses
and mixed strategies reject.

**Root decision: per-window speech admission.** Use a distinct
`LocalSpeechWindowAdmission@1` binding that window's original source clock,
range, response/projection, both producer/calibration identities and physical
root/map. An episode index lists exact final-window admission references; it
does not fabricate a source-wide transcript. Keep old
`TimedSpeechProfileAdmission@1` readable. Physical Stage4 Admission remains a
separate decision.

**Persistence owner:** the window child remains exactly three measurement
members and never writes a fourth admission member. After independently
replaying the children, the parent episode Prepare command creates one
standalone local speech admission Artifact for each candidate's final window.
These admissions commit atomically in the parent's ArtifactSet with two
base members: a local-timed-media manifest referencing the already succeeded
physical prelude and complete child chain, and the mapped candidate index.
The expected set is exactly those two plus N final-window
admissions, with N and logical IDs derived from the complete committed VLM
candidate set, not a caller-supplied count. The index identifies each admission
within that same parent set by type/logical ID/revision/content hash (not a
self-referential parent set hash) and binds its exact child measurement reference.
Consumers resolve standalone admission references only from the succeeded
parent set; prior expansion measurements have no fabricated admission refs.
Failed parent validation commits no admission set. The parent does not copy
the prelude members under their existing logical IDs/revisions and does not
make child requests reference future parent artifacts. This ownership/order
rule closes the independent review's admission-reference and cyclic-predecessor
Warnings.

| Owner seam | Required change |
| --- | --- |
| `media/physical_root*.py` | new six-set pure value/codec; old root untouched |
| `media/timed_evidence.py` and local candidate codec | mapped local coverage/assessment representation, explicit original-clock semantics |
| `media/stage4_predecessor.py` | distinct local speech admission and independent replay |
| `pipeline/prepare_physical_media_evidence_command.py` | source-bound physical prelude commits before any window child |
| `pipeline/prepare_local_speech_window_child_command.py` | closed resolution/claim/invocation/staging/terminal behavior |
| `pipeline/prepare_timed_media_evidence_command.py` | explicit local strategy, physical producer then real child loop; no root-derived fake re-inference |
| `pipeline/committed_timed_media.py` | exact strategy-aware local-chain reader |
| `pipeline/finalize_timed_media_evidence_batch_command.py` | exact versioned child shape, all candidates required |
| `pipeline/media_preflight/port.py` | physical-only production without whole-source ASR; thin local HTTP producer |
| `pipeline/runtime/media_preflight_stage.py`, `composition.py` | inject frozen local dependencies/limits, explicitly select local strategy |
| Store/model/migrations | first verify generic claim/key/member contracts; no invented table or unsupported no-migration guarantee |

Current generic tables may suffice for the fail-closed child. Strategy in a
request hash is necessary but is not proof that retry reservations or new
reader member shapes need no database work. Verify actual migrations and
transaction races before declaring that conclusion. No speculative activation
table/column is part of this plan.

## 7. Calibration activation — newly identified required seam

Independent review found no service-level execution deadlock: normal service
profile parsing only checks calibration hash syntax and positive bounds; real
acceptance is in `InstalledLocalRunProfileResolver` and its exact Store anchor
reader. Supplying arbitrary normal calibration hashes is not valid bootstrap.

Current shadow measurement requires full-source range and calls
`run_inference(original_file)`; the new normal window route calls
`run_window_inference` through local extraction. A full-source measurement
does not calibrate the new path merely because model weights match.

Before real local activation, implement an explicit versioned **shadow-local**
measurement mode reusing the exact extraction/report/projector and original
raw outputs. Its pre-calibration identity binds models, decoder, source/window,
policy and independent window anchors, but excludes the not-yet-produced
accepted Record and measured bounds. Preserve normal window route validation.

The pure shadow-local case/projector is implemented in `media/shadow_local_calibration.py`
and `media/shadow_local_calibration_projection.py`: closed case content produces
the request binding, then the existing window decoder/projector replays actual
raw bytes against independently authored ordered local anchors. Case identity
distinguishes the complete service-profile hash from its nested native identity.
Zero measured error and explicit empty observations remain valid measurements;
neither is coerced into a fabricated positive bound or an accepted Record.
This does not activate a service route or make the old full-source calibration
command/reader accept local responses.
The next explicit service/profile slice is specified in the
[shadow-local service plan](shadow-local-service-implementation-plan.md).

Persist the versioned local manifest/results → independently replay window
anchors into an **unaccepted** local validation report → define a separate
local acceptance/activation grammar → compile/install a normal local profile.
The existing complete-source `CalibrationRecord`/anchor cannot represent this
multi-source, multi-time-base evidence and must not be reused. Never put a
future accepted hash in its own measured identity, reuse fake bounds or feed
the new window envelope into the old full-source decoder.

## 8. Parallel implementation and acceptance

Implemented/reviewed checkpoints: six-set physical root/codec (f0a7b5c4),
certificate support (f2c59f35), claim-owned physical prelude/producer/readback
(49179042), physical-root mapping (f0dc57b3), native audio facts (3ff0d690), and
the pure mapped local-audio request factory, request-bound pre-dispatch BUSY
proof (a4bbb44e; 195 synthetic/loopback checks, zero skips), and exact terminal
Receipt reader (2b7f4b3f; 203 pure Store checks, independently reviewed),
Kernel-owned single-dispatch delivery port (697d13f4; 199 synthetic/loopback
checks), local lifecycle and resolved Source facts (f99682a8; 127 new pure cases),
shadow-local case/raw projection (0001b165; 150 new pure cases), and mapped
window assessment (15 new cases; independently reviewed). Root's combined
foundation/Store/Source/window regression is 923 passed. These are not complete
Runtime activation or real detector/model/DB acceptance.

Claude quota is exhausted; existing native workers now implement in disjoint
files and cross-review frozen results. Root owns integration, task metadata
and scoped commits. The next batch closes local speech child persistence,
then parent-owned admissions and exact episode readers.
Runtime wiring follows those APIs. Shadow-local calibration is a separate
producer/validator slice and cannot be folded into ordinary window Commands.

Required checks:

1. Physical DTO has six original sets/hashes, no unrun speech; v1 unchanged.
2. Nonidentity/piecewise A/V mapping, unequal tails, outward sample rounding
   and gaps verify whole interval coverage, not numerical tick equality.
3. Two-worker same-key race invokes once; the loser may see running, then both
   reread the same eventual terminal Receipt. Do not assert premature success.
4. Simulated loss after dispatch and ambiguous success commit never trigger
   redispatch or an overriding failed Receipt; arbitrary attempt-2 is rejected.
5. Exact BUSY predecessor permits only one contiguous bounded successor;
   skipped attempts, changed policy and exhausted budgets reject.
6. A touching initial measurement causes a second actual local request; no
   touching yields one. ASR words do not prove full-sentence completeness.
7. Raw/request/projection/owner/map/predecessor mutation and missing candidates
   reject on independent replay; successful replay calls no providers.
8. Bounded reads and source/WAV leases clean up on all paths; invalid or missing
   evidence cannot produce a successful partial batch.
9. Real local calibration exercises the same extraction path and independently
   authored anchors; placeholder normal-profile hashes never grant acceptance.
10. Pure/fake tests are labelled; real models, PostgreSQL races/restart and
    native codecs are verified separately on the desktop before completion.
