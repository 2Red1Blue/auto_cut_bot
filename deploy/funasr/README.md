# Standalone FunASR timed-speech service

Run FunASR as one CPU process directly on the host. Podman is used only for the
database in this deployment; it is not the production FunASR topology. The
Dockerfile remains a lock-aligned reference artifact and has not passed the
locked-model smoke described below.

## Runtime topology and admission

`FUNASR_QUEUE_CAPACITY` is required to equal `3`, matching the pipeline/HTTP
in-flight budget. Three admitted requests do **not** create three models or run
three inferences: one service owns one `AutoModel`, including its FSMN VAD
weights, and an `asyncio.Lock` serializes the complete ASR plus direct-VAD call.
A fourth request receives 503 before its body is read or its spool directory is
created.

The production lock is fixed in code to the canonical host path represented by
`/tmp/autocut-funasr-service.lock` (`/private/tmp/...` after resolution on
macOS). `FUNASR_SINGLETON_LOCK_PATH` is retained only as a fail-closed
configuration assertion: its resolved parent and filename must equal that fixed
path, so another port cannot select another lock file. The service takes a
non-blocking OS file lock before constructing `AutoModel` and holds it for the
model lifetime. Normal cleanup, startup exceptions, and process exit release
the lock; a stale regular file without a live file lock is harmless, while a
symlink lock file is rejected.

## Resource gate

The service reads Linux `MemAvailable`/swap counters or macOS `vm_stat` and
`vm.swapusage`. It fails startup before model construction unless both the
available-memory and swap limits pass. It repeats the inference-headroom check
for every request before admission and body spooling. Configure byte counts:

- `FUNASR_STARTUP_MIN_AVAILABLE_BYTES`
- `FUNASR_INFERENCE_MIN_AVAILABLE_BYTES`
- `FUNASR_MAX_SWAP_USED_BYTES`

The available-byte thresholds must be positive. The maximum swap-used threshold
is non-negative; set it to `0` to reject any observed swap use.

The observed single-process CPU smoke peaked near 3.2 GB RSS, while the macOS
host already had about 30 GB in use and 9 GB of swap, producing much higher
system pressure. A conservative initial host policy is 8 GiB available at
startup (`8589934592`), 4 GiB available before each inference (`4294967296`),
and at most 2 GiB swap used (`2147483648`). Calibrate upward from recorded host
measurements; do not lower these merely to make readiness green.

Snapshot failure or insufficient headroom returns the stable 503 body
`resource-pressure` with `Retry-After: 1`. The HTTP client records this as a
`TIMED_SPEECH_BUSY` rejection Receipt and the command permits only one retry.
This is an infrastructure-pressure result, never a content/no-speech result.
The queue-full response is also retry-limited, so three occupied slots can make
the single retry fail before a slot is released.

## Identity and required environment

`FUNASR_PROFILE_JSON` binds measured FunASR/Torch versions, actual parameter
device, this service file's SHA-256, SenseVoiceSmall/FSMN model tree hashes and
revisions, inference kinds, producer identities, timing policy, and
calibration. Startup hashes and compares all values before readiness. Silent
MPS-to-CPU fallback therefore fails the measured-device check.

The complete runtime environment is:

- `FUNASR_REQUIRED_PYTHON_VERSION=3.13.13`
- `FUNASR_ASR_MODEL_PATH` and `FUNASR_VAD_MODEL_PATH`
- `FUNASR_MAX_REQUEST_BYTES` and `FUNASR_MAX_RESPONSE_BYTES`
- `FUNASR_INFERENCE_TIMEOUT_SECONDS`
- `FUNASR_QUEUE_CAPACITY=3`
- the three resource limits above
- `FUNASR_SINGLETON_LOCK_PATH=/tmp/autocut-funasr-service.lock` (canonical path only)
- `FUNASR_SHARED_TOKEN` (non-empty and sent only in loopback Authorization)
- `FUNASR_PROFILE_JSON`

## Pipeline Media Preflight composition

The Pipeline worker is admitted only when it has an explicit private staging
root and an exact closed materialization policy.  Create the root before
starting the worker; it must be owned by the worker account, be a real
directory (not a symlink), and have mode `0700`:

```sh
install -d -m 700 /var/lib/autocut/media-preflight-staging
export AUTO_CUT_BOT_MEDIA_PREFLIGHT_STAGING_ROOT=/var/lib/autocut/media-preflight-staging
export AUTO_CUT_BOT_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_JSON='{"max_source_bytes":2147483648,"timed_speech_max_request_bytes":2147483648,"copy_chunk_bytes":1048576,"staging_quota_bytes":6442450944}'
export FUNASR_MAX_REQUEST_BYTES=2147483648
```

`timed_speech_max_request_bytes` and `FUNASR_MAX_REQUEST_BYTES` must be the
same exact value.  The JSON has no aliases, omitted fields, or defaults; it
must contain exactly `max_source_bytes`, `timed_speech_max_request_bytes`,
`copy_chunk_bytes`, and `staging_quota_bytes`, all positive integers.  The
limits are frozen into the Pipeline execution profile and Media Preflight
command identities.  The root path is deliberately operational only and is
never part of an evidence hash.

Each shared staging root pins its first `staging_quota_bytes` in a locked,
private root-local record.  A worker configured with a different quota fails
before it reserves space or materializes a source.  Do not point unrelated
deployments at the same root.

The only admitted production profile is `sensevoice_word_guard_v1`. It requires
SenseVoiceSmall `output_timestamp=True` real word pairs and independent direct
FSMN-VAD. Missing, misaligned, non-monotonic, or out-of-clock timestamps fail
closed; the service never interpolates them. Words are grouped at gaps greater
than the calibrated 700 ms as non-linguistic utterance protected ranges; direct
FSMN ranges merge at gaps up to 350 ms. These groups never claim sentence
completeness: `sentence=not_applicable` is an unsupported capability, not a
proof of complete dialogue.

The response reports independent `lexical_outcome` and `speech_outcome` fields.
`no_lexical_content` plus `speech_detected` is valid VAD-only protection and
does not fabricate TranscriptWords. Empty lexical output plus `none_detected`
is the only pure-silence closure. Any other outcome pairing, identity/policy
drift, source-clock mismatch, or excessive/non-positive timing error bound is
rejected by the HTTP client before evidence enters the pipeline. A future
sentence-boundary profile must have its own registered and calibrated identity;
the former `sensevoice_word_utterance_v1` identifier is unsupported.

Timeout or model failure exits 70/71 so a supervisor replaces the unsafe
process. Cancellation does not release the inference lock while its worker
thread is running. Endpoints are `/health/live`, `/health/ready`, and
`/v1/timed-speech-evidence`.

## Host lock and pending locked-model smoke

`requirements.lock` records the versions present for the successful bounded
host smoke: CPython 3.13.13, FunASR 1.4.1, Torch 2.13.0, and exact direct
dependencies. `python -m pip check` passed in that existing host environment.
This does not prove that a new environment installed from the lock behaves the
same, and it does not turn the current deployment verdict into GO.

Before deployment, create a clean CPython 3.13.13 host environment, install
`requirements.lock`, record `python -m pip freeze`, `python -m pip check`, model
directory hashes, and the service hash, then run the real locked-model smoke.
The retained smoke record must cover authenticated success, invalid token,
strict-decode rejection, measured CPU identity and MPS-fallback rejection,
exactly one model load, three admitted/serialized requests, fourth-request
rejection before spool, cancellation/exception release, and resource readings
showing the configured budget was respected. Until that record exists, status
remains **NO-GO**.

The checked-in test layer is intentionally provider-free: it substitutes only
the process boundary and model object, does not download model weights, and
asserts the closed request/response, admission, spool, cancellation and identity
contracts. It is not evidence of a real-model deployment smoke.

For the bounded media probe, stream-copy one short MP4 and bind its real audio
clock rather than assuming a zero origin:

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
