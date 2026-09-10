# Shadow bootstrap observation design

## Purpose

The first real drama must be able to collect raw ASR/VAD observations before an
independent-anchor calibration corpus exists. This is a shadow-only collection
path, not a weakened MediaPreflight or Stage 4 authority path.

```text
Committed source blob
  -> FunASR shadow raw endpoint
  -> immutable raw-response blob + anchor-free observations
  -> shadow bootstrap Receipt
  -> later human/trusted-anchor corpus construction
  -> CalibrationRecord validation
```

## Authority barrier

The bootstrap result is fixed `trust_status = untrusted`,
`authority_eligible = false`, and `independent_anchor_count = 0`.

It must not:

- construct a `ProducerCalibrationMeasurement` or CalibrationRecord;
- satisfy a `LocalMediaPreflightPolicy` or timed-media finalizer input;
- have a `timed_media_evidence`, `recipe`, `render`, QC or release artifact type;
- share calibration namespace/scope with accepted calibration records;
- carry an accepted timing bound, timing-error field, calibration-record reference,
  or release decision.

## Transport

Reuse only the existing authenticated shadow endpoint:
`POST /v1/shadow-calibration-funasr-raw`.
It already binds complete source bytes, source SHA-256, shadow producer identity,
policy hashes and full source range, and returns raw ASR/VAD output without a
calibration authority. Bootstrap must not make independent anchors optional in
the existing calibration DTOs; those DTOs remain calibration-only.

## Promotion

After observations exist, a separate anchored corpus imports small local samples
with independent ASR/VAD anchors. Only the existing measurement/validation path
can then compute error bounds and issue a CalibrationRecord. Promotion is a new
run/authority revision; shadow observations remain immutable historical evidence.
