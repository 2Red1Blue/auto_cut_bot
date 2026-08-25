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
