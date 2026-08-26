# Local speech foundation checkpoints

## Shared Kernel producer port — ALLOW

Root authored; calibration_contract independently reviewed. The existing HTTP
adapter now returns the Kernel-owned result value and exposes a Kernel-owned
BUSY base while retaining its LocalMediaToolError compatibility. No wire schema,
HTTP route, source lease ownership, retry or persistence behavior changed.
The future Command must still independently replay raw bytes/proof.

- Root: 199 passed, zero skipped, using the documented temporary NumPy/SoundFile
  overlay and the six prior window/full-source regression modules plus
  test_local_speech_kernel_port.py. Fake models/decoders, synthetic PCM, ephemeral
  loopback only; no real model/native codec/DB acceptance.
- Independent reviewer: 37 focused passes; Ruff and production types clean.
- No Critical/Warning findings. The result DTO is content, not admission.

## Local lifecycle and resolved Source audio facts — ALLOW

calibration_contract authored; review_calibration_migration independently
reviewed all four files. Root reviewed the DTO/codec, Source resolver diff and
test coverage, then ran 345 combined passes (127 new plus old Prepare/physical
prelude). Scoped Ruff and production types are clean. Independent review ran
242 related pure cases and found no Critical/Warning issues.

The old resolved canonical payload/hash does not gain a hidden audio field;
the local lifecycle explicitly binds the measured leaf. Full parent, physical
handles, candidate, profile expectations, window and budgets derive acyclic
request/wire identities; callers cannot supply a separate idempotency key.
Constructor checks are not Store/profile/retry permission. An initial draft
unnecessarily required ASR/VAD CalibrationRecord hashes to differ; root caught
and removed that constraint. One Record may contain both independently checked
producer roles, and a regression test now requires that valid case to work.

Other foundation slices are being reviewed separately; task05 is not complete.
