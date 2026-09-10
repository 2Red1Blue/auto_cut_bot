# Shadow bootstrap observation design

## Purpose

The first real drama must be able to collect raw ASR/VAD observations before an
independent-anchor calibration corpus exists. This is a shadow-only collection
path, not a weakened MediaPreflight or Stage 4 authority path.

```text
Committed source blob
  -> FunASR shadow bootstrap endpoint
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

## Transport and protocol closure

Bootstrap uses its own authenticated endpoint:
`POST /v1/shadow-bootstrap-timed-observation`.  Its closed request schema is
`shadow-bootstrap-observation-request-v1`; it binds complete source bytes,
source SHA-256, an explicit audio clock/range, source-byte limits, and a
response limit.  The returned raw schema is
`shadow-bootstrap-observation-funasr-raw-response-v1` and embeds the same
request identity.

The service projects provider-specific ASR extensions (for example
`sentence_info`) down to the three raw fields that the Kernel owns:
`text`, `words`, and millisecond `timestamp`.  No provider extension crosses
the immutable protocol boundary.

The Kernel then converts milliseconds to the declared integer audio clock. If
two native word intervals touch exactly but floor/ceil conversion creates one
tick of double coverage, it trims only the prior converted end to the next
start. This repair is allowed solely for that one-tick representation error;
all other native or converted overlap rejects. The projection records
`asr_rounding_repair_count` and `max_asr_rounding_repair_tick`, while preserving
the raw response hash for full replay.

Bootstrap must not make independent anchors optional in any existing
calibration DTO; those DTOs remain calibration-only.

An HTTP timeout, truncated response, or transport loss is an **unknown
dispatch**, not a denied or failed observation. The collection command leaves
its claim running and writes no terminal Receipt in that case; a later bounded
recovery policy must create a causally linked successor only after deciding
whether another provider invocation is safe. It must never silently retry the
same unknown dispatch.

## Promotion

After observations exist, a separate anchored corpus imports small local samples
with independent ASR/VAD anchors. Only the existing measurement/validation path
can then compute error bounds and issue a CalibrationRecord. Promotion is a new
run/authority revision; shadow observations remain immutable historical evidence.

## First real-PC evidence

On 2026-09-10, `r0-ep01` was sent to the desktop CUDA service using this path.
The service returned HTTP 200 with one ASR batch and one VAD batch. The strict
Kernel replay produced 272 ASR observations and 27 VAD observations, with one
recorded one-tick rounding repair. The raw request/response and replay summary
remain on the desktop under
`/home/laiu/r0-artifacts/shadow-bootstrap/r0-ep01/`; they are untrusted
observational evidence, not a CalibrationRecord or a publication authorization.
