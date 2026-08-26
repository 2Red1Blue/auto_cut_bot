"""Mapped local-window assessment over raw-bound synthetic speech evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest
from autocut_kernel.media.local_speech_window import (
    DecodedLocalPcmReport,
    LocalSpeechWindowPolicy,
    LocalSpeechWindowRequest,
)
from autocut_kernel.media.local_speech_window_codec import (
    decode_local_speech_window_response,
    encode_local_speech_window_response,
)
from autocut_kernel.media.local_speech_window_projection import project_local_speech_window
from autocut_kernel.media.root_evidence import EvidenceCompleteness, VisualClassification
from autocut_kernel.media.timed_evidence import (
    AdaptiveEvidenceWindowPolicy,
    CalibrationBinding,
    CandidateEvidenceWindowPlan,
    CandidateWindowOutcome,
    SentenceCompleteness,
    advance_candidate_evidence_window,
)
from autocut_kernel.media.types import TickRange

from tests.media.test_mapped_local_audio_window import HASH, OTHER, _case, _derive


def _long_case(*, start: int = 15_000, end: int = 60_000, **overrides):
    options = {
        "video_end": 180_000,
        "audio_end": 96_000,
        "frame_ticks": (0, 15_000, 60_000, 120_000, 180_000),
        "audio_ticks": (0, 8_000, 32_000, 64_000, 96_000),
        "visual": ((0, 180_000, VisualClassification.VALID_CONTENT),),
    }
    options.update(overrides)
    return _case(start, end, **options)


def _request(
    spec,
    *,
    max_response_bytes: int = 100_000,
    utterance_gap_milliseconds: int = 0,
    vad_merge_gap_milliseconds: int = 0,
) -> LocalSpeechWindowRequest:
    return LocalSpeechWindowRequest(
        spec,
        LocalSpeechWindowPolicy(
            HASH,
            "local-asr",
            HASH,
            "local-vad",
            OTHER,
            utterance_gap_milliseconds,
            vad_merge_gap_milliseconds,
        ),
        HASH,
        max_response_bytes,
    )


def _report(request: LocalSpeechWindowRequest) -> DecodedLocalPcmReport:
    spec = request.extraction
    pcm_bytes = spec.expected_samples * spec.channels * 4
    return DecodedLocalPcmReport(
        spec.source_sha256,
        spec.canonical_hash,
        spec.decoder_identity_sha256,
        HASH,
        OTHER,
        pcm_bytes + 128,
        spec.sample_rate,
        spec.channels,
        spec.expected_samples,
        1,
    )


def _evidence(
    spec,
    *,
    asr: object | None = None,
    vad: object | None = None,
    utterance_gap_milliseconds: int = 0,
    vad_merge_gap_milliseconds: int = 0,
):
    request = _request(
        spec,
        utterance_gap_milliseconds=utterance_gap_milliseconds,
        vad_merge_gap_milliseconds=vad_merge_gap_milliseconds,
    )
    raw = encode_local_speech_window_response(
        request,
        _report(request),
        [{"text": "", "timestamp": []}] if asr is None else asr,
        [{"value": []}] if vad is None else vad,
    )
    return project_local_speech_window(decode_local_speech_window_response(raw, request))


def _calibrations(request: LocalSpeechWindowRequest, *, asr_error: int = 1, vad_error: int = 1):
    policy = request.policy
    base = request.extraction.time_base
    return (
        CalibrationBinding(
            policy.asr_generation_policy_sha256,
            HASH,
            OTHER,
            policy.asr_producer_id,
            "local-asr-v1",
            base,
            asr_error,
            True,
            HASH,
        ),
        CalibrationBinding(
            policy.vad_generation_policy_sha256,
            OTHER,
            HASH,
            policy.vad_producer_id,
            "local-vad-v1",
            base,
            vad_error,
            True,
            OTHER,
        ),
    )


def _policy(window, *, margin: int = 2) -> AdaptiveEvidenceWindowPolicy:
    return AdaptiveEvidenceWindowPolicy(
        "mapped-local-window-test-v1",
        window.source_time_base,
        0,
        0,
        15,
        1,
        margin,
    )


def _assess(window, domain, spec, evidence, *, policy=None, calibrations=None):
    from autocut_kernel.media.mapped_local_window_assessment import assess_mapped_local_window

    policy = _policy(window) if policy is None else policy
    calibrations = _calibrations(evidence.decoded.request) if calibrations is None else calibrations
    return assess_mapped_local_window(
        evidence,
        window,
        domain,
        adaptive_policy=policy,
        asr_calibration=calibrations[0],
        vad_calibration=calibrations[1],
    )


def test_nonidentity_map_replays_raw_evidence_and_advances_existing_plan() -> None:
    window, domain, facts = _long_case()
    spec = _derive(
        window,
        domain,
        facts,
        max_outward_padding_audio_ticks=0,
        max_pcm_bytes=2_000_000,
    )
    evidence = _evidence(spec)
    policy = _policy(window)

    assessment = _assess(window, domain, spec, evidence, policy=policy)

    assert assessment.candidate_window_sha256 == window.canonical_hash
    assert assessment.sentence_completeness is SentenceCompleteness.NOT_APPLICABLE
    assert not assessment.needs_expansion
    plan = CandidateEvidenceWindowPlan(
        policy.canonical_hash,
        policy.max_expansion_count,
        window.vlm_candidate_sha256,
        window.window_manifest_sha256,
        (window,),
        (),
        CandidateWindowOutcome.AWAITING_EVIDENCE,
    )
    assert advance_candidate_evidence_window(
        plan,
        assessment,
        domain.root.frame_pts_index,
        policy,
    ).outcome is CandidateWindowOutcome.COMPLETE


@pytest.mark.parametrize(
    ("asr", "vad", "side"),
    [
        ([{"text": "left", "words": ["left"], "timestamp": [[0, 1]]}], [{"value": [[0, 1]]}], "left"),
        ([{"text": "right", "words": ["right"], "timestamp": [[480, 500]]}], [{"value": [[480, 500]]}], "right"),
    ],
)
def test_any_record_intersecting_calibrated_mapped_guard_requires_expansion(asr, vad, side) -> None:
    window, domain, facts = _long_case()
    spec = _derive(window, domain, facts, max_outward_padding_audio_ticks=1_000)
    evidence = _evidence(spec, asr=asr, vad=vad)

    assessment = _assess(window, domain, spec, evidence)

    assert getattr(assessment, f"transcript_{side}_boundary_touch")
    assert getattr(assessment, f"speech_{side}_boundary_touch")
    assert assessment.needs_expansion


def test_true_both_stream_source_edge_suppresses_boundary_touch() -> None:
    window, domain, facts = _long_case(start=0, end=180_000)
    spec = _derive(
        window,
        domain,
        facts,
        max_outward_padding_audio_ticks=0,
        max_pcm_bytes=2_000_000,
    )
    evidence = _evidence(
        spec,
        asr=[{"text": "edge", "words": ["edge"], "timestamp": [[0, 10]]}],
        vad=[{"value": [[0, 10]]}],
    )

    assessment = _assess(window, domain, spec, evidence)

    assert not assessment.transcript_left_boundary_touch
    assert not assessment.speech_left_boundary_touch
    assert not assessment.left_truncated


def test_utterance_segment_protects_word_gap_that_vad_does_not_cover() -> None:
    window, domain, facts = _long_case(
        start=61_000,
        end=120_000,
        frame_ticks=(0, 15_000, 60_000, 61_000, 120_000, 180_000),
    )
    spec = _derive(window, domain, facts, max_outward_padding_audio_ticks=1_000)
    evidence = _evidence(
        spec,
        asr=[{
            "text": "first second",
            "words": ["first", "second"],
            "timestamp": [[0, 5], [20, 25]],
        }],
        vad=[{"value": [[0, 5], [20, 25]]}],
        utterance_gap_milliseconds=20,
        vad_merge_gap_milliseconds=0,
    )

    assessment = _assess(window, domain, spec, evidence)

    assert assessment.transcript_left_boundary_touch
    assert not assessment.speech_left_boundary_touch


@pytest.mark.parametrize(
    ("asr_error", "vad_error", "expected"),
    [(50, 1, "transcript_left_boundary_touch"), (1, 50, "speech_left_boundary_touch")],
)
def test_each_role_uses_its_own_calibrated_timestamp_error_bound(
    asr_error: int,
    vad_error: int,
    expected: str,
) -> None:
    window, domain, facts = _long_case()
    spec = _derive(window, domain, facts, max_outward_padding_audio_ticks=1_000)
    evidence = _evidence(
        spec,
        asr=[{"text": "near", "words": ["near"], "timestamp": [[1, 2]]}],
        vad=[{"value": [[1, 2]]}],
    )

    assessment = _assess(
        window,
        domain,
        spec,
        evidence,
        calibrations=_calibrations(evidence.decoded.request, asr_error=asr_error, vad_error=vad_error),
    )

    assert getattr(assessment, expected)
    other = "speech_left_boundary_touch" if expected.startswith("transcript") else "transcript_left_boundary_touch"
    assert not getattr(assessment, other)


def test_video_source_end_does_not_suppress_touch_when_audio_has_a_real_tail() -> None:
    window, domain, facts = _long_case(
        start=120_000,
        end=180_000,
        audio_end=104_000,
        audio_ticks=(0, 8_000, 32_000, 64_000, 96_000, 104_000),
    )
    spec = _derive(window, domain, facts, max_outward_padding_audio_ticks=0)
    evidence = _evidence(
        spec,
        asr=[{"text": "edge", "words": ["edge"], "timestamp": [[650, 666]]}],
        vad=[{"value": [[650, 666]]}],
    )

    assessment = _assess(
        window,
        domain,
        spec,
        evidence,
        calibrations=_calibrations(evidence.decoded.request, asr_error=50, vad_error=50),
    )

    assert assessment.transcript_right_boundary_touch
    assert assessment.speech_right_boundary_touch


def test_audio_extraction_must_outwardly_cover_video_boundary_not_only_share_map_segment() -> None:
    window, domain, facts = _long_case(start=0, end=60_000)
    spec = _derive(window, domain, facts, max_outward_padding_audio_ticks=0)
    shortened = replace(spec, requested_range=TickRange(8, spec.requested_range.end_pts))
    evidence = _evidence(shortened)

    with pytest.raises(ValueError, match="outwardly cover"):
        _assess(window, domain, shortened, evidence)


def test_continuous_map_rejects_a_candidate_crossing_an_internal_audio_gap() -> None:
    window, domain, _facts = _long_case(
        start=15_000,
        end=120_000,
        gap=(32_000, 64_000),
    )
    intact_window, intact_domain, intact_facts = _long_case(start=15_000, end=120_000)
    intact = _derive(
        intact_window,
        intact_domain,
        intact_facts,
        max_outward_padding_audio_ticks=0,
    )
    spec = replace(
        intact,
        audio_boundary_set_sha256=domain.root.audio_sample_boundaries.canonical_hash,
    )
    evidence = _evidence(spec)

    with pytest.raises(ValueError, match="unmapped or uncovered"):
        _assess(window, domain, spec, evidence)


def test_assessor_rejects_direct_typed_projection_mutation_after_raw_decode() -> None:
    window, domain, facts = _long_case()
    spec = _derive(window, domain, facts, max_outward_padding_audio_ticks=0)
    evidence = _evidence(spec)
    changed = replace(
        evidence,
        transcript=replace(
            evidence.transcript,
            completeness=replace(
                evidence.transcript.completeness,
                sentence=EvidenceCompleteness.COMPLETE,
            ),
        ),
    )

    with pytest.raises(ValueError, match="raw projection"):
        _assess(window, domain, spec, changed)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda window, domain: replace(window, source_sha256=OTHER),
        lambda window, domain: replace(
            window,
            coarse_range=TickRange(60_000, 120_000),
            current_range=TickRange(60_000, 120_000),
        ),
    ],
)
def test_foreign_or_uncovered_candidate_reject(mutation) -> None:
    window, domain, facts = _long_case()
    spec = _derive(window, domain, facts, max_outward_padding_audio_ticks=0)
    evidence = _evidence(spec)
    changed_window = mutation(window, domain)
    with pytest.raises(ValueError):
        _assess(changed_window, domain, spec, evidence)


def test_asr_and_vad_calibrations_must_close_replayed_local_contexts() -> None:
    window, domain, facts = _long_case()
    spec = _derive(window, domain, facts, max_outward_padding_audio_ticks=0)
    evidence = _evidence(spec)
    calibrations = _calibrations(evidence.decoded.request)

    with pytest.raises(ValueError, match="calibration"):
        _assess(
            window,
            domain,
            spec,
            evidence,
            calibrations=(replace(calibrations[0], producer_id="foreign"), calibrations[1]),
        )
    with pytest.raises(ValueError, match="own local producer"):
        _assess(
            window,
            domain,
            spec,
            evidence,
            calibrations=(calibrations[1], calibrations[0]),
        )


def test_selected_audio_stream_identity_must_match_replayed_map_track() -> None:
    window, domain, facts = _long_case()
    spec = _derive(window, domain, facts, max_outward_padding_audio_ticks=0)
    changed = replace(spec, audio_stream_index=spec.audio_stream_index + 1)
    evidence = _evidence(changed)

    with pytest.raises(ValueError, match="exact audio facts"):
        _assess(window, domain, changed, evidence)
