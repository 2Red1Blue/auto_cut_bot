# Genuine local speech window wire

This follows the accepted local PCM primitive (`fcc4b7ae`, desktop codec
acceptance recorded in `264c5bb6`). It connects a real provider boundary, not a
replacement for window Command ownership, installed calibration or Stage4
Admission. The public Pipeline run API is unchanged.

## Frozen responsibilities

- Kernel owns `LocalAudioWindowSpec`, path-free `DecodedLocalPcmReport`,
  `LocalSpeechWindowPolicy`, `LocalSpeechWindowRequest`, strict codecs and raw
  output projection into the existing TranscriptSet/SpeechActivitySet types.
- The normal measured native service exposes a distinct authenticated loopback
  `/v2/timed-speech-window` endpoint. The original `/v1/timed-speech-evidence`
  remains full-source only; shadow-calibration endpoints are not reused.
- Pipeline owns only verified-file HTTP transport and endpoint/token/timeout
  configuration. No model import, planner, guessed clock or accepted Artifact.
- Source/VLM/candidate authorization and expansion predecessor validation stay
  in the future window Command. A binding hash carried by the provider wire is
  correlation, not proof of that authorization.

## One request

`LocalSpeechWindowRequest(extraction, policy, binding_sha256, max_response_bytes)`
binds the exact original-source extraction spec, a frozen measured speech
profile projection, caller Command/window binding, and response byte limit.
There is no source path, HTTP credential, endpoint or silent default in the
canonical request. The existing extraction cap limits source upload.

`LocalSpeechWindowPolicy` contains the full normal measured-service profile
hash, independent ASR/VAD producer IDs and generation-policy hashes, and the
exact utterance/VAD merge gap milliseconds. The profile hash transitively binds
models, device, adapter, both producer/calibration identities and the service's
measured settings. The service independently projects these fields from its
loaded normal profile and requires exact equality; the request cannot select
or modify its model/profile. Decoder identity must equal the native library/
shared-source identity measured on this service. Old service/calibration
identities do not automatically become valid after code changes.

## One response and independent projection

The closed response binds its request hash and measured extraction report,
plus original JSON ASR/VAD outputs. Keep raw native outputs for subsequent
immutable persistence; do not replace them with self-declared `pass` or only
postprocessed segments. The strict Kernel decoder verifies report source/spec/
decoder/rate/channel/sample-count/byte/frame bounds and the exact request hash.
JSON duplicate keys, nonfinite constants, unknown envelope fields and foreign
request/report identities reject.

The shared projector independently derives word ticks and VAD protected ranges
from real native millisecond pairs using floor-start/ceil-end exact rational
conversion relative to the verified extracted window start. It retains the
original audio clock and source identity; only the evidence context/coverage
extent is local. It groups words and merges VAD using the frozen policy, never
interpolates missing words or invents sentence completeness. Empty lexical
output with detected VAD is valid VAD-only protection; only both explicit empty
results mean silence. Bad/misaligned/out-of-range pairs reject.

The HTTP adapter posts once through existing bounded file transport, without
environment proxy or redirects. Unknown transport outcome is not an implicit
retry. The service verifies token/profile/request before body work, preserves
queue/resource admission, streams and verifies the original source, calls the
existing exact `run_window_inference`, validates projected output and returns
a bounded response. Private upload/WAV files and admission ownership release
on every terminal path, including repeated cancellation under the fixed native
lock/deadline lifecycle.

## Implementation ownership and checks

Root: shared types/codecs, Pipeline client and its tests, integration/docs.
`calibration_contract`: native service endpoint/report import, service tests.
`calibration_migration`: shared raw-output projector and pure projection tests.
`review_calibration_migration`: separate read-only final audit.

The user's renewed Claude Code authorization adds two bounded parallel jobs:
Claude owns the new `tests/pipeline/test_funasr_window_client_server.py` only
for production-client-to-ephemeral-service integration, and a separate Claude
run owns `local-window-command-implementation-plan.md` for the next durable
slice. Neither may modify the frozen production implementation, private
configuration, existing tests, or Git history. Root integrates and commits;
the separate reviewer checks the resulting code and test delta.

Required checks: strict request/response mutation tests, negative/nonzero clock
origins and rounding, real local WAV input checked through the endpoint with
synthetic decoder/model callbacks, original v1/shadow regressions, token/profile/
source mismatch before model work, response bound/unknown transport/no retry,
queue and cancellation cleanup. No real models/codecs/DB run on the Mac.
Codecs passing here are not evidence of durable restart/replay; that still
requires the subsequent window Command and physical-root/local-speech split.

## Review-driven corrections

- Reject JSON numeric overflow such as `1e999`, not only literal NaN/Infinity.
- Retain original response bytes and replay them before projection; mutation
  of a decoded native dictionary cannot alter raw-bound evidence.
- Explicitly derive Transcript boundary-touch flags from converted word
  endpoints. A local word touching either edge is not a closed dialogue claim.
- Merge ordered, overlapping FSMN ranges using the maximum end. A nested or
  overlapping range cannot shorten the protection interval.
- Accept the native SDK's optional textual `key`; empty lexical output may
  omit `words` only with empty timestamps. Other unknown native fields reject.

`COMPLETE` coverage here describes only the decoded local measurement extent;
it is not full-source coverage or sentence completeness. VAD-only protection
remains valid, and these provider values still require committed ownership,
calibration and independent admission at their consumers.

## Accepted wire checkpoint

Implementation is committed in `d1845f5f`. Claude's six production-client/
ephemeral-service tests passed a separate independent delta review. Final
combined validation: **338 passed, 4 desktop-only codec tests skipped**; the
three window HTTP suites account for 58 passing tests. All native model and
decoder callbacks in this Mac run are synthetic. Scoped Ruff and type checks
passed; the existing dynamic native service is not claimed globally type-clean.

The completed wire task is archived under
`.ccg/tasks/archive/2026-08/local-speech-window-wire/`. Task05 remains active:
the composed Pipeline still needs the physical-root split, durable window
Commands/readers, real local calibration and Runtime wiring. Its next design is
[the local-window Command plan](local-window-command-implementation-plan.md).
