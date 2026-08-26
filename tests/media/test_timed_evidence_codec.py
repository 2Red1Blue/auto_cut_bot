from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from autocut_kernel.media.root_evidence import MediaKind
from autocut_kernel.media.timed_evidence import (
    CandidateEvidenceWindowPlan,
    CandidateTimedEvidenceSet,
    CandidateWindowOutcome,
)
from autocut_kernel.media.timed_evidence_codec import (
    decode_adaptive_evidence_window_policy,
    decode_calibration_binding,
    decode_candidate_evidence_window,
    decode_candidate_evidence_window_plan,
    decode_candidate_timed_evidence_set,
)
from autocut_kernel.media.types import MediaValidationError

from tests.media.test_root_evidence import (
    _audio_boundaries,
    _context,
    _speech,
    _subtitles,
    _transcript,
    _video_boundary_sets,
    _visual,
)
from tests.media.test_timed_evidence import _assessment, _bindings, _initial_plan


def _closed_plan_and_evidence() -> tuple[object, CandidateTimedEvidenceSet, object]:
    plan, manifest, policy = _initial_plan()
    from autocut_kernel.media.timed_evidence import advance_candidate_evidence_window

    closed = advance_candidate_evidence_window(
        plan, _assessment(plan.final_window), manifest.frame_pts_index_set, policy,
    )
    window = closed.final_window
    assessment = closed.final_assessment
    assert assessment is not None
    frame = manifest.frame_pts_index_set
    shots, scenes = _video_boundary_sets(frame)
    audio_context = _context(MediaKind.AUDIO, "asr-v1")
    transcript = _transcript(audio_context)
    speech = _speech(replace(audio_context, producer_id="vad-v1"))
    audio = _audio_boundaries(replace(audio_context, producer_id="audio-boundary-v1"))
    video_context = _context(MediaKind.VIDEO, "visual-detector-v1")
    visual = _visual(video_context)
    subtitles = _subtitles(replace(video_context, producer_id="subtitle-detector-v1"))
    values = (transcript, speech, audio, frame, shots, scenes, visual, subtitles)
    return closed, CandidateTimedEvidenceSet(
        window, assessment, transcript, speech, audio, frame, shots, scenes, visual, subtitles,
        _bindings(values),
    ), policy


def _copy_mapping(value: object) -> dict[str, Any]:
    return json.loads(json.dumps(value))


def test_decodes_existing_candidate_plan_policy_binding_and_timed_set_wire() -> None:
    plan, evidence, policy = _closed_plan_and_evidence()

    decoded_policy = decode_adaptive_evidence_window_policy(policy.to_mapping())
    decoded_plan = decode_candidate_evidence_window_plan(plan.to_mapping())
    decoded_evidence = decode_candidate_timed_evidence_set(evidence.to_mapping())
    decoded_binding = decode_calibration_binding(evidence.calibration_bindings[0].to_mapping())

    assert decoded_policy.to_mapping() == policy.to_mapping()
    assert decoded_policy.canonical_hash == policy.canonical_hash
    assert decoded_plan.to_mapping() == plan.to_mapping()
    assert decoded_plan.canonical_hash == plan.canonical_hash
    assert decoded_plan.outcome is CandidateWindowOutcome.COMPLETE
    assert decoded_evidence.to_mapping() == evidence.to_mapping()
    assert decoded_evidence.canonical_hash == evidence.canonical_hash
    assert decoded_binding.to_mapping() == evidence.calibration_bindings[0].to_mapping()


@pytest.mark.parametrize(
    ("decoder", "mapping"),
    (
        (decode_adaptive_evidence_window_policy, "policy"),
        (decode_candidate_evidence_window_plan, "plan"),
        (decode_candidate_timed_evidence_set, "evidence"),
    ),
)
def test_decoders_reject_unknown_missing_and_non_object_wire(decoder, mapping: str) -> None:
    plan, evidence, policy = _closed_plan_and_evidence()
    raw: object = {"policy": policy, "plan": plan, "evidence": evidence}[mapping].to_mapping()
    assert type(raw) is dict
    unknown = _copy_mapping(raw)
    unknown["unregistered"] = True
    missing = _copy_mapping(raw)
    missing.pop(next(iter(missing)))
    for invalid in (unknown, missing, [], None):
        with pytest.raises(MediaValidationError):
            decoder(invalid)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("windows", 0, "expansion_ordinal"), True),
        (("windows", 0, "expansion_ordinal"), 0.5),
        (("windows", 0, "source_time_base", "numerator"), 0.5),
        (("assessments", 0, "sentence_completeness"), "not-a-sentence-status"),
        (("outcome",), "not-an-outcome"),
    ),
)
def test_plan_decoder_rejects_leaf_type_enum_and_closure_mutations(path, value) -> None:
    plan, _, _ = _closed_plan_and_evidence()
    mapping: Any = _copy_mapping(plan.to_mapping())
    target: Any = mapping
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(MediaValidationError):
        decode_candidate_evidence_window_plan(mapping)

    hash_mutation = _copy_mapping(plan.to_mapping())
    hash_mutation["assessments"][0]["candidate_window_sha256"] = "sha256:" + "f" * 64
    with pytest.raises(MediaValidationError, match="assessment"):
        decode_candidate_evidence_window_plan(hash_mutation)


@pytest.mark.parametrize("assessment_count", (0, 2))
def test_plan_constructor_and_decoder_reject_assessment_cardinality_before_indexing(
    assessment_count: int,
) -> None:
    plan, _, _ = _closed_plan_and_evidence()
    assessments = () if assessment_count == 0 else (*plan.assessments, plan.assessments[0])
    with pytest.raises(MediaValidationError, match="terminal plan must assess"):
        CandidateEvidenceWindowPlan(
            plan.policy_sha256, plan.max_expansion_count, plan.vlm_candidate_sha256,
            plan.window_manifest_sha256, plan.windows, assessments, plan.outcome,
        )

    mapping: Any = _copy_mapping(plan.to_mapping())
    mapping["assessments"] = [] if assessment_count == 0 else [
        *mapping["assessments"], _copy_mapping(mapping["assessments"][0]),
    ]
    with pytest.raises(MediaValidationError, match="terminal plan must assess"):
        decode_candidate_evidence_window_plan(mapping)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("candidate_window", "source_clock_id"), "foreign:video"),
        (("candidate_window", "source_sha256"), "sha256:" + "f" * 64),
        (("frame_pts_index", "coverage", "clock_id"), "foreign:video"),
        (("calibration_bindings", 0, "active"), False),
        (("calibration_bindings", 0, "timing_error_bound_tick"), 0),
        (("calibration_bindings", 0, "adapter_sha256"), 0.5),
    ),
)
def test_timed_evidence_decoder_rejects_source_clock_coverage_and_calibration_mutations(path, value) -> None:
    _, evidence, _ = _closed_plan_and_evidence()
    mapping: Any = _copy_mapping(evidence.to_mapping())
    target: Any = mapping
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(MediaValidationError):
        decode_candidate_timed_evidence_set(mapping)


def test_candidate_window_decoder_preserves_existing_wire_and_rejects_noncanonical_ranges() -> None:
    plan, _, _ = _closed_plan_and_evidence()
    mapping = plan.final_window.to_mapping()
    decoded = decode_candidate_evidence_window(mapping)
    assert decoded.to_mapping() == mapping
    assert decoded.canonical_hash == plan.final_window.canonical_hash

    malformed = _copy_mapping(mapping)
    malformed["current_range"] = {"start_pts": 70, "end_pts": 70}
    with pytest.raises(MediaValidationError):
        decode_candidate_evidence_window(malformed)


def test_calibration_decoder_accepts_only_explicit_null_adapter_and_closed_wire() -> None:
    _, evidence, _ = _closed_plan_and_evidence()
    binding = evidence.calibration_bindings[0]
    null_adapter = _copy_mapping(binding.to_mapping())
    null_adapter["adapter_sha256"] = None
    decoded = decode_calibration_binding(null_adapter)
    assert decoded.adapter_sha256 is None

    bad = _copy_mapping(binding.to_mapping())
    bad["adapter_sha256"] = True
    with pytest.raises(MediaValidationError):
        decode_calibration_binding(bad)
    missing = _copy_mapping(binding.to_mapping())
    missing.pop("adapter_sha256")
    with pytest.raises(MediaValidationError):
        decode_calibration_binding(missing)
