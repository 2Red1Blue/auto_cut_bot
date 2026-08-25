from __future__ import annotations

from dataclasses import fields, replace
from decimal import Decimal

import pytest
from autocut_kernel.media import (
    AdaptiveEvidenceWindowPolicy,
    CalibrationBinding,
    CandidateEvidenceWindow,
    CandidateTimedEvidenceSet,
    CandidateWindowAssessment,
    CandidateWindowOutcome,
    MediaKind,
    MediaValidationError,
    SentenceCompleteness,
    TimeBase,
    advance_candidate_evidence_window,
    plan_candidate_evidence_window,
)
from autocut_kernel.media.types import TickRange
from autocut_kernel.vlm import (
    MappedSourceInterval,
    ProxyTimelineMap,
    VlmCandidateHypothesis,
    VlmCandidateKind,
    VlmCandidateTag,
    VlmContinuity,
    VlmEditingMode,
    VlmEntity,
    VlmEntityKind,
    VlmEvent,
    VlmEventKind,
    VlmFact,
    VlmFactKind,
    VlmMeasurementKind,
    VlmNarrativeFunction,
    VlmProxyInterval,
    VlmSemanticMeasurement,
    VlmSemanticPack,
    VlmSemanticSupport,
    VlmTemporalMode,
    VlmWindowSummary,
    WindowFrameSample,
    WindowManifest,
    WindowProxyBlobRef,
    derive_vlm_global_id,
)
from test_root_evidence import (
    HASH_A,
    HASH_B,
    HASH_C,
    SOURCE_HASH,
    SOURCE_ID,
    _audio_boundaries,
    _context,
    _frame_pts,
    _speech,
    _subtitles,
    _transcript,
    _video_boundary_sets,
    _visual,
)

VIDEO_BASE = TimeBase(1, 90_000)


def _manifest_and_candidate(
    coarse_range: TickRange = TickRange(40, 60),
    mapping_error: int = 5,
    *,
    manifest_source_range: TickRange = TickRange(0, 100),
) -> tuple[WindowManifest, VlmSemanticPack, VlmCandidateHypothesis]:
    frame_set = _frame_pts(replace(_context(MediaKind.VIDEO, "frame-decoder-v1")))
    timeline = ProxyTimelineMap.translation(
        time_base=VIDEO_BASE,
        proxy_range=TickRange(0, manifest_source_range.end_pts - manifest_source_range.start_pts),
        source_start_pts=manifest_source_range.start_pts,
    )
    samples = tuple(
        WindowFrameSample(tick, tick - manifest_source_range.start_pts, HASH_C)
        for tick in frame_set.pts_index.ticks
        if manifest_source_range.start_pts <= tick < manifest_source_range.end_pts
    )
    manifest = WindowManifest(
        source_id=SOURCE_ID,
        source_clock_id=frame_set.context.clock_id,
        source_sha256=SOURCE_HASH,
        stream_index=0,
        source_time_base=VIDEO_BASE,
        source_range=manifest_source_range,
        core_range=manifest_source_range,
        frame_pts_index_set=frame_set,
        proxy_blob_ref=WindowProxyBlobRef("proxy-1", HASH_A, 10, "video/mp4"),
        preprocess_policy_sha256=HASH_A,
        window_sampling_policy_sha256=HASH_B,
        timeline_map=timeline,
        frame_samples=samples,
    )
    request_identity_sha256 = HASH_A
    entity_id = derive_vlm_global_id("entity", "entity_1", request_identity_sha256)
    fact_id = derive_vlm_global_id("fact", "fact_1", request_identity_sha256)
    event_id = derive_vlm_global_id("event", "event_1", request_identity_sha256)
    support = VlmSemanticSupport(
        proxy_interval=VlmProxyInterval(TickRange(0, 1), 0),
        supporting_frame_ids=(samples[0].frame_id,),
        confidence=Decimal("0.9"),
        source_interval=MappedSourceInterval(
            coarse_range=coarse_range,
            mapping_error_bound_source_pts=mapping_error,
            source_time_base=VIDEO_BASE,
            provider_uncertainty_proxy_pts=0,
            proxy_time_base=VIDEO_BASE,
        ),
        core_owner_window_manifest_sha256=manifest.canonical_hash,
    )
    entity = VlmEntity(
        entity_id,
        "entity_1",
        VlmEntityKind.PERSON,
        "person",
        "visible person",
        support,
    )
    fact = VlmFact(
        fact_id,
        "fact_1",
        VlmFactKind.VISIBLE_ACTION,
        entity_id,
        None,
        "person acts",
        support,
    )
    event = VlmEvent(
        event_id,
        "event_1",
        VlmEventKind.REVEAL,
        "visible reveal",
        (entity_id,),
        (fact_id,),
        (),
        (),
        None,
        VlmTemporalMode.PRESENT,
        support,
    )
    candidate = VlmCandidateHypothesis(
        derive_vlm_global_id("candidate", "candidate_1", request_identity_sha256),
        "candidate_1",
        VlmCandidateKind.HIGHLIGHT,
        event_id,
        (event_id,),
        (),
        (event_id,),
        None,
        "the reveal is a candidate",
        "a visible reveal",
        "the reveal completes",
        None,
        (VlmEditingMode.DIALOGUE, VlmEditingMode.ACTION),
        (
            VlmNarrativeFunction.HOOK,
            VlmNarrativeFunction.REVEAL,
            VlmNarrativeFunction.PAYOFF,
        ),
        (VlmCandidateTag.DIALOGUE, VlmCandidateTag.EMOTION, VlmCandidateTag.REVEAL),
        (
            VlmSemanticMeasurement(
                VlmMeasurementKind.REVEAL_STRENGTH,
                Decimal("0.9"),
                Decimal("0.9"),
                (fact_id,),
                (event_id,),
            ),
        ),
        support,
    )
    pack = VlmSemanticPack(
        request_identity_sha256,
        manifest.canonical_hash,
        HASH_B,
        VlmWindowSummary(
            "visible reveal",
            VlmTemporalMode.PRESENT,
            (fact_id,),
            (event_id,),
            Decimal("0.9"),
        ),
        VlmContinuity(False, False, False, False, (), (), ()),
        (entity,),
        (fact,),
        (event,),
        (candidate,),
    )
    return manifest, pack, candidate


def _policy(
    *,
    left: int = 0,
    right: int = 0,
    step: int = 10,
    maximum: int = 2,
) -> AdaptiveEvidenceWindowPolicy:
    return AdaptiveEvidenceWindowPolicy(
        strategy_version="media-window-v2",
        time_base=VIDEO_BASE,
        initial_left_expansion_pts=left,
        initial_right_expansion_pts=right,
        expansion_step_pts=step,
        max_expansion_count=maximum,
        boundary_touch_margin_pts=2,
    )


def _assessment(
    window: CandidateEvidenceWindow,
    *,
    transcript_left: bool = False,
    transcript_right: bool = False,
    speech_left: bool = False,
    speech_right: bool = False,
    left_truncated: bool = False,
    right_truncated: bool = False,
    sentence: SentenceCompleteness = SentenceCompleteness.COMPLETE,
) -> CandidateWindowAssessment:
    return CandidateWindowAssessment(
        candidate_window_sha256=window.canonical_hash,
        transcript_left_boundary_touch=transcript_left,
        transcript_right_boundary_touch=transcript_right,
        speech_left_boundary_touch=speech_left,
        speech_right_boundary_touch=speech_right,
        left_truncated=left_truncated,
        right_truncated=right_truncated,
        sentence_completeness=sentence,
    )


def _initial_plan(**policy_overrides: int):
    manifest, pack, candidate = _manifest_and_candidate()
    policy = _policy(**policy_overrides)
    return (
        plan_candidate_evidence_window(
            candidate, pack, manifest, manifest.frame_pts_index_set, policy
        ),
        manifest,
        policy,
    )


def test_initial_plan_waits_for_real_producer_feedback() -> None:
    plan, _, _ = _initial_plan(left=10, right=10)

    assert plan.outcome is CandidateWindowOutcome.AWAITING_EVIDENCE
    assert plan.final_window.current_range == TickRange(25, 75)
    assert plan.final_window.source_range == TickRange(0, 100)
    assert plan.assessments == ()


def test_boundary_feedback_expands_only_the_observed_side() -> None:
    plan, manifest, policy = _initial_plan()

    expanded = advance_candidate_evidence_window(
        plan,
        _assessment(
            plan.final_window,
            transcript_right=True,
            sentence=SentenceCompleteness.PARTIAL,
        ),
        manifest.frame_pts_index_set,
        policy,
    )

    assert expanded.outcome is CandidateWindowOutcome.AWAITING_EVIDENCE
    assert len(expanded.windows) == 2
    assert (
        expanded.final_window.current_range.start_pts == plan.final_window.current_range.start_pts
    )
    assert expanded.final_window.current_range.end_pts > plan.final_window.current_range.end_pts


def test_expansion_uses_full_episode_extent_not_proxy_window_extent() -> None:
    manifest, pack, candidate = _manifest_and_candidate(
        TickRange(70, 78),
        mapping_error=0,
        manifest_source_range=TickRange(20, 80),
    )
    policy = _policy(step=15, maximum=2)
    plan = plan_candidate_evidence_window(
        candidate, pack, manifest, manifest.frame_pts_index_set, policy
    )

    expanded = advance_candidate_evidence_window(
        plan,
        _assessment(
            plan.final_window,
            speech_right=True,
            sentence=SentenceCompleteness.PARTIAL,
        ),
        manifest.frame_pts_index_set,
        policy,
    )

    assert expanded.final_window.current_range.end_pts > manifest.source_range.end_pts
    assert expanded.final_window.source_range == TickRange(0, 100)


def test_complete_feedback_closes_without_another_window() -> None:
    plan, manifest, policy = _initial_plan()

    complete = advance_candidate_evidence_window(
        plan,
        _assessment(plan.final_window),
        manifest.frame_pts_index_set,
        policy,
    )

    assert complete.outcome is CandidateWindowOutcome.COMPLETE
    assert len(complete.windows) == 1
    assert len(complete.assessments) == 1


def test_unknown_sentence_without_edge_touch_closes_physical_evidence() -> None:
    plan, manifest, policy = _initial_plan()

    complete = advance_candidate_evidence_window(
        plan,
        _assessment(plan.final_window, sentence=SentenceCompleteness.UNKNOWN),
        manifest.frame_pts_index_set,
        policy,
    )

    assert complete.outcome is CandidateWindowOutcome.COMPLETE
    assert len(complete.windows) == 1
    assert complete.final_assessment is not None
    assert complete.final_assessment.sentence_completeness is SentenceCompleteness.UNKNOWN


def test_open_boundary_at_policy_limit_is_exhausted() -> None:
    plan, manifest, policy = _initial_plan(maximum=1)
    expanded = advance_candidate_evidence_window(
        plan,
        _assessment(plan.final_window, transcript_left=True),
        manifest.frame_pts_index_set,
        policy,
    )

    exhausted = advance_candidate_evidence_window(
        expanded,
        _assessment(expanded.final_window, transcript_left=True),
        manifest.frame_pts_index_set,
        policy,
    )

    assert exhausted.outcome is CandidateWindowOutcome.EXHAUSTED
    assert exhausted.final_window.expansion_ordinal == 1


def test_policy_calibration_and_tick_validation_are_fail_closed() -> None:
    policy = _policy()
    binding = CalibrationBinding(
        HASH_A,
        HASH_B,
        HASH_C,
        "detector",
        "1.0.0",
        VIDEO_BASE,
        1,
        True,
    )
    assert binding.canonical_hash == replace(binding).canonical_hash

    with pytest.raises(MediaValidationError):
        replace(policy, initial_left_expansion_pts=1.5)
    with pytest.raises(MediaValidationError):
        replace(policy, max_expansion_count=True)
    with pytest.raises(MediaValidationError):
        replace(binding, timing_error_bound_tick=0)
    with pytest.raises(MediaValidationError):
        replace(binding, active=False)


def _bindings(evidence_values: tuple[object, ...]) -> tuple[CalibrationBinding, ...]:
    by_key: dict[tuple[str, str], object] = {}
    for value in evidence_values:
        context = value.context
        by_key[(context.producer_id, context.generation_policy_sha256)] = value
    return tuple(
        CalibrationBinding(
            policy_sha256=policy_sha,
            detector_sha256=HASH_B,
            calibration_record_sha256=HASH_C,
            producer_id=producer_id,
            producer_version="1.0.0",
            time_base=value.context.time_base,
            timing_error_bound_tick=1,
            active=True,
        )
        for (producer_id, policy_sha), value in sorted(by_key.items())
    )


def test_candidate_timed_evidence_requires_conjunctive_audio_video_and_calibration() -> None:
    plan, manifest, policy = _initial_plan()
    closed_plan = advance_candidate_evidence_window(
        plan,
        _assessment(plan.final_window),
        manifest.frame_pts_index_set,
        policy,
    )
    window = closed_plan.final_window
    assessment = closed_plan.final_assessment
    assert assessment is not None
    frame_set = manifest.frame_pts_index_set
    shots, scenes = _video_boundary_sets(frame_set)
    audio_context = _context(MediaKind.AUDIO, "asr-v1")
    transcript = _transcript(audio_context)
    speech = _speech(replace(audio_context, producer_id="vad-v1"))
    audio_boundaries = _audio_boundaries(replace(audio_context, producer_id="audio-boundary-v1"))
    video_context = _context(MediaKind.VIDEO, "visual-detector-v1")
    visual = _visual(video_context)
    subtitles = _subtitles(replace(video_context, producer_id="subtitle-detector-v1"))
    evidence_values = (
        transcript,
        speech,
        audio_boundaries,
        frame_set,
        shots,
        scenes,
        visual,
        subtitles,
    )
    evidence = CandidateTimedEvidenceSet(
        candidate_window=window,
        window_assessment=assessment,
        transcript=transcript,
        speech_activity=speech,
        audio_sample_boundaries=audio_boundaries,
        frame_pts_index=frame_set,
        shot_boundaries=shots,
        scene_boundaries=scenes,
        visual_validity=visual,
        subtitle_cues=subtitles,
        calibration_bindings=_bindings(evidence_values),
    )

    assert evidence.canonical_hash.startswith("sha256:")
    assert evidence.audio_sample_boundaries.points
    assert evidence.visual_validity.intervals[-1].classification.value == "unknown"

    with pytest.raises(TypeError):
        CandidateTimedEvidenceSet(  # type: ignore[call-arg]
            candidate_window=window,
            window_assessment=assessment,
            transcript=transcript,
            speech_activity=speech,
            frame_pts_index=frame_set,
            shot_boundaries=shots,
            scene_boundaries=scenes,
            visual_validity=visual,
            subtitle_cues=subtitles,
            calibration_bindings=_bindings(evidence_values),
        )
    with pytest.raises(MediaValidationError, match="calibration"):
        CandidateTimedEvidenceSet(
            candidate_window=window,
            window_assessment=assessment,
            transcript=transcript,
            speech_activity=speech,
            audio_sample_boundaries=audio_boundaries,
            frame_pts_index=frame_set,
            shot_boundaries=shots,
            scene_boundaries=scenes,
            visual_validity=visual,
            subtitle_cues=subtitles,
            calibration_bindings=_bindings(evidence_values)[:-1],
        )


def test_candidate_window_has_no_admission_fields() -> None:
    plan, _, _ = _initial_plan()
    field_names = {field.name for field in fields(CandidateEvidenceWindow)}
    plan_field_names = {field.name for field in fields(type(plan))}
    assert not field_names.intersection({"pass", "ready", "allow"})
    assert "vlm_candidate_sha256" in field_names
    assert "vlm_candidate_sha256" in plan_field_names
    assert "vlm_observation_sha256" not in field_names | plan_field_names
    assert not hasattr(plan.final_window, "pass")
