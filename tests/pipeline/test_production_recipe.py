from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from typing import Callable, cast
from uuid import UUID

import pytest
from autocut_kernel.media.types import TickRange, TimeBase
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
from autocut_kernel.pipeline.production_recipe import (
    PRODUCTION_RECIPE_PRODUCER_ID,
    ProductionBeat,
    ProductionRecipe,
    ProductionRecipeError,
    ProductionSpan,
    ProductionStory,
)
from autocut_kernel.store.models import ArtifactScope, BlobRef, CommittedArtifactMemberReference
from autocut_kernel.vlm.models import VlmEditingMode


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _query(
    *,
    story_id: str = "story-1",
    beat_id: str = "beat-1",
    requirement_id: str = "requirement-1",
    alternative_id: str = "alternative-1",
    candidate_id: str = "candidate-1",
    source_id: str = "source-1",
    video_clock_id: str = "source-1:video_pts",
    blueprint_beat_sha256: str = _hash("2"),
) -> EditorialExactSpanQuery:
    video_time_base = TimeBase(1, 90_000)
    desired = VideoClockRange(
        source_id,
        _hash("1"),
        video_clock_id,
        video_time_base,
        TickRange(0, 120),
    )
    anchor = VideoClockRange(
        source_id,
        _hash("1"),
        video_clock_id,
        video_time_base,
        TickRange(10, 100),
    )
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
        blueprint_beat_sha256=blueprint_beat_sha256,
        evidence_requirement_sha256=_hash("5"),
        alternative_sha256=_hash("6"),
        catalog_candidate_sha256=_hash("7"),
        semantic_pack_sha256=_hash("8"),
        timed_evidence_sha256=_hash("9"),
        dialogue_protection_kind="known_speech",
        request=ExactAvSpanRequest(desired, anchor, 1, DialogueRequirement.NOT_REQUIRED),
    )


def _result(query: EditorialExactSpanQuery) -> CandidateExactSpanResult:
    video_time_base = TimeBase(1, 90_000)
    audio_time_base = TimeBase(1, 48_000)
    proof = BoundaryProof(
        "source-1",
        _hash("1"),
        "source-1:video_pts",
        video_time_base,
        10,
        100,
        "source-1:audio_sample",
        audio_time_base,
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
        original_authority_kind=CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE,
        original_authority_sha256=_hash("d"),
        guard_policy_sha256=_hash("e"),
        source_id="source-1",
        source_sha256=_hash("1"),
        source_audio_clock_id="source-1:audio_sample",
        source_audio_time_base=audio_time_base,
        source_audio_range=TickRange(0, 64),
        requirement=DialogueRequirement.NOT_REQUIRED,
        kind=DialogueGuardKind.NOT_REQUIRED,
        reason="blueprint_does_not_require_complete_dialogue",
        protected_ranges=(),
    )
    return CandidateExactSpanResult(
        video_range=TickRange(10, 100),
        audio_range=TickRange(5, 53),
        boundary_proof=proof,
        dialogue_guard=guard,
        common_segment_ordinal=0,
        canonical_decision_key=(0, 0, 0, 0, 90, 48, 10, 100, 5, 53),
        logical_cartesian_count_decimal="16",
        visited_av_pair_count=4,
        feasible_count=2,
        request_sha256=query.request.canonical_hash,
        policy_sha256=_hash("f"),
        candidate_domain_sha256=_hash("a"),
        feasible_relation_sha256=_hash("b"),
    )


def _source_blob() -> BlobRef:
    return BlobRef(UUID("11111111-1111-4111-8111-111111111111"), _hash("1"), 4096, "video/mp4")


def _source_manifest_ref() -> CommittedArtifactMemberReference:
    return CommittedArtifactMemberReference(
        UUID("22222222-2222-4222-8222-222222222222"),
        UUID("33333333-3333-4333-8333-333333333333"),
        0,
        ArtifactScope("pipeline", "job", "run-1"),
        "whole_series_source_manifest",
        "whole_series_source_manifest",
        1,
        _hash("c"),
    )


def _span(*, ordinal: int = 0, query: EditorialExactSpanQuery | None = None) -> ProductionSpan:
    selected_query = query or _query()
    return ProductionSpan.from_exact_span(
        ordinal=ordinal,
        source_blob=_source_blob(),
        source_manifest_ref=_source_manifest_ref(),
        query=selected_query,
        result=_result(selected_query),
    )


def _recipe() -> ProductionRecipe:
    span = _span()
    beat = ProductionBeat(0, "beat-1", _hash("2"), (span,))
    story = ProductionStory(0, "story-1", _hash("3"), (beat,))
    return ProductionRecipe(PRODUCTION_RECIPE_PRODUCER_ID, "render-production-v1", _hash("4"), story)


def _wire() -> dict[str, object]:
    return cast(dict[str, object], json.loads(json.dumps(_recipe().to_mapping())))


def _story_wire(value: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], value["story"])


def _beat_wire(value: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], cast(list[object], _story_wire(value)["beats"])[0])


def _span_wire(value: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], cast(list[object], _beat_wire(value)["spans"])[0])


def test_production_recipe_roundtrips_canonically_and_is_immutable() -> None:
    recipe = _recipe()
    decoded = ProductionRecipe.from_mapping(recipe.to_mapping())

    assert decoded == recipe
    assert decoded.to_mapping() == recipe.to_mapping()
    assert decoded.canonical_hash == recipe.canonical_hash
    with pytest.raises(FrozenInstanceError):
        decoded.profile_id = "changed"  # type: ignore[misc]


def test_recipe_retains_independent_av_endpoints_pairing_and_render_source() -> None:
    span = _recipe().story.beats[0].spans[0]
    proof = span.exact_span_result.boundary_proof

    assert (proof.video_clock_id, proof.video_time_base, proof.video_in_tick, proof.video_out_tick) == (
        "source-1:video_pts",
        TimeBase(1, 90_000),
        10,
        100,
    )
    assert (proof.audio_clock_id, proof.audio_time_base, proof.audio_in_tick, proof.audio_out_tick) == (
        "source-1:audio_sample",
        TimeBase(1, 48_000),
        5,
        53,
    )
    assert span.av_pairing_proof_sha256 == proof.clock_map_certificate_sha256
    assert span.exact_span_proof_sha256 == proof.canonical_hash
    assert span.source_blob.content_hash == proof.source_sha256
    assert span.source_manifest_ref.artifact_type == "whole_series_source_manifest"


def test_one_blueprint_beat_can_retain_multiple_requirement_candidate_spans() -> None:
    first = _span()
    second_query = _query(
        requirement_id="requirement-2",
        alternative_id="alternative-2",
        candidate_id="candidate-2",
    )
    second = _span(ordinal=1, query=second_query)
    beat = ProductionBeat(0, "beat-1", _hash("2"), (first, second))

    assert tuple(item.candidate_id for item in beat.spans) == ("candidate-1", "candidate-2")
    assert ProductionBeat.from_mapping(beat.to_mapping()) == beat


def test_empty_recipe_story_and_beat_are_rejected() -> None:
    with pytest.raises(ProductionRecipeError, match="exactly one Story"):
        ProductionRecipe(
            PRODUCTION_RECIPE_PRODUCER_ID,
            "production-v1",
            _hash("1"),
            cast(ProductionStory, ()),
        )
    with pytest.raises(ProductionRecipeError, match="at least one Beat"):
        ProductionStory(0, "story-1", _hash("1"), ())
    with pytest.raises(ProductionRecipeError, match="at least one span"):
        ProductionBeat(0, "beat-1", _hash("1"), ())


def test_duplicate_beat_identity_and_duplicate_ordinals_are_rejected() -> None:
    beat = _recipe().story.beats[0]
    duplicate_id = replace(beat, ordinal=1)
    with pytest.raises(ProductionRecipeError, match="repeats a Beat"):
        ProductionStory(0, "story-1", _hash("3"), (beat, duplicate_id))

    with pytest.raises(ProductionRecipeError, match="ordinals"):
        ProductionStory(0, "story-1", _hash("3"), (beat, beat))


def test_two_story_plural_wire_shape_is_rejected_as_unknown() -> None:
    wire = _wire()
    story = wire.pop("story")
    wire["stories"] = [story, story]

    with pytest.raises(ProductionRecipeError, match="missing or unknown fields"):
        ProductionRecipe.from_mapping(wire)


Mutation = Callable[[dict[str, object]], None]


def _unknown(value: dict[str, object]) -> None:
    value["unknown"] = "forbidden"


def _null(value: dict[str, object]) -> None:
    value["profile_id"] = None


def _float_tick(value: dict[str, object]) -> None:
    result = cast(dict[str, object], _span_wire(value)["exact_span_result"])
    cast(dict[str, object], result["video_range"])["start_pts"] = 10.0


def _bool_ordinal(value: dict[str, object]) -> None:
    _beat_wire(value)["ordinal"] = False


@pytest.mark.parametrize("mutate", (_unknown, _null, _float_tick, _bool_ordinal))
def test_strict_codec_rejects_unknown_null_float_and_bool_as_int(mutate: Mutation) -> None:
    wire = _wire()
    mutate(wire)
    with pytest.raises(ProductionRecipeError):
        ProductionRecipe.from_mapping(wire)


def test_fixture_producer_profile_and_source_are_rejected() -> None:
    wire = _wire()
    wire["producer_id"] = "fixture_ground_truth_v1"
    with pytest.raises(ProductionRecipeError, match="producer"):
        ProductionRecipe.from_mapping(wire)

    wire = _wire()
    wire["profile_id"] = "fixture-render-v1"
    with pytest.raises(ProductionRecipeError, match="fixture profiles"):
        ProductionRecipe.from_mapping(wire)

    wire = _wire()
    cast(dict[str, object], _span_wire(wire)["source_blob"])["media_type"] = (
        "application/x-fixture-video"
    )
    with pytest.raises(ProductionRecipeError, match="fixture source"):
        ProductionRecipe.from_mapping(wire)


@pytest.mark.parametrize(
    "field",
    ("exact_span_query_sha256", "exact_span_result_sha256", "exact_span_proof_sha256"),
)
def test_tampered_query_result_and_proof_hashes_are_rejected(field: str) -> None:
    wire = _wire()
    _span_wire(wire)[field] = _hash("f")

    with pytest.raises(ProductionRecipeError, match="stale or forged"):
        ProductionRecipe.from_mapping(wire)


def test_tampered_embedded_query_result_and_pairing_proof_are_rejected() -> None:
    wire = _wire()
    query = cast(dict[str, object], _span_wire(wire)["exact_span_query"])
    query["candidate_id"] = "candidate-forged"
    with pytest.raises(ProductionRecipeError):
        ProductionRecipe.from_mapping(wire)

    wire = _wire()
    result = cast(dict[str, object], _span_wire(wire)["exact_span_result"])
    result["feasible_relation_sha256"] = _hash("f")
    with pytest.raises(ProductionRecipeError, match="result content hash"):
        ProductionRecipe.from_mapping(wire)

    wire = _wire()
    _span_wire(wire)["av_pairing_proof_sha256"] = _hash("f")
    with pytest.raises(ProductionRecipeError, match="pairing proof"):
        ProductionRecipe.from_mapping(wire)


def test_cross_source_and_clock_mismatches_are_rejected() -> None:
    query = _query(source_id="foreign-source", video_clock_id="foreign:video")
    result = replace(_result(query), request_sha256=query.request.canonical_hash)
    with pytest.raises(ProductionRecipeError, match="video Source clock mismatch"):
        ProductionSpan.from_exact_span(
            ordinal=0,
            source_blob=_source_blob(),
            source_manifest_ref=_source_manifest_ref(),
            query=query,
            result=result,
        )

    query = _query()
    result = _result(query)
    foreign_proof = replace(result.boundary_proof, audio_clock_id="foreign:audio")
    with pytest.raises(ProductionRecipeError, match="audio Source clock mismatch"):
        ProductionSpan.from_exact_span(
            ordinal=0,
            source_blob=_source_blob(),
            source_manifest_ref=_source_manifest_ref(),
            query=query,
            result=replace(result, boundary_proof=foreign_proof),
        )


def test_render_source_blob_and_manifest_owner_must_close() -> None:
    query = _query()
    result = _result(query)
    with pytest.raises(ProductionRecipeError, match="BlobRef differs"):
        ProductionSpan.from_exact_span(
            ordinal=0,
            source_blob=replace(_source_blob(), content_hash=_hash("f")),
            source_manifest_ref=_source_manifest_ref(),
            query=query,
            result=result,
        )

    wrong_ref = replace(_source_manifest_ref(), artifact_type="fixture_source_manifest")
    with pytest.raises(ProductionRecipeError, match="non-production owner"):
        ProductionSpan.from_exact_span(
            ordinal=0,
            source_blob=_source_blob(),
            source_manifest_ref=wrong_ref,
            query=query,
            result=result,
        )
