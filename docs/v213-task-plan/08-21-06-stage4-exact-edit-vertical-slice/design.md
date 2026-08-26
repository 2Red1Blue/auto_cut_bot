# Exact Physical A/V Edit Design

Current implementation sequence and file ownership are in
[Committed-media integration wave](production-integration-wave.md).
Root/timed evidence codecs and exact readers come before physical admission;
existing fixture Recipe/Render and root-derived primitive clock maps are not
production authority.

```text
Doubao VLM coarse interval + VLM editing modes
  + SenseVoice word ticks -> deterministic utterance protected ranges
  + FSMN-VAD -> independent speech protected ranges
  + FramePtsIndex/Shot/VisualValidity/SubtitleCue
  + AudioSampleBoundarySet/VideoToAudioClockMap
        -> closed start/end candidate domains
        -> conjunctive hard-rule filtering
        -> exact canonical A/V selection
        -> independent Admission recomputation
        -> non-empty SpanVariant/Recipe or explicit failure
```

The historical `ASR -> VAD -> visual` function is not copied. Its useful
behaviour becomes deterministic candidate construction and decision-key
preferences. Every selected endpoint still has to satisfy all evidence layers.

## Historical capability projection

- SenseVoice `output_timestamp=True` words become `TranscriptWord` on the
  source audio clock under the closed, hash-bound
  `sensevoice_word_guard_v1` profile.
- A frozen `word_gap_threshold` derives ordered utterance protected ranges;
  these are physical evidence, not semantic sentences. Combined with merged
  FSMN-VAD ranges and frozen rolls they prove only that a selected audio span
  does not truncate known speech.
- FSMN-VAD ranges are unioned into protection for ASR misses and non-lexical
  vocal events. They do not satisfy `dialogue_integrity/complete`.
- `sentence_boundary_guard_v1` is a distinct future profile. It is admissible
  for complete dialogue only with registered calibration, complete
  source-clock sentence coverage and closed word membership. A field named
  `sentence_info`, punctuation inference, a word gap or a VAD segment does not
  establish this profile.
- PySceneDetect boundaries become ShotBoundary evidence; stable-shot checks
  additionally require VisualValidity and subtitle clearance.
- Legacy dialogue/action parameters become calibrated, versioned alignment
  profiles. Mode is selected only from VLM Candidate capability. Mixed mode
  uses dialogue safety dominance.

## Candidate and proof ownership

The compiler creates ordered video-start/video-end and audio-start/audio-end
domains around the VLM uncertainty/search windows. Physical endpoints are only
decoded frames and samples. Transcript/VAD/shot entries are anchors/protected
ranges, never endpoints. Exact A/V pairing and canonical selection are the
single optimizer; the alignment profile cannot hide a second greedy winner.

The report persists domain count/hash, feasible relation hash, selected key,
BoundaryProof and DialogueIntegrityProof. Admission recomputes evidence
membership and safety. Raw VLM time, missing evidence, max-shift overflow,
`start >= end` and no legal pair are failures, not fallback success.

## Dialogue guard capability boundary

The compiler and Admission independently derive one closed
`SourceDialogueGuardEvidence` arm and the canonical protected-range hash:

- `required`: only a complete-dialogue Blueprint and
  `sentence_boundary_guard_v1` with complete sentence proof;
- `not_required`: an audio-bearing candidate without a complete-dialogue
  requirement, using either calibrated sentence evidence or the SenseVoice
  word/VAD known-speech guard;
- `not_applicable/no_audio`: video-only media with every audio-dependent
  evidence set explicitly not applicable.

An audio-bearing request needing complete dialogue with sentence
`not_applicable`, partial or unknown is indeterminate/quarantined. It cannot
silently change to `not_required`, even when VAD is confident. Profile ID,
profile version, model/adapter hashes, word-gap/VAD-roll policy, calibration
and source-clock coverage bind the proof and Admission result.

## A/V presentation timeline

Video and audio PTS are separate integer clocks but share a demuxed source
presentation timeline only when a committed probe certificate proves their
clock transforms. The certificate records each stream clock/time base,
presentation origin, covered presentation interval, mapping/error policy and
probe/tool identity. Its usable domain is the intersection of the two proven
presentation intervals. Leading/trailing single-stream media is recorded as
non-overlap; it is not hidden by scaling one complete stream duration onto the
other.

For a video tick, conservative audio bounds are derived from equal rational
presentation time and then snapped to committed audio sample boundaries under
the calibrated error allowance. The current duration-ratio interpolation over
both complete stream endpoints is not a valid production certificate. On the
real 45-episode corpus, 43 episodes have unequal A/V end times (observed tail
delta from -0.026009070s to +0.031995465s), so identity/full-range stretching
would be a real correctness bug rather than a theoretical edge case.

## Migration fixtures

The non-normative corpus `jobs/when-lucifer-kneels` is pinned by the hashes in
the compatible-algorithm document. Fixtures must include ASR-containing,
nearest-utterance, VAD-only, visual-only, short-shot/white-flash, subtitle,
onset/tail and the historical invalid-range counterexample. Acceptance is
defined by safety invariants and calibrated style envelopes, not exact legacy
float equality.
