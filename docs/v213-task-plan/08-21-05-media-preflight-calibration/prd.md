# VLM-First Timed Evidence and Boundary Calibration

## Goal

For real Doubao coarse semantic intervals, commit the timed audio/video
evidence consumed by Stage 4 without granting VLM physical-cut authority.
Stage 1–3 consumes only VLM semantic observations and owner-bound source/window refs.
The task replaces the old ASR→VAD→visual fallback meaning with a conjunctive,
evidence-bound alignment flow. It creates no publish permission.

## Requirements

- Depend on committed SourceManifest/WindowManifestSet/ProxyTimelineMap and
  Doubao VlmObservationSet; a VLM interval is only a coarse candidate with an
  uncertainty bound.
- `PrepareTimedMediaEvidence` adaptively expands each candidate window and
  atomically commits TranscriptSet, SpeechActivitySet, FramePtsIndex,
  AudioSampleBoundarySet, ShotBoundarySet/SceneBoundarySet,
  VisualValiditySet, SubtitleCueSet and coverage/provenance records.
- ASR, VAD, decoded frame/sample membership, visual validity and subtitle
  clearance are conjunctive constraints. Success in one producer never
  short-circuits the others.
- ASR/Transcript text is not a剧情、事件或高光语义来源。它只证明对白区间、
  句段闭合和候选边缘是否会吞音；Stage 1–3、Story Proposal 与高光评分只能
  使用已提交的 Doubao VLM observations。
- The first production profile performs one complete-source SenseVoice/FSMN
  inference per committed source window and derives candidate expansion
  assessments from that root evidence. It must not claim candidate-local
  re-inference. Each assessment records coverage, boundary touch, truncation
  and sentence capability; a future local re-inference profile needs its own
  source-bound request identity and policy.
- Timed-media production materializes an immutable source BlobRef through a
  bounded, private verified-file lease after the fresh Kernel claim. It never
  hands a full source `bytes` value across Store → Pipeline → FunASR. The
  frozen request/profile byte limit is checked before disk or provider work;
  replay performs no materialization or detector call.
- OCR text is not a semantic input. Burned-in/embedded subtitle timing remains
  required as a safety outcome; unrun detection cannot mean `none_detected`.
- Every artifact binds source/proxy clocks, policy/detector versions, error
  bounds and immutable evidence hashes. Missing authorization, map/hash/clock
  disagreement, detector gaps and `unknown` required regions fail closed.
- The succeeded timed-media ArtifactSet must additionally commit exactly one
  immutable `TimedSpeechProfileAdmission` and one probe-derived
  `CommittedVideoToAudioClockMapCertificate`. The former resolves a registered
  profile against independent Transcript and VAD producer/calibration records;
  the latter derives only from committed probe/index facts and explicitly
  records the common A/V presentation interval and non-overlap tails. Neither
  fact may be caller-created, inferred as identity, or re-created by Stage 4.
- Persist a complete source-bound video-to-audio presentation-map certificate (or the
  exact committed probe facts from which the shared Kernel deterministically
  derives it). Runtime-only identity assumptions are forbidden. Stage 4 must
  bind the committed certificate/fact hash into selection, proof, report and
  independent Admission. Completeness means the common proven presentation
  interval plus explicit leading/trailing non-overlap, not forced equality of
  the two stream durations.
- Calibration-dependent values such as subtitle clearance are produced by a
  CalibrationRecord; no zero/default value is silently promoted to policy.
- Production Transcript/SpeechActivity evidence comes from a standalone,
  version-pinned FunASR service through `TimedSpeechEvidencePort`.
  SenseVoiceSmall plus an independent FSMN-VAD producer is the first real
  mixed-language drama profile; Whisper CLI and FFmpeg `silencedetect` are
  forbidden production substitutes.
- The host CPU profile is the first reproducible baseline. MPS is admitted only
  by a CalibrationRecord from the same golden corpus; Podman CPU is a portable
  fallback and never evidence that Apple Metal acceleration was exercised.

## Acceptance Criteria

- [ ] At least one real Doubao observation is expanded into complete local
  Transcript/VAD/video/audio/scene/subtitle evidence in the real `autocut`
  PostgreSQL database through additive migrations and a unique Job identity;
  test fixtures must never drop the real schemas.
- [ ] VLM proxy ticks map to source only through verified ProxyTimelineMap (or
  an independently verified identity certificate); pure float-second offset
  translation is rejected.
- [ ] ASR hit with VAD/visual/subtitle failure, truncated dialogue, missing
  audio, unknown visual class and unproved subtitle outcome all deny or remain
  indeterminate and never invoke ExactSpanCompiler.
- [ ] Replay returns the exact committed ArtifactSet without repeated ASR or
  detector work.
- [ ] The committed ArtifactSet contains or transitively binds a complete A/V
  presentation-map fact; nonidentity/piecewise mapping and unequal stream tails
  are representable without duration stretching, and missing,
  discontinuous, source-mismatched or tampered mapping fails closed before
  ExactSpanCompiler.
- [ ] A forged/unregistered speech profile, transcript-only producer match,
  VAD producer/calibration mismatch, partial/truncated evidence, synthetic
  clock map, or probe/certificate hash mismatch cannot produce a profile
  admission or Stage 4-consumable evidence reference.
- [ ] The FunASR response binds source/audio clocks, content and model hashes,
  requested coverage, integer ticks, non-zero error bounds and distinct ASR/VAD
  producer identities; empty arrays, model drift and device-profile drift fail
  closed.
- [ ] 首个生产 profile 固定为 `sensevoice_word_guard_v1` 且
  `word_timing_capability=required`。SenseVoiceSmall
  只接受 `output_timestamp=True` 返回的真实且一一对应的 `words/timestamp`；
  ASR 声称 lexical words 存在时，数量不等、时间非单调、越出 source clock
  或时间缺失均 fail closed。ASR 显式 `no_lexical_content` 且 FSMN-VAD
  `speech_detected` 时必须产生 VAD-only protected ranges，不伪造 TranscriptWord，
  也不因非词语声音直接失败。它的 `sentence=not_applicable` 只表示未支持
  sentence-boundary capability，绝不表示完整对白。未来 sentence-boundary profile
  必须单独注册/校准，不能静默替代当前 profile；任何 profile 都禁止插值伪造 word tick。
- [ ] 大于冻结 source-byte 限制的 BlobRef 在读取内容、预留 staging、调用
  detector 或 FunASR 前被拒绝；多 chunk 传输验证 hash/length，任意失败、取消、
  retry 耗尽或 commit 失败都清理私有路径和配额租约。并发 replay 不会二次
  materialize，同一 busy retry 复用同一验证后的文件。
- [ ] Calibration fixtures bind non-zero timing error allowances and policy
  hashes; production/publication stays closed.

## Planning Record

This task is the real evidence bridge between Doubao semantic candidates and
the exact A/V span compiler. It must not import the legacy fallback aligner.
