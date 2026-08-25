# Implementation

1. Close evidence DTOs, coverage/error-bound semantics, policy identities and
   the committed source-bound A/V clock-map certificate/facts. Add immutable
   profile-registry admission with independent Transcript/VAD producer and
   calibration matching; atomically persist profile admission, probe and
   certificate beside the root/candidate timed evidence members.
2. Implement adaptive candidate-window planner over verified source/proxy maps.
3. Add the claim-owned bounded BlobRef materialization lease and PostgreSQL
   chunked verified-file implementation; remove the timed-media full-`bytes`
   handoff while preserving retry/replay and local-port hash validation.
4. Add the version-pinned standalone FunASR service, closed HTTP
   `TimedSpeechEvidencePort`, and committed Transcript/SpeechActivity producers;
   remove production Whisper CLI and `silencedetect` VAD paths.
5. Add decoded video frame/audio sample endpoint producers and scene/visual/subtitle outcomes.
6. Compose conjunctive feasibility evidence and deny indeterminate gaps.
7. Persist one atomic ArtifactSet through the existing PostgreSQL Store; make replay detector-free.
8. Calibrate subtitle/audio/video tolerances on real fixtures and bind CalibrationRecord hashes.
9. Run a real Doubao candidate through the command; independently review before Stage 1–3 starts.
10. Establish the native CPU SenseVoiceSmall/FSMN-VAD golden baseline; keep MPS
   as a future calibrated profile and Podman CPU as a reproducible fallback.

11. Deploy the timed-speech authority registry before enabling media preflight:
    an authority administrator compiles the locked source, selects one exact
    profile key, and runs the protected bootstrap command against PostgreSQL.
    Persisted same-snapshot bootstrap replays; divergent profile/snapshot
    identity conflicts. Inject that verified non-placeholder snapshot into
    runtime composition. Pipeline HTTP cannot initiate bootstrap or rotation,
    and runtime startup must fail before accepting work if the injection or
    immutable anchor is absent.
