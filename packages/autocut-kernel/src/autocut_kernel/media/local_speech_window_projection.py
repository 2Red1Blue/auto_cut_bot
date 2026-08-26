"""Project raw native local-window speech measurements into evidence values.

This module only closes the measured local window.  It does not establish a
committed predecessor, calibration admission, edit permission, or model truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from .local_speech_window import LocalSpeechWindowError
from .local_speech_window_codec import (
    DecodedLocalSpeechWindow,
    decode_local_speech_window_response,
)
from .root_evidence import (
    Coverage,
    CoverageOutcome,
    EvidenceCompleteness,
    EvidenceContext,
    MediaKind,
    SpeechActivitySegment,
    SpeechActivitySet,
    SpeechSourceOutcome,
    TranscriptCompleteness,
    TranscriptSegment,
    TranscriptSet,
    TranscriptSourceOutcome,
    TranscriptWord,
)
from .types import TickRange, TimeBase, sha256_prefixed


class LocalSpeechWindowProjectionError(ValueError):
    """The raw native measurement cannot prove an exact local speech window."""


def _integer(value: object, name: str) -> int:
    if type(value) is not int:  # noqa: E721
        raise LocalSpeechWindowProjectionError(f"{name} must be an exact integer")
    return value


def _native_object(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str],
    name: str,
) -> dict[str, object]:
    if type(value) is not list:  # noqa: E721
        raise LocalSpeechWindowProjectionError(f"{name} must be a one-object native result")
    items = cast(list[object], value)
    if len(items) != 1 or type(items[0]) is not dict:  # noqa: E721
        raise LocalSpeechWindowProjectionError(f"{name} must be a one-object native result")
    item = cast(dict[object, object], items[0])
    keys = set(item)
    if not required <= keys <= required | optional:
        raise LocalSpeechWindowProjectionError(f"{name} native result has unknown or missing fields")
    if "key" in item and type(item["key"]) is not str:  # noqa: E721
        raise LocalSpeechWindowProjectionError(f"{name} native key must be text when present")
    return cast(dict[str, object], item)


def local_millisecond_range(
    start_milliseconds: int,
    end_milliseconds: int,
    time_base: TimeBase,
    requested_range: TickRange,
) -> TickRange:
    """Map one local-WAV millisecond range outwards into original clock ticks."""
    start = _integer(start_milliseconds, "native millisecond start")
    end = _integer(end_milliseconds, "native millisecond end")
    if type(time_base) is not TimeBase or type(requested_range) is not TickRange:  # noqa: E721
        raise LocalSpeechWindowProjectionError("millisecond conversion requires exact clock and range")
    if start < 0 or start >= end:
        raise LocalSpeechWindowProjectionError("native millisecond range is invalid")
    scale = 1_000 * time_base.numerator
    start_tick = requested_range.start_pts + start * time_base.denominator // scale
    end_tick = requested_range.start_pts + (
        end * time_base.denominator + scale - 1
    ) // scale
    try:
        converted = TickRange(start_tick, end_tick)
    except ValueError as error:
        raise LocalSpeechWindowProjectionError(
            "native millisecond range has no positive original-clock interval"
        ) from error
    if not requested_range.contains(converted):
        raise LocalSpeechWindowProjectionError("native millisecond range escapes requested window")
    return converted


def _pairs(
    value: object,
    *,
    name: str,
    reject_overlap: bool,
) -> tuple[tuple[int, int], ...]:
    if type(value) is not list:  # noqa: E721
        raise LocalSpeechWindowProjectionError(f"{name} must be a native array")
    raw_pairs = cast(list[object], value)
    result: list[tuple[int, int]] = []
    for position, raw_pair in enumerate(raw_pairs):
        if type(raw_pair) is not list:  # noqa: E721
            raise LocalSpeechWindowProjectionError(f"{name}[{position}] must be an integer pair")
        pair = cast(list[object], raw_pair)
        if len(pair) != 2:
            raise LocalSpeechWindowProjectionError(f"{name}[{position}] must be an integer pair")
        start = _integer(pair[0], f"{name}[{position}].start")
        end = _integer(pair[1], f"{name}[{position}].end")
        if start < 0 or start >= end:
            raise LocalSpeechWindowProjectionError(f"{name}[{position}] is invalid")
        if result and (
            result[-1][0] > start or (reject_overlap and result[-1][1] > start)
        ):
            raise LocalSpeechWindowProjectionError(f"{name} pairs overlap or move backwards")
        result.append((start, end))
    return tuple(result)


def _coverage(context: EvidenceContext) -> Coverage:
    return Coverage(
        context.source_id,
        context.source_sha256,
        context.clock_id,
        context.time_base,
        context.origin_tick,
        context.end_tick,
        CoverageOutcome.COMPLETE,
    )


def _context(decoded: DecodedLocalSpeechWindow, *, asr: bool) -> EvidenceContext:
    request = decoded.request
    extraction = request.extraction
    policy = request.policy
    return EvidenceContext(
        extraction.source_id,
        extraction.source_sha256,
        MediaKind.AUDIO,
        extraction.clock_id,
        extraction.time_base,
        extraction.requested_range.start_pts,
        extraction.requested_range.duration_pts,
        policy.asr_producer_id if asr else policy.vad_producer_id,
        policy.asr_generation_policy_sha256 if asr else policy.vad_generation_policy_sha256,
    )


def _converted_pairs(
    pairs: tuple[tuple[int, int], ...],
    *,
    decoded: DecodedLocalSpeechWindow,
    name: str,
) -> tuple[TickRange, ...]:
    extraction = decoded.request.extraction
    result = tuple(
        local_millisecond_range(start, end, extraction.time_base, extraction.requested_range)
        for start, end in pairs
    )
    if any(previous.end_pts > current.start_pts for previous, current in zip(result, result[1:], strict=False)):
        raise LocalSpeechWindowProjectionError(f"{name} overlap after exact clock conversion")
    return result


def _project_asr(
    decoded: DecodedLocalSpeechWindow,
) -> tuple[TranscriptSet, bool]:
    raw = _native_object(
        decoded.asr_native_output,
        required=frozenset({"text", "timestamp"}),
        optional=frozenset({"words", "key"}),
        name="ASR",
    )
    text = raw["text"]
    timestamps = raw["timestamp"]
    if type(text) is not str or type(timestamps) is not list:  # noqa: E721
        raise LocalSpeechWindowProjectionError("ASR text and timestamp must be exact native values")
    raw_timestamps = cast(list[object], timestamps)
    context = _context(decoded, asr=True)
    coverage = _coverage(context)
    empty = not text.strip()
    words_value = raw.get("words")
    if empty:
        if raw_timestamps or words_value not in (None, []):
            raise LocalSpeechWindowProjectionError("empty ASR must have empty timestamps and words")
        return (
            TranscriptSet(
                "local-speech-window:transcript",
                context,
                coverage,
                TranscriptSourceOutcome.NO_LEXICAL_CONTENT,
                TranscriptCompleteness(
                    EvidenceCompleteness.COMPLETE,
                    EvidenceCompleteness.COMPLETE,
                    EvidenceCompleteness.NOT_APPLICABLE,
                ),
                (),
                (),
                (),
            ),
            False,
        )
    if type(words_value) is not list:  # noqa: E721
        raise LocalSpeechWindowProjectionError("nonempty ASR requires one word for each timestamp")
    raw_words = cast(list[object], words_value)
    if not raw_words or len(raw_words) != len(raw_timestamps):
        raise LocalSpeechWindowProjectionError("nonempty ASR requires one word for each timestamp")
    words_text: list[str] = []
    for position, value in enumerate(raw_words):
        if type(value) is not str or not value.strip():  # noqa: E721
            raise LocalSpeechWindowProjectionError(f"ASR word[{position}] must be nonempty text")
        words_text.append(value.strip())
    pairs = _pairs(raw_timestamps, name="ASR timestamp", reject_overlap=True)
    if len(pairs) != len(words_text):
        raise LocalSpeechWindowProjectionError("ASR word/timestamp cardinality differs")
    ranges = _converted_pairs(pairs, decoded=decoded, name="ASR timestamp")
    words = tuple(
        TranscriptWord(
            f"local-word-{position:08d}",
            context.source_id,
            context.source_sha256,
            context.clock_id,
            context.time_base,
            interval.start_pts,
            interval.end_pts,
            word,
        )
        for position, (word, interval) in enumerate(zip(words_text, ranges, strict=True))
    )
    gap = decoded.request.policy.utterance_gap_milliseconds
    groups: list[tuple[int, int]] = []
    group_start = 0
    for position in range(1, len(words)):
        if pairs[position][0] - pairs[position - 1][1] > gap:
            groups.append((group_start, position))
            group_start = position
    groups.append((group_start, len(words)))
    segments = tuple(
        TranscriptSegment(
            f"local-segment-{position:08d}",
            context.source_id,
            context.source_sha256,
            context.clock_id,
            context.time_base,
            words[start].in_tick,
            words[end - 1].out_tick,
            (),
            "".join(word.text for word in words[start:end]),
        )
        for position, (start, end) in enumerate(groups)
    )
    return (
        TranscriptSet(
            "local-speech-window:transcript",
            context,
            coverage,
            TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
            TranscriptCompleteness(
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.NOT_APPLICABLE,
            ),
            segments,
            words,
            (),
            words[0].in_tick <= context.origin_tick,
            words[-1].out_tick >= context.end_tick,
        ),
        True,
    )


def _project_vad(decoded: DecodedLocalSpeechWindow) -> tuple[SpeechActivitySet, bool]:
    raw = _native_object(
        decoded.vad_native_output,
        required=frozenset({"value"}),
        optional=frozenset({"key"}),
        name="VAD",
    )
    pairs = _pairs(raw["value"], name="VAD value", reject_overlap=False)
    merged: list[tuple[int, int]] = []
    gap = decoded.request.policy.vad_merge_gap_milliseconds
    for start, end in pairs:
        if merged and start - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    ranges = _converted_pairs(tuple(merged), decoded=decoded, name="VAD value")
    context = _context(decoded, asr=False)
    segments = tuple(
        SpeechActivitySegment(
            f"local-speech-{position:08d}",
            context.source_id,
            context.source_sha256,
            context.clock_id,
            context.time_base,
            interval.start_pts,
            interval.end_pts,
            None,
        )
        for position, interval in enumerate(ranges)
    )
    return (
        SpeechActivitySet(
            "local-speech-window:vad",
            context,
            _coverage(context),
            SpeechSourceOutcome.SPEECH_DETECTED if segments else SpeechSourceOutcome.NONE_DETECTED,
            segments,
        ),
        bool(segments),
    )


@dataclass(frozen=True, slots=True)
class LocalSpeechWindowEvidence:
    """Raw-bound local speech evidence, not an admission or edit result."""

    decoded: DecodedLocalSpeechWindow
    transcript: TranscriptSet
    speech_activity: SpeechActivitySet

    def __post_init__(self) -> None:
        if (
            type(self.decoded) is not DecodedLocalSpeechWindow  # noqa: E721
            or type(self.transcript) is not TranscriptSet  # noqa: E721
            or type(self.speech_activity) is not SpeechActivitySet  # noqa: E721
        ):
            raise LocalSpeechWindowProjectionError("projection requires exact decoded and evidence values")
        sha256_prefixed(self.decoded.response_sha256, "local speech response hash")


def project_local_speech_window(decoded: DecodedLocalSpeechWindow) -> LocalSpeechWindowEvidence:
    """Project one raw local-window ASR/VAD response without inferring semantics."""
    if type(decoded) is not DecodedLocalSpeechWindow:  # noqa: E721
        raise LocalSpeechWindowProjectionError("projection requires an exact decoded local speech window")
    try:
        replayed = decode_local_speech_window_response(decoded.raw_response, decoded.request)
    except LocalSpeechWindowError as error:
        raise LocalSpeechWindowProjectionError("local speech raw response cannot be replayed") from error
    if replayed.response_sha256 != decoded.response_sha256:
        raise LocalSpeechWindowProjectionError("local speech raw response hash differs from decoded binding")
    transcript, has_words = _project_asr(replayed)
    speech, has_speech = _project_vad(replayed)
    if has_words and not has_speech:
        raise LocalSpeechWindowProjectionError("word timestamps require observed VAD speech")
    if not has_words and not has_speech:
        transcript = TranscriptSet(
            transcript.transcript_set_id,
            transcript.context,
            transcript.coverage,
            TranscriptSourceOutcome.NO_SPEECH,
            transcript.completeness,
            (),
            (),
            (),
        )
    return LocalSpeechWindowEvidence(replayed, transcript, speech)


__all__ = [
    "LocalSpeechWindowEvidence",
    "LocalSpeechWindowProjectionError",
    "local_millisecond_range",
    "project_local_speech_window",
]
