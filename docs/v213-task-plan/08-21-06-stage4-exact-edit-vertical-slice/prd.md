# Stage 4 Exact A/V Edit — VLM-first Production Slice

## Goal

Compile one real Doubao VLM coarse interval from the current drama against
committed MediaEvidence into a non-empty integer-tick A/V SpanVariant and
Recipe, or an explicit failure. The slice preserves the proven historical
SenseVoice/FSMN/stable-shot cut quality while enforcing the new source-clock,
evidence, exact-search and independent-Admission boundaries. It grants no
publication permission.

## Requirements

- Consume only committed root input plus an owner-bound real VLM observation.
  Until Stage 1--3 is live, a marked editorial fixture may project the exact
  observation/candidate refs; the production profile rejects caller dicts,
  paths and unbound float seconds.
- Endpoints are real FramePtsIndex frame PTS and AudioSampleBoundarySet sample
  ticks joined through a committed presentation-timeline clock-map certificate.
  The certificate maps equal source presentation time with exact rational
  arithmetic over the common A/V availability interval; it must not stretch
  the full video range onto the full audio range when stream durations differ.
- The first registered `sensevoice_word_guard_v1` profile derives utterance
  protected ranges deterministically from complete real SenseVoice word ticks
  using the frozen word-gap policy. It proves known-speech non-truncation only,
  never semantic sentence completeness. FSMN-VAD independently expands speech
  protection for ASR-missed/non-lexical audio; it never proves complete
  dialogue and never substitutes for a physical sample endpoint.
- A complete-dialogue Blueprint requires the distinct registered and
  calibrated `sentence_boundary_guard_v1` profile with complete source-clock
  sentence evidence. `sentence=not_applicable` under the word-guard profile
  is valid only for an audio-bearing `not_required` guard or a video-only
  `not_applicable/no_audio` guard; it never upgrades to complete dialogue.
- VLM is the sole semantic owner of the coarse interval and editing mode.
  `dialogue` uses dialogue-safe lead/tail preferences; `action` uses a
  forward-biased stable-shot preference. If both modes are present, dialogue
  safety dominates. ASR text/word count/strength never selects the mode.
- Stable shot, visual validity and positive subtitle clearance are hard
  constraints conjunctive with Transcript/VAD/frame/sample/clock-map checks.
  ASR, VAD or visual success never short-circuits another producer. Unknown or
  partial evidence has no fallback.
- Historical three-tier ordering is a candidate-preference baseline only. The
  shared ExactSpan compiler owns the complete feasible relation and canonical
  choice; independent Admission recomputes dialogue, visual, subtitle and A/V
  rules. No old `autocut_core` aligner import is allowed.
- Missing feasible pairs produce an exact report and deny/repair outcome,
  never a raw VLM endpoint or empty Recipe.

## Acceptance Criteria

- [ ] A real current-drama VLM observation plus real SenseVoice words,
  FSMN-VAD, frames, samples, shot/visual/subtitle evidence produces the same
  non-empty A/V Recipe/Report hash on repeated runs.
- [ ] Historical `when-lucifer-kneels` fixtures cover ASR-utterance, VAD-only,
  visual-only, onset/tail protection and action-forward cases. The new result
  need not byte-match legacy float seconds, but must preserve the expected
  cut-style invariant and pass stricter physical safety checks.
- [ ] White/black/frozen/transition/unknown, short-shot and subtitle fixtures
  reject invalid endpoints and either choose the canonical legal PTS or fail
  explicitly.
- [ ] Float, cross-clock, out-of-bounds and partial-media inputs produce no
  Recipe.
- [ ] A/V streams with equal starts but unequal tails map by presentation time,
  expose the non-overlap explicitly and never introduce duration-ratio drift;
  endpoints outside the common interval fail when audio is required.
- [ ] A word-guard profile with complete words/VAD produces an exact
  `not_required` known-speech protection proof for a non-dialogue requirement;
  the same evidence under `dialogue_integrity=complete` is indeterminate and
  produces no Recipe.
- [ ] A registered sentence-boundary profile with complete sentence/word/VAD
  evidence can satisfy a required dialogue guard; VAD confidence, word gaps,
  punctuation-like text, or synthetic sentence records cannot substitute it.
- [ ] `no_lexical_content + speech_detected` remains VAD-only protected
  evidence for a non-dialogue candidate, while audio-bearing
  `not_applicable` Transcript/VAD and all malformed word timings fail closed.
- [ ] `no_lexical_content + VAD speech_detected` contributes a VAD-only
  protected range for a non-dialogue candidate without fabricated words;
  claimed lexical words with missing/misaligned timestamps fail closed.
- [ ] The same inputs/policy yield identical SpanVariant/RuleResult/Recipe in
  Pipeline and Agent Runtime conformance adapters.

## Planning Record

The authoritative algorithm is Stage 4 contract section 4.1 plus the
compatible-algorithm principles. Historical artifacts are regression evidence
only and never an executable dependency.
