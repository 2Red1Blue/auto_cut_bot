# Requirements

The PC must run the current branch against the 50-episode authorized source
corpus using its RTX 4080, fresh v2.1.3 PostgreSQL database, SenseVoiceSmall,
FSMN-VAD, and the existing private Doubao credentials.  The implementation must
not trust or relabel the existing GPU shadow profile because its service hash is
for a different source revision.

Acceptance requirements:

1. A current-source GPU shadow profile is a distinct, versioned grammar and
   enables only shadow calibration endpoints.
2. Normal `/v1/timed-speech-evidence` stays denied unless an independently
   accepted CalibrationRecord and installed local-run authority are present.
3. A rebuilt GPU image has only loopback exposure, read-only model mounts, a
   single-instance operating contract, and bounded resources.
4. Shadow measurement is durable, independently recomputed, and cannot turn a
   raw provider response into local-run authority by itself.
5. The resulting real Pipeline runs from WSL, not native Windows Python, so
   SourcePrep keeps its POSIX fail-closed filesystem guarantees.
6. Missing or incompatible local calibration must not prevent the Pipeline
   control plane from starting, reading a complete historical evidence closure,
   or running work that does not require new timed-speech evidence.
7. A model, timing-policy, source/proxy timeline, or semantic-input change must
   create a new immutable requirement/generation only for the affected evidence.
   It is not a retry and must never overwrite a historical Artifact or Receipt.
8. A PC and a Mac may retain distinct accepted calibration capabilities. PC
   evidence may be imported and reused on Mac only through its complete,
   exact immutable closure; the PC capability never licenses Mac to generate
   new timed-speech evidence.
9. A new whole-episode aggregate may select exact successful child evidence from
   more than one origin Job, but only when every target episode has one matching
   requirement fingerprint and the completed aggregate is atomically closed.
