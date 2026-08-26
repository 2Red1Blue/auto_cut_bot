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
12. Upgrade the committed shadow measurement manifest to v3 with full canonical
    raw context and expected anchors; reject v2 for independent validation.
13. Implement the closed CalibrationRecord aggregate/ASR child/VAD child and
    independent validation-member grammar. Keep accepted assembly internal to
    the validator command; prevent reused anchors/observations/evidence hashes.
14. Add migration 0017 and a dedicated Store writer that atomically commits the
    exact four members, succeeded generic Receipt, terminal authority Job and
    immutable anchor. Database tests must execute PostgreSQL and cover missing
    payload fields, failed-with-set, nonterminal Job, pre-existing protected
    data, replay/conflict and anchor mutation.
15. Implement `ValidateCalibrationRecord@2.1.3` as a read-only deterministic
    command over exact committed measurement references and raw Blob bytes.
    Invalid and unavailable branches write Receipt-only outcomes.
16. Connect the raw shadow service through an exact-request, original-owner
    file adapter with explicit limits, no environment proxy or redirect, no
    implicit retry, and guaranteed lease cleanup. Test real HTTP transport
    separately from synthetic model output; retain independent Kernel decode.
    Authority source/anchor resolution and native calibration remain separate
    incomplete integration work, not implied by an adapter unit-test pass.
17. Project the revalidated shadow profile into the exact existing native
    service configuration; verify its native-identity hash without replacing
    it. Resolve ordered corpus members from exact committed identity-source
    manifests, their audio clocks and independently supplied hash-bound
    anchors, then construct the measurement request and source-owner bindings.
    Test source/proxy substitution, wrong clock/anchor/profile, incomplete
    corpus, and no provider/materialization during resolution. Keep real
    annotation availability separate from synthetic implementation fixtures.
18. Add the exact succeeded measurement-outcome Store reader, sharing existing
    two-member closure, and a thin deployment execution function composing
    resolution, measurement, persisted-ref lookup and independent validation.
    Test reference substitution, non-success short-circuit, no repeat native
    execution on replay, explicit validator key and durable unknown outcomes.
    Use real isolated PostgreSQL for Store tests; never reset application data.
