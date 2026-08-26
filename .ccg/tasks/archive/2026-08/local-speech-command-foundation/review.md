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

## Shadow-local case and raw projection — ALLOW

review_calibration_migration authored; root independently read both production
files and both tests. Root reproduced 303 pure passes (150 new plus prior
shadow/raw/window regressions), with scoped Ruff/types clean. One test-only
relative import initially failed ordinary pytest collection; the author fixed
it to the project's absolute test import convention and root reran the default
pytest command successfully. No production blocking findings remain.

Case hash binds independent local anchors, original source/provenance, actual
extraction, decoder/model/service/policy identities without future Record or
accepted bounds. The request binds that case. Projection reuses the existing
raw decoder/projector and matches all observations in order; no sorting/clipping
gold or guessing matches. Real zero error and explicit empty observations remain
measurements, not a fabricated positive bound or accepted calibration.

```sh
uv run --no-sync pytest -q tests/media/test_shadow_local_calibration.py tests/media/test_shadow_local_calibration_projection.py tests/media/test_local_speech_window_codec.py tests/media/test_local_speech_window_projection.py tests/media/test_shadow_calibration_raw.py
# 303 passed
```

This is not a deployed shadow-local service, persisted CalibrationRecord,
independent acceptance or normal Runtime activation.

## Mapped local-window assessment — ALLOW

calibration_migration authored; calibration_contract independently reviewed.
Root reviewed and corrected the initial design/implementation before acceptance:
audio snap error alone cannot replace ASR/VAD errors; word-only guards omitted
utterance-gap protected segments; unordered calibration-set validation did not
prevent swapped roles; selected audio stream also needs to match the probe.
These corrections and direct regressions are in the accepted code.

The function replays raw bytes, requires the full A/V span in one proved
continuous interval, maps policy guard points with exact Fraction arithmetic,
and never treats those interior guard points as physical frame endpoints.
Source-edge suppression requires both streams' own endpoints. Sentence
completeness stays not_applicable. No child commit or profile permission occurs.

- Author: 15 new cases; 90 mapped/window regressions; Ruff/types clean.
- Independent review: 15 cases plus a negative-origin pure probe; ALLOW.
- Root: 923 combined pure cases; scoped Ruff/types clean.

```sh
uv run --no-sync pytest -q --tb=short tests/media/test_mapped_local_window_assessment.py tests/media/test_mapped_local_audio_window.py tests/media/test_local_speech_window_contract.py tests/media/test_resolved_source_audio_facts.py tests/media/test_shadow_local_calibration.py tests/media/test_shadow_local_calibration_projection.py tests/media/test_shadow_calibration_raw.py tests/media/test_local_speech_window_codec.py tests/media/test_local_speech_window_projection.py tests/media/test_prepare_timed_media_evidence_command.py tests/pipeline/test_physical_media_prelude.py tests/store/test_terminal_command_receipt.py tests/store/test_exact_committed_set_reader.py
# 923 passed
```

Foundation task complete. Parent task05 remains in progress; durable child,
exact chain/episode readers, parent admissions, shadow-local service/persistence/
acceptance, Runtime wiring and desktop execution have not been completed here.
