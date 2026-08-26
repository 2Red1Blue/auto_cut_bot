# Local speech foundation checkpoints

## Shared Kernel producer port — ALLOW

Root authored; calibration_contract independently reviewed. The existing HTTP
adapter now returns the Kernel-owned result value and exposes a Kernel-owned
BUSY base while retaining its LocalMediaToolError compatibility. No wire schema,
HTTP route, source lease ownership, retry or persistence behavior changed.
The future Command must still independently replay raw bytes/proof.

- Root: 199 passed, zero skipped, using the documented temporary NumPy/SoundFile
  overlay and the seven prior window/full-source regression modules plus
  test_local_speech_kernel_port.py. Fake models/decoders, synthetic PCM, ephemeral
  loopback only; no real model/native codec/DB acceptance.
- Independent reviewer: 37 focused passes; Ruff and production types clean.
- No Critical/Warning findings. The result DTO is content, not admission.

Other foundation slices are being reviewed separately; task05 is not complete.
