# Review — CUDA shadow calibration deployment

## Result

Approved for the PC shadow-calibration deployment slice.

## Correctness checks

- The CUDA service accepts the exact closed wire profile emitted by the
  authority-profile renderer: CUDA device object, build audit hash, explicit
  timing-engine compatibility version, and derived compatibility hash.
- The service independently remeasures model/runtime/decode inputs and compares
  the derived compatibility hash.  It cannot use a CPU fallback.
- The CUDA profile enables the raw shadow-calibration endpoint only; normal
  timed-speech and local-window endpoints remain denied.
- The image pins the PC-verified runtime family: CPython 3.13.13, FunASR 1.4.1,
  and Torch/Torchaudio 2.11.0+cu128.

## Validation

- `uv run pytest -q tests/pipeline/test_funasr_timed_speech.py tests/pipeline/test_shadow_calibration_service_profile.py tests/authority/test_authority_profile_sources.py tests/media/test_timing_compatibility.py` — 112 passed.
- `uv run ruff check …` and `git diff --check` — passed.
- Full-file formatter check reports pre-existing formatting drift in the large
  service/test files; it was not mass-reformatted because that would obscure
  this focused deployment change.
- Local BasedPyright reports unresolved GPU/audio runtime dependencies and
  existing broad unknown-type diagnostics (170 errors); the validated PC CUDA
  container is the applicable environment for that check.

## Deliberate boundary

This is shadow calibration only.  It does not create a CalibrationRecord or
enable the normal Pipeline endpoint; those require the subsequent real PC
calibration and local-run authority installation.

## Follow-up design review

The existing CalibrationRecord closes over a static `native_port_identity_sha256`
but does not persist the dynamically measured CUDA timing-compatibility hash.
It would be unsafe to turn the new self-measured shadow identity directly into
normal-mode authority. The next change must bind that hash through the raw
measurement, independent validator, persisted record and installed-runtime
resolver before normal CUDA mode is enabled.

## Environment-specific capability and reuse slice

### Result

Approved at the kernel/control-plane boundary, with one remaining integration
step: Media Preflight must consume the new runtime-capability resolver instead
of the legacy singular local-run calibration resolver.

### Findings resolved during review

- A first implementation allowed PC-CUDA and Mac-CPU capabilities to point to
  the same old `shadow_calibration@N` anchor. This was rejected and replaced
  with independent immutable scopes:
  `runtime_calibration@pc_cuda@N` and
  `runtime_calibration@mac_cpu@N`.
- The initial migration widened only the anchor table. Existing 0017 Job and
  receipt triggers still recognized the old scope grammar, so a real v2 Job
  would have been rejected. Migration 0024 now replaces those guards before
  accepting any v2 capability write.
- HTTP/control-plane startup no longer reads dynamic calibration acceptance.
  Missing calibration must be classified at the target Media Preflight stage,
  not turn the complete API/worker into an outage.

### Implemented boundaries

- `RuntimeMeasurementIdentity` separates timing-compatible identity from the
  audit-only build hash.
- Immutable runtime capability persistence and exact Store re-read bind one
  environment-specific accepted calibration closure.
- `awaiting_calibration` and `recompute_needed` are durable, receipt-less
  pipeline states; workers acknowledge rather than retry them.
- `EvidenceRequirement` and `EvidenceIndex` separate equivalent evidence from
  its origin Job/Receipt/ArtifactSet.
- `ComposeWholeEpisodeEvidence@2.1.3` rereads every selected child closure by
  exact identity and writes one append-only aggregate in the destination Job.

### Validation

- Focused kernel/control-plane suite: `342 passed, 75 skipped`.
- Focused Ruff and `git diff --check`: passed.
- PostgreSQL-backed cases are skipped on this Mac because Podman is unavailable
  (`127.0.0.1:65096` refused the Podman socket). Migration 0024/0025 must be
  applied and tested against the PC disposable verification database before
  normal evidence is enabled.

### Remaining implementation gate

Do not enable normal timed-speech evidence yet. First wire the static runtime
capability policy plus a fresh service measurement into Media Preflight; it
must emit `awaiting_calibration` for a missing accepted capability and
`recompute_needed` for a changed requirement before it materializes media or
calls ASR/VAD. The legacy singular resolver remains read-only historical
compatibility until that switch is complete.

## Dynamic Media Preflight capability admission

### Implemented

- `GET /v1/runtime-measurement-identity` is a separately authenticated,
  readiness-gated FunASR endpoint. It exposes the complete self-measured
  `RuntimeMeasurementIdentity`, never an accepted record or a profile selector.
- Pipeline composition derives the static PC-CUDA/Mac-CPU policy only from the
  installed authority resource, then injects it plus a strict loopback client
  into Media Preflight.
- Immediately before the batch can construct a Kernel detector command, the
  stage fresh-reads service identity and resolves the exact persisted v2
  capability. `MediaEvidenceUnavailableError` becomes `awaiting_calibration`;
  a malformed/changed binding becomes `recompute_needed`; transient local
  service failure remains receipt-less `indeterminate`.

### Local validation

- Ruff: passed.
- BasedPyright on the changed application modules: 0 errors, 0 warnings.
- Focused authority/runtime/service suite: superseded by the follow-up result below.

### Still required on the PC

The Mac cannot start Podman, so the PostgreSQL-backed final proof is pending:
apply migrations 0024/0025 to the disposable verification database, seed one
accepted PC-CUDA capability, then exercise the real service endpoint and one
Media Preflight run. This is an environment verification gate, not a fallback
or startup blocker.

### Adversarial follow-up repairs

The first integration review found four blocking paths and they were repaired
before delivery:

- Configured normal FunASR profiles now derive the same complete timing
  identity lazily at the authenticated identity endpoint; startup itself does
  not gain a decoder/calibration prerequisite.
- An HTTP `409` means the live identity needs recalibration and projects to
  `recompute_needed`; only transport/temporary service unavailability stays
  `indeterminate`.
- The Store distinguishes "no accepted capability for this environment" from
  "a capability exists but its timing identity differs" using the dedicated
  `RuntimeCalibrationIdentityMismatchError`; the latter projects to
  `recompute_needed`.
- Migration 0026 permits an indeterminate command to reconcile into either
  receipt-less calibration wait state. The reconciler and PostgreSQL writer no
  longer attempt to invent a terminal Receipt for those states.

The final review also corrected two persistence/liveness issues: migration
0027 includes `profile_source_sha256` and `registry_snapshot_sha256` in the
immutable capability primary key, so an authority lineage update can persist a
fresh calibration even if physical timing is unchanged. An explicit CAS resume
now atomically wakes only `awaiting_calibration` to `pending`/`accepted`; the
next Media Preflight invocation rechecks live capability before touching media.
`recompute_needed` deliberately remains terminal: a caller must submit a new
Run, preserving the failed run's causal history.

Final local validation: Ruff and `git diff --check` passed; BasedPyright for
all changed application modules reported 0 errors/0 warnings; targeted suites
reported `152 passed, 4 skipped`. A broad `pytest -q` remains
environment-blocked before collection by absent separate Agent/Core and
authority-repository inputs, unrelated to this change; it is not represented
as a passing regression run.

Independent adversarial re-review found and closed the final two issues: a
capability from any older authority lineage now yields `recompute_needed`
rather than an unsafe wakeable wait state, and the CAS wake query is explicitly
limited to the `media_preflight` command. The reviewer reported no remaining
Critical findings after the final patch.

## CUDA command authority projection review

### Result

Approved as an isolated durable-command slice. It does not yet wire CUDA into
Pipeline completion: that remains gated on a CUDA-specific committed reader and
batch finalizer.

### Adversarial fixes

- The command requires the exact installed Store-reading CUDA authority
  resolver; a structural lookalike resolver cannot inject a caller-built
  projection.
- The resolver pins the protected static operation-policy hash. The producer's
  closed `pc-cuda-runtime-timed-speech-policy-v1` mapping must echo that hash,
  the exact Store-derived projection fields, ASR/VAD records/bounds and native
  adapter identity.
- CUDA accepts only its v2 loopback evidence route and v2 provenance grammar;
  the legacy CPU command accepts only v1 provenance. A legacy route, a
  different policy schema, or a static-policy hash drift produces a durable
  denial.
- A lost success acknowledgement remains indeterminate and is reconciled by
  reading the command slot; it cannot be turned into a denial after commit.

### Validation

- `uv run pytest -q tests/media/test_prepare_runtime_timed_media_evidence_command.py tests/media/test_prepare_timed_media_evidence_command.py tests/authority/test_runtime_timed_speech.py tests/pipeline/test_runtime_timed_speech_request.py tests/pipeline/test_funasr_timed_speech.py tests/pipeline/test_local_media_preflight.py` — 200 passed.
- Focused Ruff — passed.
- BasedPyright for the changed CUDA command and installed resolver — 0 errors,
  0 warnings.
- `git diff --check` — passed.
