# Standalone FunASR timed-speech service

Run this separate process on the host CPU; the pipeline never imports FunASR,
ModelScope, or Torch. `FUNASR_PROFILE_JSON` binds measured FunASR/Torch versions,
device, SenseVoiceSmall/FSMN model tree hashes, producer identities, policy and
calibration. Required paths and limits are `FUNASR_ASR_MODEL_PATH`,
`FUNASR_VAD_MODEL_PATH`, `FUNASR_MAX_REQUEST_BYTES`,
`FUNASR_MAX_RESPONSE_BYTES`, `FUNASR_INFERENCE_TIMEOUT_SECONDS`, and
`FUNASR_QUEUE_CAPACITY`.

The production profile declares `word_timing_capability=required`, invokes
`output_timestamp=True`, rejects missing/misaligned/non-monotonic/out-of-clock
timestamps, groups words at gaps greater than the manifest's calibrated 700ms,
and merges direct FSMN ranges at gaps up to 350ms. Empty lexical output plus VAD
speech is explicit `no_lexical_content` with VAD-only protected ranges. Pure
silence requires both explicit empty ASR and empty VAD. No tick is interpolated.

One AutoModel owns one FSMN weight instance; ASR and direct VAD inference run
separately under a single lock. Native timeout/failure exits 70/71 so a
supervisor replaces the unsafe process. Podman is a CPU fallback; MPS is a
separate calibrated identity. Endpoints: `/health/live`, `/health/ready`, and
`/v1/timed-speech-evidence`.
