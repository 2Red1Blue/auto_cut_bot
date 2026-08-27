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
