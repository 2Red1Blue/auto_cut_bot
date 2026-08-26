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
