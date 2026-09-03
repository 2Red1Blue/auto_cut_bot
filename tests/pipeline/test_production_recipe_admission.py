from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from typing import Callable, cast
from uuid import UUID

import pytest
from autocut_kernel.media.types import TickRange, TimeBase, canonical_sha256
from autocut_kernel.physical_edit.candidate_dialogue_guard import CandidateDialogueGuard
from autocut_kernel.physical_edit.candidate_exact_span import CandidateExactSpanResult
from autocut_kernel.physical_edit.candidate_timed_speech_authority import (
    CandidateTimedSpeechAuthorityKind,
)
from autocut_kernel.physical_edit.dialogue_guard import DialogueGuardKind, DialogueRequirement
from autocut_kernel.physical_edit.editorial_exact_span import EditorialExactSpanQuery
from autocut_kernel.physical_edit.exact_span import (
    BoundaryProof,
    ExactAvSpanRequest,
    VideoClockRange,
)
from autocut_kernel.pipeline.production_recipe_admission import (
    PHYSICAL_EDIT_REPLAY_EVALUATOR_STRATEGY_VERSION,
    PHYSICAL_EDIT_RULE_IDS,
    PhysicalEditAdmission,
    PhysicalEditAdmissionError,
    PhysicalEditBackend,
    PhysicalEditCheck,
    PhysicalEditChoiceIdentity,
    PhysicalEditCompilationAttempt,
    PhysicalEditCompilationEntry,
    PhysicalEditCompilationReport,
    PhysicalEditRecipeSubject,
    PhysicalEditReplayEvidence,
    PhysicalEditReplayFact,
    VerifiedPhysicalEditAdmission,
    build_physical_edit_admission,
    verify_physical_edit_admission,
)
from autocut_kernel.store.models import ArtifactScope, CommittedArtifactMemberReference
from autocut_kernel.vlm.models import VlmEditingMode


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _ref(
    *,
    receipt: str,
    artifact_set: str,
    ordinal: int,
    artifact_type: str,
    logical_id: str,
    content_character: str,
) -> CommittedArtifactMemberReference:
    return CommittedArtifactMemberReference(
        UUID(receipt),
        UUID(artifact_set),
        ordinal,
        ArtifactScope("pipeline", "job", "run-1"),
        artifact_type,
        logical_id,
        1,
        _hash(content_character),
    )


def _query(
    *,
    story_id: str = "story-1",
    beat_id: str = "beat-1",
    requirement_id: str = "requirement-1",
    alternative_id: str = "alternative-1",
    candidate_id: str = "candidate-1",
) -> EditorialExactSpanQuery:
    base = TimeBase(1, 90_000)
    desired = VideoClockRange("source-1", _hash("1"), "video", base, TickRange(0, 120))
    anchor = VideoClockRange("source-1", _hash("1"), "video", base, TickRange(10, 100))
    return EditorialExactSpanQuery(
        story_id=story_id,
        beat_id=beat_id,
        evidence_requirement_id=requirement_id,
        alternative_id=alternative_id,
        candidate_id=candidate_id,
        anchor_event_id="event-1",
        anchor_event_sha256=_hash("3"),
        span_intent="tight",
        dominant_editing_mode=VlmEditingMode.ACTION,
        policy_sha256=_hash("4"),
        blueprint_beat_sha256=_hash("2"),
        evidence_requirement_sha256=_hash("5"),
        alternative_sha256=_hash("6"),
        catalog_candidate_sha256=_hash("7"),
        semantic_pack_sha256=_hash("8"),
        timed_evidence_sha256=_hash("9"),
        dialogue_protection_kind="known_speech",
        request=ExactAvSpanRequest(desired, anchor, 1, DialogueRequirement.NOT_REQUIRED),
    )


def _result(
    query: EditorialExactSpanQuery,
    *,
    authority_kind: CandidateTimedSpeechAuthorityKind = (
        CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE
    ),
) -> CandidateExactSpanResult:
    video_base = TimeBase(1, 90_000)
    audio_base = TimeBase(1, 48_000)
    proof = BoundaryProof(
        "source-1",
        _hash("1"),
        "video",
        video_base,
        10,
        100,
        "audio",
        audio_base,
        5,
        53,
        _hash("a"),
        _hash("b"),
        _hash("c"),
        _hash("d"),
        _hash("e"),
    )
    guard = CandidateDialogueGuard(
        root_evidence_sha256=_hash("f"),
        candidate_evidence_sha256=query.timed_evidence_sha256,
        candidate_window_sha256=_hash("a"),
        window_plan_sha256=_hash("b"),
        timed_speech_authority_sha256=_hash("c"),
        original_authority_kind=authority_kind,
        original_authority_sha256=_hash("d"),
        guard_policy_sha256=_hash("e"),
        source_id="source-1",
        source_sha256=_hash("1"),
        source_audio_clock_id="audio",
        source_audio_time_base=audio_base,
        source_audio_range=TickRange(0, 64),
        requirement=DialogueRequirement.NOT_REQUIRED,
        kind=DialogueGuardKind.NOT_REQUIRED,
        reason="blueprint_does_not_require_complete_dialogue",
        protected_ranges=(),
    )
    return CandidateExactSpanResult(
        TickRange(10, 100),
        TickRange(5, 53),
        proof,
        guard,
        0,
        (0, 0, 0, 0, 90, 48, 10, 100, 5, 53),
        "16",
        4,
        2,
        query.request.canonical_hash,
        _hash("f"),
        _hash("a"),
        _hash("b"),
    )


def _entry(
    authority_kind: CandidateTimedSpeechAuthorityKind = (
        CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE
    ),
) -> PhysicalEditCompilationEntry:
    query = _query()
    result = _result(query, authority_kind=authority_kind)
    attempt = PhysicalEditCompilationAttempt(
        "tight", "selected", "STAGE4_SPAN_SELECTED", query.canonical_hash, result.canonical_hash
    )
    return PhysicalEditCompilationEntry(
        0,
        "story-1",
        "beat-1",
        "requirement-1",
        "alternative-1",
        "candidate-1",
        0,
        0,
        (attempt,),
        query,
        result,
    )


def _report(
    backend: PhysicalEditBackend = "installed_cpu_profile",
) -> PhysicalEditCompilationReport:
    stage3 = (
        _ref(
            receipt="11111111-1111-4111-8111-111111111111",
            artifact_set="22222222-2222-4222-8222-222222222222",
            ordinal=0,
            artifact_type="editorial_blueprint",
            logical_id="editorial_blueprint@story-1",
            content_character="1",
        ),
        _ref(
            receipt="11111111-1111-4111-8111-111111111111",
            artifact_set="22222222-2222-4222-8222-222222222222",
            ordinal=1,
            artifact_type="evidence_closure_set",
            logical_id="evidence_closure_set@story-1",
            content_character="2",
        ),
        _ref(
            receipt="11111111-1111-4111-8111-111111111111",
            artifact_set="22222222-2222-4222-8222-222222222222",
            ordinal=2,
            artifact_type="context_manifest",
            logical_id="context_manifest@story-1",
            content_character="3",
        ),
        _ref(
            receipt="11111111-1111-4111-8111-111111111111",
            artifact_set="22222222-2222-4222-8222-222222222222",
            ordinal=3,
            artifact_type="semantic_feasibility_admission",
            logical_id="semantic_feasibility_admission",
            content_character="4",
        ),
    )
    cuda = backend == "runtime_cuda_capability"
    batch_type = "runtime_timed_media_evidence_batch" if cuda else "timed_media_evidence_batch"
    batch = _ref(
        receipt="33333333-3333-4333-8333-333333333333",
        artifact_set="44444444-4444-4444-8444-444444444444",
        ordinal=0,
        artifact_type=batch_type,
        logical_id=batch_type,
        content_character="5",
    )
    child_types = (
        "root_media_evidence_bundle",
        "candidate_timed_evidence_index",
        ("runtime_timed_speech_capability_admission" if cuda else "timed_speech_profile_admission"),
        "presentation_timeline_probe",
        "committed_video_to_audio_clock_map_certificate",
    )
    child_logical_ids = (
        (
            "root_media_evidence",
            "candidate_timed_evidence",
            "runtime_timed_speech_capability_admission",
            "presentation_timeline_probe",
            "video_to_audio_clock_map",
        )
        if cuda
        else (
            "root_media_evidence_episode_0000",
            "candidate_timed_evidence_episode_0000",
            "timed_speech_profile_admission_episode_0000",
            "presentation_timeline_probe_episode_0000",
            "video_to_audio_clock_map_episode_0000",
        )
    )
    child = tuple(
        _ref(
            receipt="55555555-5555-4555-8555-555555555555",
            artifact_set="66666666-6666-4666-8666-666666666666",
            ordinal=index,
            artifact_type=child_types[index],
            logical_id=child_logical_ids[index],
            content_character=character,
        )
        for index, character in enumerate(("6", "7", "8", "9", "a"))
    )
    authority_kind = (
        CandidateTimedSpeechAuthorityKind.RUNTIME_CUDA_CAPABILITY
        if cuda
        else CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE
    )
    return PhysicalEditCompilationReport(
        _hash("a"),
        stage3,
        batch,
        (child,),
        backend,
        _hash("d"),
        _hash("4"),
        _hash("f"),
        (_entry(authority_kind),),
    )


def _subjects() -> tuple[PhysicalEditRecipeSubject, ...]:
    return (
        PhysicalEditRecipeSubject(
            0,
            "story-1",
            "recipe",
            "recipe/story-1",
            1,
            ArtifactScope("pipeline", "job", "run-1"),
            _hash("c"),
        ),
    )


def _golden_censuses(
    report: PhysicalEditCompilationReport,
    subjects: tuple[PhysicalEditRecipeSubject, ...],
) -> tuple[tuple[int, str], ...]:
    entries = report.entries
    predecessor_census = {
        "input_binding_sha256": report.input_binding_sha256,
        "stage3_member_refs": [item.to_mapping() for item in report.stage3_member_refs],
        "media_batch_member_ref": report.media_batch_member_ref.to_mapping(),
        "timed_media_child_member_refs": [
            [item.to_mapping() for item in row] for row in report.timed_media_child_member_refs
        ],
        "backend_discriminator": report.backend_discriminator,
        "authority_sha256": report.authority_sha256,
        "editorial_exact_policy_sha256": report.editorial_exact_policy_sha256,
        "candidate_exact_policy_sha256": report.candidate_exact_policy_sha256,
    }
    choices = [PhysicalEditChoiceIdentity.from_entry(item).to_mapping() for item in entries]
    queries = [
        {
            "ordinal": item.ordinal,
            "query": item.selected_query.to_mapping(),
            "query_sha256": item.selected_query.canonical_hash,
        }
        for item in entries
    ]
    relations = [
        {
            "ordinal": item.ordinal,
            "result": item.selected_result.to_mapping(),
            "result_sha256": item.selected_result.canonical_hash,
        }
        for item in entries
    ]
    dialogue = [
        {
            "ordinal": item.ordinal,
            "guard": item.selected_result.dialogue_guard.to_mapping(),
            "guard_sha256": item.selected_result.dialogue_guard.canonical_hash,
        }
        for item in entries
    ]
    av = [
        {
            "ordinal": item.ordinal,
            "boundary_proof": item.selected_result.boundary_proof.to_mapping(),
            "boundary_proof_sha256": item.selected_result.boundary_proof.canonical_hash,
        }
        for item in entries
    ]
    timing = [
        {
            "ordinal": item.ordinal,
            "story_id": item.story_id,
            "beat_id": item.beat_id,
            "video_range": {
                "start_pts": item.selected_result.video_range.start_pts,
                "end_pts": item.selected_result.video_range.end_pts,
            },
            "audio_range": {
                "start_pts": item.selected_result.audio_range.start_pts,
                "end_pts": item.selected_result.audio_range.end_pts,
            },
        }
        for item in entries
    ]
    output = [item.to_mapping() for item in subjects]
    return (
        (
            len(report.stage3_member_refs)
            + 1
            + sum(len(row) for row in report.timed_media_child_member_refs),
            canonical_sha256(predecessor_census),
        ),
        (len(choices), canonical_sha256(choices)),
        (len(queries), canonical_sha256(queries)),
        (len(relations), canonical_sha256(relations)),
        (len(dialogue), canonical_sha256(dialogue)),
        (len(av), canonical_sha256(av)),
        (len(timing), canonical_sha256(timing)),
        (len(output), canonical_sha256(output)),
    )


def _replay(
    report: PhysicalEditCompilationReport,
    subjects: tuple[PhysicalEditRecipeSubject, ...],
) -> PhysicalEditReplayEvidence:
    return PhysicalEditReplayEvidence(
        tuple(
            PhysicalEditReplayFact(rule_id, count, digest)
            for rule_id, (count, digest) in zip(
                PHYSICAL_EDIT_RULE_IDS, _golden_censuses(report, subjects), strict=True
            )
        ),
        PHYSICAL_EDIT_REPLAY_EVALUATOR_STRATEGY_VERSION,
    )


def _wire(value: object) -> dict[str, object]:
    mapping = cast(object, getattr(value, "to_mapping")())
    return cast(dict[str, object], json.loads(json.dumps(mapping)))


def _independently_decoded_refs(
    refs: tuple[CommittedArtifactMemberReference, ...],
) -> tuple[CommittedArtifactMemberReference, ...]:
    return tuple(
        CommittedArtifactMemberReference.from_mapping(
            cast(dict[str, object], json.loads(json.dumps(item.to_mapping())))
        )
        for item in refs
    )


def _trusted_verify(
    report: PhysicalEditCompilationReport,
    admission: PhysicalEditAdmission,
    *,
    frozen_choice_order: tuple[PhysicalEditChoiceIdentity, ...] | None = None,
    expected_stage3_member_refs: tuple[CommittedArtifactMemberReference, ...] | None = None,
    expected_input_binding_sha256: str | None = None,
    expected_authority_sha256: str | None = None,
    expected_editorial_exact_policy_sha256: str | None = None,
    expected_candidate_exact_policy_sha256: str | None = None,
) -> VerifiedPhysicalEditAdmission:
    children = tuple(
        _independently_decoded_refs(row) for row in report.timed_media_child_member_refs
    )
    batch = CommittedArtifactMemberReference.from_mapping(
        cast(dict[str, object], json.loads(json.dumps(report.media_batch_member_ref.to_mapping())))
    )
    choices = frozen_choice_order or (
        PhysicalEditChoiceIdentity(
            0, "story-1", "beat-1", "requirement-1", "alternative-1", "candidate-1", 0, 0
        ),
    )
    return verify_physical_edit_admission(
        admission,
        report=report,
        recipe_subjects=_subjects(),
        expected_job_scope=ArtifactScope("pipeline", "job", "run-1"),
        expected_input_binding_sha256=(
            report.input_binding_sha256
            if expected_input_binding_sha256 is None
            else expected_input_binding_sha256
        ),
        expected_authority_sha256=(
            report.authority_sha256
            if expected_authority_sha256 is None
            else expected_authority_sha256
        ),
        expected_editorial_exact_policy_sha256=(
            report.editorial_exact_policy_sha256
            if expected_editorial_exact_policy_sha256 is None
            else expected_editorial_exact_policy_sha256
        ),
        expected_candidate_exact_policy_sha256=(
            report.candidate_exact_policy_sha256
            if expected_candidate_exact_policy_sha256 is None
            else expected_candidate_exact_policy_sha256
        ),
        expected_stage3_member_refs=(
            expected_stage3_member_refs
            if expected_stage3_member_refs is not None
            else _independently_decoded_refs(report.stage3_member_refs)
        ),
        expected_media_batch_member_ref=batch,
        expected_timed_media_child_member_refs=children,
        frozen_choice_order=choices,
        replay_evidence=_replay(report, _subjects()),
    )


def _forge_admission_binding(
    admission: PhysicalEditAdmission, field_name: str
) -> PhysicalEditAdmission:
    if field_name == "input_binding_sha256":
        return replace(admission, input_binding_sha256=_hash("e"))
    if field_name == "authority_sha256":
        return replace(admission, authority_sha256=_hash("e"))
    if field_name == "editorial_exact_policy_sha256":
        return replace(admission, editorial_exact_policy_sha256=_hash("e"))
    if field_name == "candidate_exact_policy_sha256":
        return replace(admission, candidate_exact_policy_sha256=_hash("e"))
    raise AssertionError(f"unsupported test field: {field_name}")


def test_report_and_admission_roundtrip_canonically_and_are_frozen() -> None:
    report = _report()
    decoded_report = PhysicalEditCompilationReport.from_mapping(report.to_mapping())
    assert decoded_report == report
    assert decoded_report.canonical_hash == report.canonical_hash

    admission = build_physical_edit_admission(report, _subjects(), _replay(report, _subjects()))
    decoded = PhysicalEditAdmission.from_mapping(admission.to_mapping())
    assert decoded == admission
    assert decoded.canonical_hash == admission.canonical_hash
    assert decoded.validation_status == "valid"
    assert decoded.next_action == "render"
    assert decoded.authority_status == "unverified"
    assert tuple(item.rule_id for item in decoded.checks) == PHYSICAL_EDIT_RULE_IDS
    with pytest.raises(FrozenInstanceError):
        decoded.next_action = "stop"  # type: ignore[misc]


def test_trusted_verifier_is_the_only_render_authorization_boundary() -> None:
    report = _report()
    candidate = build_physical_edit_admission(report, _subjects(), _replay(report, _subjects()))

    verified = _trusted_verify(report, candidate)

    assert type(verified) is VerifiedPhysicalEditAdmission
    assert verified.admission == candidate
    assert verified.render_authorized is True
    assert verified.verification_binding_sha256.startswith("sha256:")


@pytest.mark.parametrize("backend", ("installed_cpu_profile", "runtime_cuda_capability"))
@pytest.mark.parametrize(
    "field_name",
    (
        "input_binding_sha256",
        "authority_sha256",
        "editorial_exact_policy_sha256",
        "candidate_exact_policy_sha256",
    ),
)
def test_trusted_verifier_rejects_independent_and_candidate_binding_tamper(
    backend: PhysicalEditBackend, field_name: str
) -> None:
    report = _report(backend)
    candidate = build_physical_edit_admission(report, _subjects(), _replay(report, _subjects()))
    tampered = _hash("e")

    with pytest.raises(PhysicalEditAdmissionError):
        _trusted_verify(
            report,
            candidate,
            expected_input_binding_sha256=(
                tampered if field_name == "input_binding_sha256" else None
            ),
            expected_authority_sha256=(tampered if field_name == "authority_sha256" else None),
            expected_editorial_exact_policy_sha256=(
                tampered if field_name == "editorial_exact_policy_sha256" else None
            ),
            expected_candidate_exact_policy_sha256=(
                tampered if field_name == "candidate_exact_policy_sha256" else None
            ),
        )

    with pytest.raises(PhysicalEditAdmissionError):
        _trusted_verify(report, _forge_admission_binding(candidate, field_name))


def test_trusted_verifier_rejects_direct_and_decoded_forged_admissions() -> None:
    report = _report()
    expected = build_physical_edit_admission(report, _subjects(), _replay(report, _subjects()))
    forged_check = replace(
        expected.checks[0], expected_sha256=_hash("e"), observed_sha256=_hash("e")
    )
    forged_direct = replace(expected, checks=(forged_check, *expected.checks[1:]))
    with pytest.raises(PhysicalEditAdmissionError):
        _trusted_verify(report, forged_direct)

    forged_wire = _wire(expected)
    first_check = cast(dict[str, object], cast(list[object], forged_wire["checks"])[0])
    first_check["expected_sha256"] = _hash("e")
    first_check["observed_sha256"] = _hash("e")
    forged_decoded = PhysicalEditAdmission.from_mapping(forged_wire)
    with pytest.raises(PhysicalEditAdmissionError):
        _trusted_verify(report, forged_decoded)

    authority_wire = _wire(expected)
    authority_wire["authority_status"] = "verified"
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditAdmission.from_mapping(authority_wire)


def test_trusted_verifier_rejects_forged_predecessor_and_choice_order() -> None:
    report = _report()
    candidate = build_physical_edit_admission(report, _subjects(), _replay(report, _subjects()))
    expected_refs = list(_independently_decoded_refs(report.stage3_member_refs))
    expected_refs[0] = replace(expected_refs[0], content_hash=_hash("e"))
    with pytest.raises(PhysicalEditAdmissionError):
        _trusted_verify(report, candidate, expected_stage3_member_refs=tuple(expected_refs))

    wrong_choice = PhysicalEditChoiceIdentity(
        0, "story-1", "beat-1", "requirement-1", "alternative-1", "other", 0, 0
    )
    with pytest.raises(PhysicalEditAdmissionError):
        _trusted_verify(report, candidate, frozen_choice_order=(wrong_choice,))


def test_cuda_report_layout_and_authority_have_positive_parity() -> None:
    report = _report("runtime_cuda_capability")
    candidate = build_physical_edit_admission(report, _subjects(), _replay(report, _subjects()))

    verified = _trusted_verify(report, candidate)

    assert verified.render_authorized is True
    assert report.entries[0].selected_result.dialogue_guard.original_authority_kind is (
        CandidateTimedSpeechAuthorityKind.RUNTIME_CUDA_CAPABILITY
    )


def test_cuda_report_rejects_cpu_child_layout_and_authority() -> None:
    cuda = _report("runtime_cuda_capability")
    cpu = _report()
    with pytest.raises(PhysicalEditAdmissionError):
        replace(cuda, timed_media_child_member_refs=cpu.timed_media_child_member_refs)
    with pytest.raises(PhysicalEditAdmissionError):
        replace(cuda, entries=cpu.entries)


@pytest.mark.parametrize(
    ("mutation"),
    (
        lambda wire: wire.update({"unknown": 1}),
        lambda wire: wire.pop("entries"),
        lambda wire: wire.update({"entries": None}),
        lambda wire: cast(dict[str, object], cast(list[object], wire["entries"])[0]).update(
            {"ordinal": 0.0}
        ),
        lambda wire: cast(dict[str, object], cast(list[object], wire["entries"])[0]).update(
            {"episode_ordinal": False}
        ),
    ),
)
def test_report_closed_codec_rejects_unknown_missing_null_float_and_bool(
    mutation: Callable[[dict[str, object]], object],
) -> None:
    wire = _wire(_report())
    mutation(wire)
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditCompilationReport.from_mapping(wire)


def test_attempt_outcome_closure_and_entry_ordering_are_strict() -> None:
    query = _query()
    result = _result(query)
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditCompilationAttempt(
            "tight", "selected", "STAGE4_SPAN_SELECTED", query.canonical_hash, None
        )
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditCompilationAttempt(
            "tight",
            "no_legal_span",
            "STAGE4_NO_LEGAL_SPAN",
            query.canonical_hash,
            result.canonical_hash,
        )
    selected = PhysicalEditCompilationAttempt(
        "tight", "selected", "STAGE4_SPAN_SELECTED", query.canonical_hash, result.canonical_hash
    )
    rejected = PhysicalEditCompilationAttempt(
        "scene", "no_legal_span", "STAGE4_NO_LEGAL_SPAN", query.canonical_hash, None
    )
    with pytest.raises(PhysicalEditAdmissionError):
        replace(_entry(), attempts=(selected, rejected))
    with pytest.raises(PhysicalEditAdmissionError):
        replace(_entry(), attempts=(selected, selected))


def test_report_rejects_order_duplicate_and_child_census_mismatch() -> None:
    report = _report()
    with pytest.raises(PhysicalEditAdmissionError):
        replace(report, entries=(replace(_entry(), ordinal=1),))
    with pytest.raises(PhysicalEditAdmissionError):
        replace(report, entries=(_entry(), replace(_entry(), ordinal=1)))
    with pytest.raises(PhysicalEditAdmissionError):
        replace(
            report, timed_media_child_member_refs=(report.timed_media_child_member_refs[0][:-1],)
        )
    with pytest.raises(PhysicalEditAdmissionError):
        replace(report, entries=(replace(_entry(), episode_ordinal=1),))


def test_report_rejects_noncanonical_stage3_child_and_job_scope_layouts() -> None:
    stage3_wire = _wire(_report())
    stage3_refs = cast(list[object], stage3_wire["stage3_member_refs"])
    cast(dict[str, object], stage3_refs[0])["artifact_type"] = "editorial_blueprint_set"
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditCompilationReport.from_mapping(stage3_wire)

    child_wire = _wire(_report())
    child_rows = cast(list[object], child_wire["timed_media_child_member_refs"])
    first_child = cast(list[object], child_rows[0])
    cast(dict[str, object], first_child[2])["artifact_type"] = (
        "runtime_timed_speech_capability_admission"
    )
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditCompilationReport.from_mapping(child_wire)

    scope_wire = _wire(_report())
    refs = [
        *cast(list[object], scope_wire["stage3_member_refs"]),
        scope_wire["media_batch_member_ref"],
        *cast(list[object], cast(list[object], scope_wire["timed_media_child_member_refs"])[0]),
    ]
    for ref in refs:
        scope = cast(dict[str, object], cast(dict[str, object], ref)["scope"])
        scope["namespace"] = "other"
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditCompilationReport.from_mapping(scope_wire)


def test_report_codec_rejects_embedded_query_and_result_tampering() -> None:
    query_wire = _wire(_report())
    entry = cast(dict[str, object], cast(list[object], query_wire["entries"])[0])
    cast(dict[str, object], entry["selected_query"])["candidate_id"] = "forged"
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditCompilationReport.from_mapping(query_wire)

    result_wire = _wire(_report())
    entry = cast(dict[str, object], cast(list[object], result_wire["entries"])[0])
    cast(dict[str, object], entry["selected_result"])["request_sha256"] = _hash("e")
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditCompilationReport.from_mapping(result_wire)


def test_recipe_subject_is_precommit_closed_and_tamper_is_not_renderable() -> None:
    subject_wire = _wire(_subjects()[0])
    subject_wire["artifact_type"] = "receipt"
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditRecipeSubject.from_mapping(subject_wire)

    report = _report()
    subjects = _subjects()
    facts = list(_replay(report, subjects).facts)
    output = facts[-1]
    facts[-1] = replace(output, evidence_sha256=_hash("e"))
    admission = build_physical_edit_admission(
        report,
        subjects,
        PhysicalEditReplayEvidence(tuple(facts), PHYSICAL_EDIT_REPLAY_EVALUATOR_STRATEGY_VERSION),
    )
    assert admission.checks[-1].status == "fail"
    assert (admission.validation_status, admission.next_action) == ("invalid", "stop")

    with pytest.raises(PhysicalEditAdmissionError):
        build_physical_edit_admission(
            report,
            (replace(subjects[0], story_id="other-story"),),
            _replay(report, subjects),
        )


def test_check_and_admission_cannot_self_forge_pass_or_render() -> None:
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditCheck("PE-IN-001", "pass", 1, _hash("1"), 1, _hash("2"), ())
    report = _report()
    subjects = _subjects()
    facts = list(_replay(report, subjects).facts)
    facts[0] = replace(facts[0], evidence_sha256=_hash("e"))
    admission = build_physical_edit_admission(
        report,
        subjects,
        PhysicalEditReplayEvidence(tuple(facts), PHYSICAL_EDIT_REPLAY_EVALUATOR_STRATEGY_VERSION),
    )
    wire = _wire(admission)
    wire["validation_status"] = "valid"
    wire["next_action"] = "render"
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditAdmission.from_mapping(wire)

    with pytest.raises(TypeError):
        build_physical_edit_admission(  # type: ignore[call-arg]
            report, subjects, _replay(report, subjects), checks=admission.checks
        )


def test_indeterminate_or_failed_rule_never_renders() -> None:
    report = _report()
    subjects = _subjects()
    facts = list(_replay(report, subjects).facts)
    facts[4] = PhysicalEditReplayFact("PE-DLG-001", None, None, "PE_DLG_001_REPLAY_INDETERMINATE")
    admission = build_physical_edit_admission(
        report,
        subjects,
        PhysicalEditReplayEvidence(tuple(facts), PHYSICAL_EDIT_REPLAY_EVALUATOR_STRATEGY_VERSION),
    )
    assert admission.checks[4].status == "indeterminate"
    assert (admission.validation_status, admission.next_action) == ("indeterminate", "quarantine")


@pytest.mark.parametrize(
    ("field", "value"),
    (("checks", None), ("validation_status", None), ("recipe_subjects", False)),
)
def test_admission_codec_rejects_null_and_wrong_json_types(field: str, value: object) -> None:
    report = _report()
    subjects = _subjects()
    admission = build_physical_edit_admission(report, subjects, _replay(report, subjects))
    wire = _wire(admission)
    wire[field] = value
    with pytest.raises(PhysicalEditAdmissionError):
        PhysicalEditAdmission.from_mapping(wire)
