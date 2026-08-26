# Design — Locked Shadow Calibration and Local Run Profiles

```text
protected Git source -> shadow calibration profile -> CalibrationRecord
  -> protected run profile -> verified RegistrySet snapshot
  -> authority-only PostgreSQL bootstrap anchor -> HTTP runtime injection
  -> local-only Pipeline work
```

Pipeline HTTP and ordinary configuration never choose a profile. Credentials
remain deployment secrets; authority source contains only identities and hashes.

## Profile states

`shadow_calibration_v1` freezes the selected model/detector identities,
calibration corpus/window policy, source-clock rules, word-gap/VAD merge policy
and non-zero acceptance criteria. It may invoke calibration but cannot enable
HTTP media-preflight.

`local_run_v1` is a successor source profile. It repeats the frozen policy and
adds the exact CalibrationRecord member hash. Only this state is compiled into
a RegistrySet, bootstrapped into the authority Store and injected into runtime.
New calibration means a new profile version, never an anchor mutation.

## Calibration authority boundary

The existing `calibration_record_sha256` consumer field is not a
CalibrationRecord. Before a run profile can exist, the Kernel must own a
closed immutable CalibrationRecord member and independent validation receipt.
The record binds the shadow-profile source/RegistrySet identity, immutable
corpus members and anchors, source clocks, the exact native ASR and VAD model
trees/service bytes/versions, every measurement artifact, the deterministic
bound calculation and strictly-positive accepted ASR/VAD bounds. It is not
valid when produced from caller JSON, a model directory name, a fixture,
self-reported service profile, OCR, zero values, or guessed values.

The shadow calibration command is local-only and cannot construct Pipeline
HTTP or publication services. It first writes measurement evidence, then a
candidate record, then an independent validator either commits a validation
receipt and immutable record or commits a terminal denial/indeterminate
receipt. `local_run_v1` may name only a committed record and validation receipt
whose profile, model, policy, source and RegistrySet identities exactly match.
This makes a measurement-dependent profile a later protected source revision,
not a mutable bootstrap-anchor update.

The measurement evidence is not an opaque response blob.  For each corpus
member it must contain a closed, calibration-only
`shadow-calibration-funasr-raw-response-v1` envelope and a separately stored,
canonical full-source invocation mapping.  The envelope preserves the direct
SenseVoice word timestamps and FSMN intervals, binds the request/source clock,
range, policies and measured shadow-native identity, and deliberately contains
neither `calibration_record_sha256` nor a timing-error bound.  The normal
`timed-speech-evidence-response-v1` cannot bootstrap the first record because
its ordinary service identity already assumes those fields.  The independent
validator re-decodes the raw envelope with duplicate-key rejection, recomputes
integer observations, VAD merge, anchor pairing and positive maxima, and then
compares that result to the measurement projection.  A blob hash or the
measurement command's projection alone is never evidence of calibration.

The validator uses a dedicated authority Job derived from the canonical shadow
profile key, not the one-shot timed-speech bootstrap Job. A successful command
terminals its Job; therefore each new profile version has its own calibration
validation Job, protected calibration scope, logical record ID and immutable
anchor. Generic Store writes remain unable to create that protected record or
anchor.

### Frozen Phase-2 persistence contract

The accepted validation ArtifactSet has exactly four members in this order:

| ordinal | artifact type | role |
| ---: | --- | --- |
| 0 | `calibration_record` | aggregate record |
| 1 | `calibration_record_member` | SenseVoice ASR child record |
| 2 | `calibration_record_member` | FSMN-VAD child record |
| 3 | `calibration_validation_receipt` | independent accepted decision |

All members use scope
`autocut_authority/calibration/shadow_calibration@<profile_version>`, revision
`1`, and fixed logical IDs beneath
`calibration-record/{aggregate|member/asr|member/vad|validation}/<profile-key>/1`.
The aggregate content hash is distinct from both child hashes; the two child
hashes are distinct from each other and are the values projected into the
existing ASR/VAD Registry requirements.  The validation member and generic
Command Receipt are different objects: the member is stable source-bindable
evidence, while the generic Receipt proves the Store transaction and command
outcome.

`ValidateCalibrationRecord@2.1.3` runs under authority Job
`autocut_calibration_validator:<profile-key>`.  Its request contains only exact
committed references to the prior measurement manifest/results and the locked
shadow profile identity. It re-reads every immutable raw blob, rejects duplicate
keys, recomputes alignment and integer bounds, and compares the stored
projection without trusting it. Accepted writes all four members, the succeeded
Command Receipt and one immutable `calibration_record_anchors` row in a single
transaction. Deterministic invalid evidence writes only a denied Receipt;
unavailable evidence writes only a failed/indeterminate Receipt. Neither branch
creates an ArtifactSet or anchor.

The validator is read-only with respect to providers, so process loss before a
commit is safe to replay through the existing command idempotency boundary. It
must not reuse or extend the native-invocation recovery state from migration
0016. The anchor has no mutable current pointer and cannot be updated or
deleted.

Successful calibration authority Jobs use a narrowly scoped finalization
variant: their exact validator Receipt/four-member set/anchor closes the Job
with no open slots; ordinary Pipeline Jobs retain the existing
`FinalizeRunOutcome` requirement. Validator Job key/profile cannot mutate in
any state. A terminal failed Receipt is replay-only under its key; an explicit
bounded retry uses a new attempt key over the same immutable inputs. Both
Receipt-to-set and set-to-Receipt/anchor constraints must hold, including when
a later transaction tries to attach artifacts to a denied/failed slot.
Shared aggregate/child identity fields are nested under the closed `identity`
JSON object, and SQL closure must use that exact shape.

### Frozen independently-validatable measurement input

The current `shadow-calibration-measurement-manifest-v2` is not a sufficient
validator input: it persists the invocation and raw BlobRef but drops the
canonical `raw_context` containing the expected ASR/VAD anchors.  A validator
over v2 could only trust the measurement projection and therefore cannot
produce an independent receipt.

Before `ValidateCalibrationRecord@2.1.3` is implemented, measurement
finalization must emit `shadow-calibration-measurement-manifest-v3`.  Each
ordered manifest member contains exactly the corpus-member reference, expected
anchor-reference hash, native invocation, complete canonical `raw_context` and
immutable raw-response BlobRef.  The results member remains the untrusted
projection used only for equality comparison.  The validator rejects v2 as
non-validatable, rereads the exact two-member succeeded measurement set and raw
Blob bytes, reconstructs every typed anchor/context, re-decodes the native
response and recomputes the integer bounds.  No logical-head lookup or caller
supplied context is permitted.

The record vocabulary follows this predecessor boundary:
`registry_snapshot_sha256` names the measured RegistrySet snapshot;
`producer_kind` is `asr|vad`; model IDs are exactly `SenseVoiceSmall` and
`fsmn-vad`.  The bound-algorithm identity is a Kernel-owned deterministic hash
of the frozen aggregation/alignment algorithm, not a caller-selected field.
ASR and VAD producer IDs, detector hashes, model IDs, model hashes and child
record hashes must all differ.

The pure media facade may decode and verify candidate/committed record bytes,
but it must not offer a public function that manufactures an accepted
validation receipt from caller-provided hashes.  Accepted assembly is an
internal validator-command seam and consumes only independently recomputed
proof material.  This API boundary prevents accidental self-certification;
the protected Store command and PostgreSQL transaction remain the actual
authority boundary.

## Shadow measurement recovery

Native calibration is not treated as a pure retryable function after a process
or Store-commit interruption.  Before a native call, the command persists the
closed member plan (invocation, locked context and anchors) in a shadow-only
attempt aggregate.  A leased member transitions `pending -> invoking ->
staged`; staging the raw response, BlobRef and decoder-derived projection is
one Store transaction.  The final two-member ArtifactSet/Receipt is also one
transaction over staged members only.

If a worker loses its response after a committed stage, an expiring recovery
lease discovers and finalizes the durable stage without invoking FunASR again.
If a worker may have begun native inference but no stage exists after its lease
expires, that member is `indeterminate`, not failed or silently retried.  A
successor attempt preserves the predecessor and requires a bounded, recorded
retry authorization.  Known pre-invocation resource/connection failures may
retry within that same bounded policy; unknown native outcomes never do.  This
specialized aggregate changes neither generic `command_slots` replay nor VLM
recovery semantics.

| role | identity |
| --- | --- |
| semantic VLM | `doubao-seed-2-1-pro-260628` via Ark streaming |
| word ASR | native CPU SenseVoiceSmall, `output_timestamp=true` |
| speech activity | distinct native CPU FSMN-VAD |

## Source and lock

Place closed profile/RegistrySet sources beneath protected `governance/`, and
list every file as `registry_source` in the authority inventory. The existing
A -> B -> C protocol applies: reviewed source commit A, sole-child inventory
commit B, then generated-lock commit C computed solely from B and A Git blobs.
An authority build/admin command verifies those Git blobs, compiles the closed
source set, and emits one immutable packaged authority-context resource. The
runtime never reads a checkout, `tools/`, Git path, commit, profile selector or
ordinary configuration: it reads only that installed resource and then checks
the durable PostgreSQL anchor.

## Runtime and rollback

Server deployment receives an immutable verified snapshot from the packaged
authority-context resource. It resolves the exact local-run profile and durable
anchor before worker reconstruction or outbox leasing. Any absent/zero/unknown
resource, missing anchor or record mismatch denies startup. HTTP has no
bootstrap/profile selector.

Identical bootstrap replays its Receipt; divergent identity terminates as a
conflict/rejection Receipt with no running slot. Stop a bad deployment and
deploy the previous verified profile; retain immutable anchors/records and
never delete or rewrite them.

## Delivery sequence

```text
closed raw-envelope/recoverable measurement/CalibrationRecord contract + shadow source
  -> protected A/B/C lock
  -> bounded native CPU measurement + independent record validation
  -> protected local_run_v1 source naming that record
  -> second protected A/B/C lock
  -> verified admin bootstrap -> injected HTTP snapshot -> local-only run
```

The authority-source A commit also requires an explicit tracked authorization
for this authority child; the user approval that started this task is recorded
as that approval's provenance, but a generic task must never self-authorize at
runtime. The B and C commits remain inventory-only and generated-lock-only.

## Build integration slice: locked Registry and shadow context

Freeze implementation ownership to `tools/authority/locked_registry.py`,
`tools/authority/shadow_context.py`, `tests/authority/test_locked_registry.py`,
and `tests/authority/test_shadow_context.py`, plus this task's planning record.
This is inside the existing authority-child grant. Do not modify real source
profiles, inventory/lock, runtime, ordinary CLI or package installation in
this slice.

The build entry reads an explicit C Git blob (not a checkout lock), derives A
and B from it, replays the existing A/B/C verifier and verifies every locked
blob. The selected Registry tree must consist only of lock-covered
`registry_source` files. Copy those exact regular Git blobs to a private
temporary directory, preserve their fixed eight-pack relative paths, call the
existing source loader/compiler, require ready, and reject missing or unused
files. Do not accept a caller-created RegistrySet or source mapping.

Shadow context construction reuses that verified compilation and resolves
narrative/shadow raw profile bytes only from exact `registry_source` entries.
The expected shadow contract hash comes from the locked raw
`governance/schemas/shadow-calibration-profile.schema.json` `schema_source`
entry, matching the existing profile grammar tests; it is never a repeated
caller-provided hash. Reuse the existing grammar decoder. Native service-identity
recomputation remains owned by the existing service projector at calibration
input resolution, before any I/O; do not duplicate it or import the app into
tools/Kernel. Report source/lock/registry provenance separately from calibration
acceptance. This shadow context may feed calibration only, never bootstrap or
HTTP composition.

Current published lock has neither target schema nor registry/profile entries.
Positive tests must use explicitly synthetic A/B/C repositories; no successful
fixture becomes a deployed authority profile. Dirty checkout bytes are never
read or accepted; an explicit immutable historical commit remains usable despite
unrelated checkout/config changes.

Local-run requires two distinct provenance chains: its predecessor reference
must equal a separately verified shadow source, shadow Registry snapshot and
shadow lock bundle. The existing grammar only validates the latter two hashes'
syntax, so current C cannot certify old C. Installed-resource publication and
local-run/accepted-anchor loading are subsequent work, not implied by this
shadow-only build slice.
