# Candidate-local ASR/VAD I/O follow-up

## Status and scope

This is an evidence-backed follow-up plan, not an implemented production path.
The current [PRD](prd.md) explicitly describes the first profile as complete-source
SenseVoice/FSMN inference followed by candidate-window assessments. The new
candidate-local physical guard and exact compiler do not change that producer
behavior or establish real local inference acceptance.

Preserve the existing ownership boundaries: committed Source facts own physical
source identity, frames, samples and presentation clocks; VLM owns semantic
candidates and editing modes; SenseVoice/FSMN supply physical speech protection
only. This work does not introduce ASR semantics, choose a final edit, grant
Stage4 Admission, or authorize publication.

## Actual current path and constraints

Paths below are repository-relative; line anchors identify the inspected code
and may move in later commits.

| Boundary | Current behavior |
|---|---|
| [HTTP run route](../../../auto_cut_bot/api/server.py#L640) and [runtime composition](../../../auto_cut_bot/pipeline/runtime/composition.py#L483) | The durable worker invokes the composed media-preflight stage, not a separate candidate-ASR endpoint. |
| [MediaPreflight stage](../../../auto_cut_bot/pipeline/runtime/media_preflight_stage.py#L395) | Executes one Prepare command per episode, then the exact batch finalizer. Installed policy/Source/VLM checks precede producer work. |
| [Local port](../../../auto_cut_bot/pipeline/media_preflight/port.py#L371) and [speech request builder](../../../auto_cut_bot/pipeline/media_preflight/port.py#L1388) | Calls `_speech_port.produce` once with the entire source audio extent. |
| [TimedSpeechEvidenceRequest](../../../auto_cut_bot/pipeline/media_preflight/speech_port.py#L107) | Explicitly rejects a requested range different from the complete source range. |
| [HTTP response decoder](../../../auto_cut_bot/pipeline/media_preflight/funasr_http.py#L234) and [coverage decoder](../../../auto_cut_bot/pipeline/media_preflight/funasr_http.py#L311) | Constructs source-wide contexts and complete requested coverage. Simply supplying a local range does not produce a valid local context/coverage contract. |
| [Service endpoint](../../../deploy/funasr/service.py#L1214) and [native inference](../../../deploy/funasr/service.py#L756) | Rejects non-full-source requests, then passes the complete uploaded MP4 to both SenseVoice and independent FSMN inference. |
| [Candidate closure](../../../packages/autocut-kernel/src/autocut_kernel/pipeline/prepare_timed_media_evidence_command.py#L942) | Expands windows by reassessing the same root evidence. Each candidate contains the root Transcript/VAD; expansion does not call a producer again. |
| [Committed reader](../../../packages/autocut-kernel/src/autocut_kernel/pipeline/committed_timed_media.py#L371) | Recomputes that root-derived closure and requires exact equality. An independently measured local result currently cannot replace it. |

The request/service/reader contracts all need a coordinated change. Changing
only the port call, trimming an existing transcript, or slicing a full-source
model response is not candidate-local inference.

## Required root and coverage contract separation

Keep the original source-global **physical** evidence and its identity intact:
FramePtsIndex, AudioSampleBoundarySet, Shot/SceneBoundary, VisualValidity,
SubtitleCue, source/probe provenance, and the committed piecewise presentation
certificate. Local speech must refer to this original physical evidence, not
replace its transcript and silently create a differently hashed root.
Preserve the physical sets' actual hashes; a new aggregate contract has its own
honestly computed identity and must not reuse an old aggregate hash.

The current [RootMediaEvidenceBundle constructor](../../../packages/autocut-kernel/src/autocut_kernel/media/root_evidence.py#L1129)
requires Transcript/VAD as well as all physical sets, with complete source-wide
coverage. [Timed-profile admission](../../../packages/autocut-kernel/src/autocut_kernel/media/stage4_predecessor.py#L470)
also consumes root Transcript/VAD. Therefore **removing whole-source ASR requires
an explicit split of the global physical-root and local speech contracts**,
including their persisted codecs and readers. Preserve historical records; do
not reinterpret old bytes or silently add defaults.

In particular, unrun whole-source ASR/VAD must never become empty `NO_SPEECH`,
`NONE_DETECTED`, `NOT_APPLICABLE`, or fabricated `COMPLETE` coverage. Adding real
local calls while retaining the old whole-source ASR would be a transitional
path, not completion of the no-whole-source-inference objective.

Local speech carries its actual source identity, original audio clock, measured
range, coverage and boundary/truncation facts. Complete coverage of a local
measurement does not mean complete coverage of the source. Compare coverage
using absolute rational presentation time; do not subtract each stream's origin
independently or stretch unequal stream durations. Requested audio extraction
must derive from committed clock/sample facts and an explicit extraction policy;
unproved gaps cannot be covered by joining two mappable endpoints.

## Smallest implementation sequence

1. **Freeze the physical-root/local-speech contract split and one-window wire.**
   Keep the physical sets and certificate owned by their existing domain. Define
   a closed local measurement request binding exact Source/VLM/candidate owners,
   candidate-window hash and expansion ordinal, original audio clock, requested
   range, extraction/mapping policy, installed profile/calibration, and byte
   limits. Define the corresponding raw-response/projection/provenance records.
   Update root/profile-admission consumers explicitly; do not add a compatibility
   arm that treats missing speech as measured silence.
2. **Implement one genuine local producer call.**
   In `speech_port.py`, `funasr_http.py`, and `deploy/funasr/service.py`, separate
   the original source extent from the requested extraction range. Verify the
   original file before bounded extraction; send only the requested audio to
   the models. Bind the actual extraction and convert model-local timestamps
   back onto the original audio clock without guessed offsets or interpolated
   words. Reuse the verified-file transport and independent ASR/VAD identity
   validation. Initially uploading the original file per call is possible but
   still consumes I/O; it must not be reported as zero-copy or optimized reuse.
3. **Add a claim-owned window child command and drive real expansion.**
   Reuse the existing deterministic Command/Receipt/ArtifactSet operations. A
   window child request identifies its exact candidate and ordinal, plus the
   previous committed window result for a successor. The producer seam can be
   `produce_window(resolved_request, verified_source, window)`; this is a proposed
   interface, not an API already present. Kernel planning uses the existing
   [planner and advance functions](../../../packages/autocut-kernel/src/autocut_kernel/media/timed_evidence.py#L643):
   read/measure one window, derive its assessment, then either close, request the
   next window, or fail at the frozen expansion bound. Neither the adapter nor
   the model chooses that control flow.
4. **Persist and independently replay the measured chain, then connect Runtime.**
   Change Prepare/its finalizer and `committed_timed_media.py` to bind exact child
   receipts and all measured window records, rather than regenerate candidates
   solely from root speech. Replay checks every request/response/projection,
   ordinal, assessment and expansion transition against committed Source/VLM,
   installed profile and calibration. Preserve the complete candidate set and
   compact episode-level references. Update `port.py` and the Runtime adapter
   only after these contracts close. No successful batch may omit an exhausted,
   missing or failed candidate.

Process one episode/window at a time and release heavy intermediate results.
Reuse explicit evidence-read and materialization budgets, including cumulative
batch accounting; never derive JSON memory limits from the much larger source
file cap. Request/response and extraction budgets must be explicit and bound,
not hidden fallback constants.

## Reusable persistence and the real recovery limit

The existing Prepare command already provides exact Source/VLM rereads before
claim, a private verified-file lease, immutable Blob writes, and atomic generic
success/rejection Receipts. Reuse those operations; no new database or general
workflow framework is needed for a correctly fail-closed first window command.

However, [current Prepare execution](../../../packages/autocut-kernel/src/autocut_kernel/pipeline/prepare_timed_media_evidence_command.py#L473)
returns any non-fresh claim without redispatch. Its one `TIMED_SPEECH_BUSY` retry
is in-memory and retries the entire producer call; it is not a durable per-window
attempt journal. A succeeded window can replay with zero HTTP calls. An unknown
in-flight outcome after a crash must remain indeterminate, not automatically
issue a second native call.

The [shadow-measurement command](../../../packages/autocut-kernel/src/autocut_kernel/pipeline/measure_shadow_calibration_command.py#L325)
has invoking/staged/recovery state, but that protocol is calibration-specific.
Do not disguise ordinary candidate measurements as shadow calibration or as
text-generation attempts. If resumable staged native responses are required,
scope that persistence change explicitly after the window request/result
contract; ordinary claims alone do not provide it.

## Parallel ownership and Stage4 independence

After the shared wire is frozen, bounded work can proceed independently:

- **Kernel owner:** physical/local contract split, window child command,
  assessment replay, exact readers and pure mutation tests.
- **Producer owner:** request/response codec, verified local extraction and
  native service integration; no planner or semantic decisions.
- **Runtime/test owner:** adapter wiring and coherent Source/VLM/window fixtures
  using the frozen APIs; no fake successful admissions.

This evidence-production slice can be developed and tested before Stage4
Admission. It must end at committed, independently replayable local evidence;
candidate guard/clock checks are useful consumers, not substitutes for the later
physical evaluator, canonical edit selection, Recipe or publication decision.

## Calibration and activation consequence

The service's [service_hash](../../../deploy/funasr/service.py#L271) hashes its
source bytes; [detector_hash](../../../deploy/funasr/service.py#L297) includes that
service identity. Installed policy validation binds the native service and both
producers. Changing local extraction or response behavior therefore changes
locked identities, even if model weights remain unchanged. Existing accepted
CalibrationRecords remain historical facts; they do not automatically attest
the new extraction path. Real activation requires matching installed identities
and calibration evidence covering the new timing/extraction behavior. Unit
fixtures or copied old bounds cannot establish that compatibility.

## Acceptance for this follow-up

- A touching initial window causes a second **actual local** inference with a
  distinct bound request; a closed first result causes exactly one call.
- The model receives only the requested decoded audio, not a whole-episode
  inference whose output is subsequently trimmed. Tests inspect the actual
  model input boundary; mocked outputs alone are insufficient evidence of it.
- Nonzero/negative origins, unequal stream tails and non-integral conversions
  preserve original ticks. Gaps, truncated/unknown results and local roll
  overrun fail closed. Word-only evidence never proves complete dialogue.
- Full candidate/expansion identity, both ASR/VAD producers, calibration,
  raw-response hashes and source authorization replay independently. Rehashed
  foreign responses, omitted windows and changed predecessors reject.
- Successful child and final-batch replay performs no HTTP/native work;
  interrupted unknown calls do not silently repeat. Failed/exhausted children
  cannot produce a successful partial batch.
- Byte limits are checked before large allocations; leases and extraction
  files are released on success, rejection, timeout and cancellation. Multiple
  episodes do not retain all heavy decoded speech/root payloads simultaneously.
- Pure tests and service tests with synthetic model output are labelled as
  such. Real acceptance separately exercises native local inference and durable
  restart/replay with matching calibration; it is not claimed by this document.
