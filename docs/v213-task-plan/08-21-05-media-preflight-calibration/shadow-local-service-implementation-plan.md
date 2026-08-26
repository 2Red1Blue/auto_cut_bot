# Shadow-local service measurement — next implementation slice

Status: designed, not implemented or activated. Native-agent read-only research
and root decisions following the accepted pure case/projector checkpoint
`0001b165`. Complements [local-window Command plan](local-window-command-implementation-plan.md#7-calibration-activation--newly-identified-required-seam).

## Why a separate measured mode

The existing shadow service profile and calibration Command measure full-source
inference. Local inference uses exact extraction, a local PCM WAV and projection
back to source ticks. Sharing model weights does not establish equal timing
behavior. The old SourceClockPolicy is explicitly full-source-only and must not
be silently reinterpreted. This slice generates measurements only; it does not
install a normal profile or grant admission.

## Service profile identity

Add a closed `funasr-shadow-local-calibration-profile-v1` service schema. Reuse
the existing shadow model/service/device/version/producer and timing-policy
fields, with an explicit measured `decoder_identity_sha256`. No accepted Record,
timing-error bound or future Receipt is permitted. The new schema identifies
the local-PCM execution path; do not accept the old schema at the new route.

The native identity is the canonical hash of all these pre-calibration fields
excluding its own native identity field. The complete service-profile hash then
includes that native identity. A builder must derive, not accept contradictory
values for, these hashes. The already implemented ShadowLocalCalibrationCase
binds the complete hash in its wire policy and the native identity separately.
The pure builder is content generation, not a deployment or acceptance permit.

Startup continues to measure actual service code, both model trees, framework
versions and device. Also compare the actual decoder identity (PyAV/libav,
NumPy/libsndfile, extraction planner and PCM policy) before setting ready. Do not
replace measurement with a caller-provided identity or an installed old Record.

## Fixed route and shared execution

Add `/v2/shadow-calibration-speech-window` and
`Service.shadow_local_window_evidence()`. Factor a shared internal window
handler with fixed wrappers: normal requires NORMAL_PROFILE_SCHEMA; shadow-local
requires the new schema. Preserve the old normal and full-source shadow routes.
No request field may select a weaker mode or bypass the wrapper's required schema.

Reuse the existing LocalSpeechWindowRequest/Response codec and
`run_window_inference()` path, including source-byte verification, extraction
report, original raw outputs and projection checks. The request policy must
match the actual measured profile and its decoder. Independent gold anchors
stay in Kernel and are not disclosed to the service/model.

Preserve exact authentication, canonical manifest/hash, service resource limits,
single dispatch, bounded source/response and cancellation-safe lease/queue cleanup.
Only the admission call's pre-dispatch refusal emits a verified BUSY proof.
Not-ready, incomplete transport or later inference failures remain unknown or
failed, never evidence that dispatch did not start.

Add a fixed shadow-local HTTP wrapper sharing the existing single-dispatch
implementation. Do not make the normal endpoint validator accept arbitrary
paths, hosts, redirects, credentials or a user-selected mode. Keep Kernel's
producer port application-independent.

## Ownership and verification

Suggested disjoint files:

- New pure `media/shadow_local_service_profile.py` plus its tests: closed content,
  actual-field hash derivation, normal/old-shadow/Record/bound rejection.
- `deploy/funasr/service.py` plus new synthetic shadow-local endpoint/startup
  tests: mode separation, actual decoder mismatch, limits, BUSY and cancellation.
- Existing window HTTP client plus a fixed shadow-local wrapper and tests:
  shared wire/result/error behavior, normal URL restrictions unchanged.

Use fake model/decoder and ephemeral HTTP on Mac; no native models/codecs or DB.
Use the existing independent local anchors/projector to consume synthetic route
responses end-to-end. Real extraction, both models and calibration corpus run on
the desktop only. A passing fake response is not a completed calibration.

## Following persistence and activation work

Define a versioned local source/profile grammar binding decoder, local cases,
independent anchor corpus and acceptance algorithm. Persist measurements; let an
independent validator reread raw bytes, exact case ownership and all observations.
Zero measured error must remain zero; an explicit conservative acceptance policy
may add a justified margin but must not falsify the measurement.

Only a corresponding persisted local CalibrationRecord/anchor may compile and
activate a normal local profile. The existing `bind_profile_calibration()` and
full-source installed resource cannot substitute for that chain. Durable local
child Commands and episode admissions must independently check the actual local
mode, not merely hash syntax or the constructor of an expectations DTO.
