# Design

```text
Committed VLM coarse interval + ProxyTimelineMap
  → claim-owned bounded verified source materialization
  → complete-source transcript coverage + logical candidate assessment
  → VAD protected ranges + audio sample endpoint set
  → video frame endpoint set + shot/scene/visual/subtitle constraints
  → exact A/V feasible-pair evidence
  → Stage 4 canonical selection
```

The command owns retries, expansion budget, provenance and atomic persistence.
Provider/detector adapters produce evidence only. They never return a final
source tick or `pass`; Admission and ExactSpanCompiler independently recompute
membership and policy rules.

## Stage 4 predecessor closure

Preflight is the sole producer of two immutable facts that Stage 4 must read
by committed member reference. `TimedSpeechProfileAdmission@1` resolves a
registry entry with separate Transcript and VAD producer/model/adapter/
calibration requirements against the exact evidence members, source audio
clock and calibrated guard policy. It records no self-declared `pass`; a
consumer replays the match. `sensevoice_word_guard_v1` admits only known
speech protection; a complete-dialogue admission requires a separately
registered/calibrated sentence profile.

`PresentationTimelineProbe@1` records the committed stream/index identities,
time bases, origins, coverage, probe tool and mapping/error-policy refs.
`CommittedVideoToAudioClockMapCertificate@1` is deterministically compiled
from that probe and records its common presentation interval and each
leading/trailing non-overlap. It is never inferred from full stream durations
or constructed by a Stage 4 caller. All four facts (root evidence, candidate
timed evidence index, speech admission, probe/certificate) commit atomically
in the preflight ArtifactSet, binding the same source/root evidence hashes.

## Bounded source materialization boundary

After a fresh command claim, the Store verifies the exact Job-owned BlobRef and
streams it in bounded chunks into an `O_EXCL|O_NOFOLLOW` private file under a
0700 staging directory. It computes hash and byte count while writing, fsyncs,
seals the verified regular file, and returns an idempotent lease. The command
owns this lease across the one permitted `TIMED_SPEECH_BUSY` retry and closes
it in `finally` after success, denial, failure or cancellation. A non-fresh
claim returns before any Blob read, disk reservation or detector invocation.

`max_source_bytes` is frozen request/profile policy and is checked against both
the declared BlobRef and FunASR request bound before materialization. Staging
quota, path and chunk size are operational controls, not evidence identity.
No shared path/cache crosses Jobs. This removes Pipeline/FunASR application RAM
duplication but deliberately does not claim that current source ingestion or
PostgreSQL `bytea` storage is streaming.

The root committed evidence seam also carries a complete source-bound A/V
clock-map certificate, or exact media-probe facts accepted by one shared
deterministic certificate compiler. It is never synthesized as identity by a
Pipeline adapter. Task 06 consumes its committed hash and independently
revalidates coverage, continuity, rational mapping and error bounds.

The certificate is defined on a proven common presentation-timeline interval,
not by linearly mapping the complete video duration to the complete audio
duration. It records stream presentation origins and any leading/trailing
non-overlap. Equal presentation time is converted between exact time bases;
rounding/error bounds are explicit. This matters in the real corpus: 43 of 45
episodes have unequal A/V end times, with observed audio-minus-video tail delta
between -0.026009070s and +0.031995465s.

The first implementation may use deterministic local tools for scene
detection. Production VAD is only the independently identified FSMN-VAD
producer behind `TimedSpeechEvidencePort`; FFmpeg `silencedetect` is not an
allowed substitute. Every detector must publish coverage and error bounds. A
missing detector is not an empty set. Transcript text is bounded to candidate
windows and stored only for boundary proof/diagnostics. Candidate、Event、Story、
Blueprint 与高光评分不得读取其内容；它们的剧情语义只来自 VLM observation。

## FunASR service boundary

The Pipeline process never imports FunASR, ModelScope or Torch. A persistent
single-inference-queue service implements a closed `TimedSpeechEvidencePort`.
Its request binds source/hash, audio time base/origin/duration, requested range
and policy hash. Its response binds coverage/outcome, integer transcript and
FSMN-VAD ticks, boundary-touch/truncation/completeness, model/tool/device hashes
and non-zero timing error bounds. Empty output is complete only with explicit
full coverage and `no_speech`.

The first real profile is `sensevoice_word_guard_v1`: SenseVoiceSmall plus an
independent FSMN-VAD producer on the native host CPU, with
`word_timing_capability=required` and no sentence-boundary capability.
The frozen FunASR build has been measured with `output_timestamp=True` to
return one real timestamp pair per word on the source audio clock. The adapter
must reject missing/misaligned/non-monotonic/out-of-clock word timing instead
of degrading to sentence-only evidence. Word-gap segmentation (`>0.7s`) and
FSMN-VAD merge (`<=0.35s`) are versioned policy values inherited from the
verified historical baseline, not hidden constants. They derive known-speech
protection, never semantic sentences. Final endpoints still come from the
audio sample clock. A future calibrated sentence-boundary model or MPS profile
requires a separate capability/calibration identity and is never a hidden
fallback. Changing model, device, FunASR version or policy changes command
identity and cannot silently resume an older run.

## Authority registry bootstrap lifecycle

Before a deployment enables Pipeline media preflight, an authority administrator
compiles the locked authority source and invokes the explicit
`authority-bootstrap-timed-speech-profile` command with that source root and
the exact profile id/version. The command reads only the hash-bound
`stage_05/timed_speech_profiles.yaml` member captured by the compiled registry
source; it accepts no profile JSON, environment profile selection, or Pipeline
HTTP payload. The compiler's ready `RegistrySet.source_hash` becomes the frozen
`AuthorityRegistrySnapshot.registry_set_sha256`, and the selected entry must
match the snapshot key exactly.

The authority command writes the existing immutable Bootstrap command receipt,
profile member and PostgreSQL anchor atomically. Reinvoking it with the same
compiled hash and profile replays the original result. An existing profile key
bound to a different snapshot or member conflicts; rotation is an explicit
authority deployment change (normally a new profile version) and is never a
silent overwrite. The runtime is then composed with the verified snapshot as a
deployment injection. If it is missing, all-zero, invalid, or lacks its durable
anchor, composition/preflight fails closed before accepting work.

Pipeline HTTP has no route, request field, environment JSON selector, or
auto-seeding path for this operation. It can only dereference the composed
Store-anchored resolver after a fresh preflight claim.
