"""Pure projection checks for native local speech-window measurements."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from autocut_kernel.media.local_audio_window import LocalAudioWindowSpec
from autocut_kernel.media.local_speech_window import (
    DecodedLocalPcmReport,
    LocalSpeechWindowPolicy,
    LocalSpeechWindowRequest,
)
from autocut_kernel.media.local_speech_window_codec import (
    DecodedLocalSpeechWindow,
    decode_local_speech_window_response,
    encode_local_speech_window_response,
)
from autocut_kernel.media.local_speech_window_projection import (
    LocalSpeechWindowProjectionError,
    local_millisecond_range,
    project_local_speech_window,
)
from autocut_kernel.media.root_evidence import (
    EvidenceCompleteness,
    SpeechSourceOutcome,
    TranscriptSourceOutcome,
)
from autocut_kernel.media.types import TickRange, TimeBase

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_HASH_C = "sha256:" + "c" * 64
_AUDIO_BASE = TimeBase(1, 48_000)


def _request(
    *,
    requested: TickRange = TickRange(-480, 960),
    time_base: TimeBase = _AUDIO_BASE,
    utterance_gap_milliseconds: int = 4,
    vad_merge_gap_milliseconds: int = 2,
) -> LocalSpeechWindowRequest:
    extraction = LocalAudioWindowSpec(
        source_id="local-speech-source",
        source_sha256=_HASH_A,
        audio_stream_index=2,
        clock_id="local-speech-source:audio",
        time_base=time_base,
        source_range=TickRange(-48_000, 96_000),
        requested_range=requested,
        sample_rate=48_000,
        channels=1,
        audio_boundary_set_sha256=_HASH_B,
        decoder_identity_sha256=_HASH_C,
        max_source_bytes=100_000,
        max_decode_frames=100,
        max_frame_bytes=100_000,
        max_pcm_bytes=100_000,
    )
    return LocalSpeechWindowRequest(
        extraction,
        LocalSpeechWindowPolicy(
            _HASH_A,
            "asr-local-v1",
            _HASH_B,
            "vad-local-v1",
            _HASH_C,
            utterance_gap_milliseconds,
            vad_merge_gap_milliseconds,
        ),
        _HASH_B,
        100_000,
    )


def _report(request: LocalSpeechWindowRequest) -> DecodedLocalPcmReport:
    extraction = request.extraction
    pcm_bytes = extraction.expected_samples * extraction.channels * 4
    return DecodedLocalPcmReport(
        extraction.source_sha256,
        extraction.canonical_hash,
        extraction.decoder_identity_sha256,
        _HASH_A,
        _HASH_B,
        pcm_bytes + 128,
        extraction.sample_rate,
        extraction.channels,
        extraction.expected_samples,
        2,
    )


def _decoded(
    asr: object,
    vad: object,
    *,
    request: LocalSpeechWindowRequest | None = None,
) -> DecodedLocalSpeechWindow:
    exact_request = request or _request()
    raw = encode_local_speech_window_response(exact_request, _report(exact_request), asr, vad)
    return decode_local_speech_window_response(raw, exact_request)


def test_projects_exact_local_clock_ranges_groups_words_and_merges_vad() -> None:
    decoded = _decoded(
        [{
            "text": "alpha beta",
            "words": ["alpha", "beta"],
            "timestamp": [[0, 3], [8, 10]],
            "key": "native-asr-key",
        }],
        [{"value": [[0, 3], [4, 6]], "key": ""}],
    )

    evidence = project_local_speech_window(decoded)

    transcript = evidence.transcript
    speech = evidence.speech_activity
    assert evidence.decoded == decoded
    assert transcript.context.origin_tick == -480
    assert transcript.context.duration_tick == 1_440
    assert transcript.coverage.in_tick == -480
    assert transcript.coverage.out_tick == 960
    assert transcript.words[0].in_tick == -480
    assert transcript.words[0].out_tick == -336
    assert transcript.words[1].in_tick == -96
    assert transcript.words[1].out_tick == 0
    assert len(transcript.segments) == 2
    assert transcript.sentences == ()
    assert transcript.completeness.sentence is EvidenceCompleteness.NOT_APPLICABLE
    assert transcript.boundary_touch_left
    assert not transcript.boundary_touch_right
    assert speech.source_outcome is SpeechSourceOutcome.SPEECH_DETECTED
    assert tuple((item.in_tick, item.out_tick) for item in speech.segments) == ((-480, -192),)


def test_millisecond_conversion_uses_floor_start_ceil_end_on_negative_original_ticks() -> None:
    actual = local_millisecond_range(1, 2, TimeBase(1, 44_100), TickRange(-2_205, 2_205))

    assert actual == TickRange(-2_161, -2_116)


def test_projects_vad_only_and_explicit_double_empty_silence() -> None:
    vad_only = project_local_speech_window(
        _decoded([{"text": "", "timestamp": []}], [{"value": [[2, 5]]}])
    )
    assert vad_only.transcript.source_outcome is TranscriptSourceOutcome.NO_LEXICAL_CONTENT
    assert vad_only.speech_activity.source_outcome is SpeechSourceOutcome.SPEECH_DETECTED

    silence = project_local_speech_window(
        _decoded([{"text": "", "words": [], "timestamp": []}], [{"value": []}])
    )
    assert silence.transcript.source_outcome is TranscriptSourceOutcome.NO_SPEECH
    assert silence.speech_activity.source_outcome is SpeechSourceOutcome.NONE_DETECTED


def test_projects_real_local_boundary_touch_and_unions_ordered_overlapping_vad() -> None:
    evidence = project_local_speech_window(
        _decoded(
            [{"text": "edge", "words": ["edge"], "timestamp": [[0, 30]]}],
            [{"value": [[0, 20], [10, 30]]}],
        )
    )

    assert evidence.transcript.boundary_touch_left
    assert evidence.transcript.boundary_touch_right
    assert tuple((item.in_tick, item.out_tick) for item in evidence.speech_activity.segments) == (
        (-480, 960),
    )


@pytest.mark.parametrize(
    ("asr", "vad", "match"),
    [
        ([{"text": "word", "timestamp": [[0, 1]]}], [{"value": [[0, 1]]}], "requires one word"),
        ([{"text": "word", "words": ["word"], "timestamp": [[0, 1]]}], [{"value": []}], "require observed VAD"),
        ([{"text": "", "words": ["word"], "timestamp": []}], [{"value": [[0, 1]]}], "empty ASR"),
        ([{"text": "word", "words": ["word"], "timestamp": [[0.0, 1]]}], [{"value": [[0, 1]]}], "exact integer"),
        ([{"text": "word", "words": ["word"], "timestamp": [[True, 1]]}], [{"value": [[0, 1]]}], "exact integer"),
        ([{"text": "word", "words": ["a", "b"], "timestamp": [[3, 5], [4, 6]]}], [{"value": [[0, 6]]}], "overlap"),
        ([{"text": "word", "words": ["word"], "timestamp": [[0, 31]]}], [{"value": [[0, 1]]}], "escapes"),
        ([{"text": "word", "words": ["word"], "timestamp": [[0, 1]], "sentences": []}], [{"value": [[0, 1]]}], "unknown or missing"),
        ([{"text": "word", "words": ["word"], "timestamp": [[0, 1]]}], [{"value": [[0, 2.0]]}], "exact integer"),
        ([{"text": "word", "words": ["word"], "timestamp": [[0, 1]]}], [{"value": [[4, 6], [3, 5]]}], "move backwards"),
    ],
)
def test_rejects_malformed_or_unproved_native_local_measurement(
    asr: object,
    vad: object,
    match: str,
) -> None:
    with pytest.raises(LocalSpeechWindowProjectionError, match=match):
        project_local_speech_window(_decoded(asr, vad))


def test_projection_replays_raw_response_not_mutable_decoded_outputs() -> None:
    asr = [{"text": "word", "words": ["word"], "timestamp": [[0, 2]]}]
    decoded = _decoded(asr, [{"value": [[0, 2]]}])
    decoded_asr = cast(list[object], decoded.asr_native_output)
    decoded_item = cast(dict[str, object], decoded_asr[0])
    decoded_words = cast(list[object], decoded_item["words"])
    decoded_words[0] = "mutated-after-decode"

    evidence = project_local_speech_window(decoded)

    assert evidence.transcript.words[0].text == "word"
    with pytest.raises(LocalSpeechWindowProjectionError, match="cannot be replayed"):
        project_local_speech_window(replace(decoded, raw_response=b"not-json"))
    with pytest.raises(LocalSpeechWindowProjectionError, match="hash differs"):
        project_local_speech_window(replace(decoded, response_sha256=_HASH_C))


@pytest.mark.parametrize(
    ("start", "end"),
    [(-1, 1), (2, 2), (3, 2)],
)
def test_millisecond_conversion_rejects_invalid_or_out_of_window_ranges(start: int, end: int) -> None:
    with pytest.raises(LocalSpeechWindowProjectionError):
        local_millisecond_range(start, end, _AUDIO_BASE, TickRange(0, 480))
