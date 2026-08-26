# Implementation Plan

Current wave: [Committed-media integration](production-integration-wave.md).
Start with strict existing-wire evidence codecs, then an exact five-member and
batch reader, actual Stage3/Catalog join, committed piecewise-clock consumption,
physical Admission and production A/V Recipe/Command. The steps below retain
the target cut-quality and real-acceptance requirements.

1. Require the committed media-preflight execution profile, real timed
   evidence ArtifactSet and presentation-timeline A/V clock certificate from
   Task 05; reject full-range duration interpolation and unproved identity.
2. Close VLM coarse-anchor/editing-mode request DTOs and the hash-bound
   `TimedSpeechProfile` union before candidate-domain code. Implement
   `sensevoice_word_guard_v1` as deterministic word-gap/VAD known-speech
   protection and reserve required dialogue for a distinct calibrated
   sentence-boundary profile; ASR text remains unreachable outside Stage 4.
3. Implement calibrated dialogue/action alignment profiles as candidate-domain
   construction in the shared kernel, not as a greedy fallback function.
4. Extend ExactSpan to the complete A/V relation over real frame/sample clocks,
   canonical word-gap/VAD rolled protection, stable shot, visual and subtitle
   constraints. It must derive the closed dialogue-guard arm rather than use a
   shared `pass` flag.
5. Persist complete proofs/report including profile/policy/calibration refs and
   add independent Admission recomputation of guard arm, protected ranges and
   endpoint safety.
6. Convert pinned historical examples into integer-tick safety/style fixtures,
   including the legacy invalid `start>=end` counterexample and the complete
   word-only/non-dialogue success, word-only/required-dialogue indeterminate,
   sentence-profile required-dialogue, VAD-only non-lexical, audio-bearing
   not-applicable rejection, and video-only/no-audio cases.
7. Run a real current-drama VLM interval through Pipeline HTTP -> PostgreSQL ->
   Stage 4 and verify deterministic replay. Check the Agent adapter produces the
   same command/result hashes.
8. Run unit/integration/real-media tests, Ruff/type check and independent
   review, then commit immediately. Publication remains closed.
