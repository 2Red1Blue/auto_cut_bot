# Plan

1. Preserve the already-completed CUDA shadow identity/deployment work; it
   remains measurement-only and grants no normal evidence route by itself.
2. [done] Add immutable runtime measurement identity and environment-specific
   calibration capabilities. A static policy can bind independent accepted
   PC-CUDA and Mac-CPU records without replacing either historical record.
3. [done] Decouple runtime startup from dynamic capability availability. The control
   plane starts with valid static authority; timed-speech work becomes
   `awaiting_calibration` or `recompute_needed` when its frozen requirement
   cannot be satisfied.
4. [done] Add a pure requirement fingerprint and an exact reusable-evidence index.
   The fingerprint excludes Job/command ownership but includes every semantic
   input that can change the produced evidence.
5. [done] Add an append-only cross-Job whole-episode composition command. It can select
   old successful episode evidence and new recomputed episode evidence only by
   exact closure; the existing same-Job batch command remains unchanged.
6. [done] Wire dynamic runtime capability resolution into Media Preflight. It
   reads the authenticated self-measured local FunASR timing identity and does
   a fresh exact Store lookup before detector/materialization work. Missing
   capability projects to `awaiting_calibration`; a changed or malformed
   binding projects to `recompute_needed`; service unavailability remains
   recoverable `indeterminate`. Do not re-run VLM or physical evidence when
   the target requirement proves historical inputs are reusable.
7. [done] Project an accepted `pc_cuda` capability into a closed, request-facing
   `RuntimeTimedSpeechProjection`.  The projection contains the accepted ASR/VAD
   child record hashes and exact integer timing bounds; it does not derive them
   from the historical CPU policy.  `PcCudaRuntimeTimedSpeechPolicy` then takes
   only operational HTTP/physical limits from the static media policy and all
   CUDA authority from that projection.
8. [done] Add the versioned PC-CUDA timed-speech HTTP contract. The v2 request
   carries one closed accepted projection (capability/measurement/record/
   validation/ASR/VAD refs and bounds), derives a dedicated loopback route, and
   the service independently matches it to its self-measured CUDA process.
   The response echoes that authority exactly. The legacy CPU v1 request and
   `/v1/timed-speech-evidence` remain byte-compatible; the discarded PC `.gpu`
   configuration files are not used. The v2 manifest intentionally omits a
   duplicate `expected_producers` member so the closed projection fits standard
   HTTP header limits; producers are derived only from `runtime_authority`.
9. [done] The sibling CUDA command now persists the five-member
   whole-episode evidence set with `runtime_timed_speech_capability_admission`
   and a normal Store CommandOutcome/Receipt. Its request hash includes the
   full accepted `RuntimeTimedSpeechProjection`; it is a distinct command name
   and cannot use the CPU `local_run` profile or
   `PrepareTimedMediaEvidence@2.1.3`. Its committed reader and batch finalizer
   now have an exact CUDA-only member layout and replay contract.
   The application producer seam is likewise separate: physical evidence keeps
   the static local policy, while ASR/VAD can enter only through the v2 CUDA
   request carrying the command-resolved runtime authority. Its protected
   static-operation-policy hash is pinned in the installed resolver and must
   match the exact closed v2 provenance/policy mapping; a caller cannot choose
   it or route CUDA back through the CPU v1 endpoint.
10. [done] The PC composition injects the exact CUDA resolver and Media
   Preflight chooses only the runtime command/finalizer for a fresh `pc_cuda`
   measurement. Its wire policy and the FunASR service both use
   `/v2/runtime-timed-speech-evidence`; a V1 operation route is rejected by
   producer, Kernel reader, and service. If this CUDA-composed worker reads a
   non-CUDA identity it returns `recompute_needed` rather than silently using
   the CPU command. A CPU worker is deliberately composed without the runtime
   resolver/identity port and keeps its independent historical chain. The
   downstream editorial-input reader dispatches only to the matching CPU or
   CUDA committed-batch reader, never adapts one grammar into the other.
11. [in progress] Apply the migrations to the PC PostgreSQL instance, persist
   an accepted `pc_cuda` CalibrationRecord/Capability, then run a real source
   through the HTTP Pipeline. PC SSH trust renewal is the only current remote
   access prerequisite; do not merge the original checkout's old “force CUDA”
   edits.
