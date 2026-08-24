# Standalone FunASR timed-speech service

Run this separate process on the host CPU; the pipeline never imports FunASR,
ModelScope, or Torch. `FUNASR_PROFILE_JSON` binds measured FunASR/Torch versions,
actual parameter device, this service file's SHA-256, SenseVoiceSmall/FSMN model
tree hashes and revisions, the two inference kinds, producer identities, policy,
and calibration. Startup hashes and compares all of these values before ready.
Required paths and limits are `FUNASR_ASR_MODEL_PATH`,
`FUNASR_VAD_MODEL_PATH`, `FUNASR_MAX_REQUEST_BYTES`,
`FUNASR_MAX_RESPONSE_BYTES`, `FUNASR_INFERENCE_TIMEOUT_SECONDS`, and
`FUNASR_QUEUE_CAPACITY`. `FUNASR_SHARED_TOKEN` is required by both service and
pipeline and is sent only in the loopback Authorization header; never put it in
profile JSON, artifacts, command arguments, or logs.

The production profile declares `word_timing_capability=required`, invokes
`output_timestamp=True`, rejects missing/misaligned/non-monotonic/out-of-clock
timestamps, groups words at gaps greater than the manifest's calibrated 700ms,
as non-linguistic utterance protected ranges, and merges direct FSMN ranges at
gaps up to 350ms. Gap groups never claim sentence completeness. Empty lexical output plus VAD
speech is explicit `no_lexical_content` with VAD-only protected ranges. Pure
silence requires both explicit empty ASR and empty VAD. No tick is interpolated.

One AutoModel owns one FSMN weight instance; ASR and direct VAD inference run
separately under a single lock. Queue capacity is acquired atomically before a
request body is read or a temporary file is created; excess requests receive
503. Cancellation cannot release the inference lock while its worker thread is
still running. Native timeout/model failure exits 70/71 so a supervisor replaces
the unsafe process. The command retries one admission-busy result and never
retries an unknown inference result. Podman is a CPU fallback; MPS is a separate
calibrated identity and silent device fallback prevents readiness. Endpoints:
`/health/live`, `/health/ready`, and `/v1/timed-speech-evidence`.

For a host launch, activate the pinned environment from `requirements.lock`,
export the model paths, bounded limits, token, and a closed measured profile,
then run `python deploy/funasr/service.py`. Compute the profile's
`service_sha256` from that exact `service.py`; compute each `model_sha256` with
the service's canonical `tree_hash`, and use each resolved snapshot directory
name as `model_revision`.

The bounded live smoke uses one short stream-copy MP4, one request, and no
parallel inference. Probe and bind its real audio clock rather than assuming a
zero origin:

```sh
ffmpeg -hide_banner -loglevel error -ss 0 -i "$EPISODE" -t 30 \
  -map 0:v:0 -map 0:a:0 -c copy -avoid_negative_ts make_zero "$SMOKE_MP4"
ffprobe -v error -select_streams a:0 \
  -show_entries stream=time_base,start_pts,duration_ts -of json "$SMOKE_MP4"
curl --fail --max-time 60 -H "Authorization: Bearer $FUNASR_SHARED_TOKEN" \
  -H "X-Timed-Speech-Manifest: $MANIFEST_BASE64" \
  -H "X-Timed-Speech-Request-SHA256: $REQUEST_SHA256" \
  --data-binary "@$SMOKE_MP4" http://127.0.0.1:8765/v1/timed-speech-evidence
```
