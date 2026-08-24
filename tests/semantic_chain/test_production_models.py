from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from autocut_kernel.contracts.compiler.refs import ArtifactRef, DomainRef
from autocut_kernel.semantic_chain.production_models import (
    BlueprintBeat,
    Candidate,
    CandidateAlternative,
    CandidateCatalog,
    ContextBudget,
    ContextManifest,
    CoverageAdmission,
    CoverageDisposition,
    CoverageLedger,
    CoverageResolution,
    CoverageRow,
    CoverageUnitType,
    DurationRangeSeconds,
    EditingMode,
    EditorialBlueprint,
    EpisodeDigest,
    EpisodeDigestSet,
    EventCard,
    EventCardSet,
    EvidenceClosure,
    EvidenceClosureMember,
    EvidenceClosureSet,
    EvidenceRequirement,
    MaterialRequirement,
    NarrativeEdge,
    NarrativeGraph,
    NarrativeNode,
    NarrativeNodeType,
    OwnerBoundVlmObservationRef,
    PhysicalRequirement,
    Portfolio,
    PortfolioAdmission,
    ProductionModelError,
    Proposal,
    RequiredClosure,
    SemanticFeasibilityAdmission,
    SpanPolicy,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


def _artifact(name: str, digest: str = HASH_A) -> ArtifactRef:
    return ArtifactRef(name, digest)


def _domain(owner: str, object_type: str, object_id: str, digest: str = HASH_A) -> DomainRef:
    return DomainRef(_artifact(owner, digest), object_type, object_id)


def _observation(
    editing_modes: tuple[EditingMode, ...] = (EditingMode.DIALOGUE,),
) -> OwnerBoundVlmObservationRef:
    return OwnerBoundVlmObservationRef(
        observation_ref=_domain("art_vlm", "vlm_observation", "obs_001"),
        vlm_observation_sha256=HASH_B,
        source_ref=_domain("art_sources", "source", "source_001"),
        window_ref=_domain("art_windows", "vlm_window", "window_001"),
        capability_policy_ref=_artifact("art_capability_policy", HASH_C),
        editing_modes=editing_modes,
    )


def _candidate(
    editing_modes: tuple[EditingMode, ...] = (EditingMode.DIALOGUE,),
) -> Candidate:
    return Candidate.from_vlm_capability(
        candidate_id="cand_001",
        event_refs=(_domain("art_events", "event", "event_001"),),
        observation=_observation(editing_modes),
        supported_narrative_functions=("hook_and_orient",),
        usable_duration_seconds=24,
        authorization_ref=_domain("art_sources", "source_authorization", "source_001"),
    )


def _physical_requirements() -> tuple[PhysicalRequirement, ...]:
    return (
        PhysicalRequirement("dialogue_integrity", "complete"),
        PhysicalRequirement("subtitle_clearance", "protect_detected_cues"),
        PhysicalRequirement("visual_validity", "endpoint_and_stable_region"),
    )


def _material_requirement() -> MaterialRequirement:
    return MaterialRequirement(
        requirement_id="mr_001",
        obligation_ref=_domain("art_graph", "obligation", "obl_001"),
        minimum_usable_seconds=12,
        physical_requirements=_physical_requirements(),
        allowed_source_refs=(),
        forbidden_source_refs=(),
    )


def _proposal() -> Proposal:
    return Proposal(
        proposal_id="proposal_001",
        story_id="story_001",
        title="A closed proposal",
        narrative_claim="A committed observation supports the story.",
        thread_refs=(_domain("art_graph", "story_thread", "thread_001"),),
        required_obligation_refs=(_domain("art_graph", "obligation", "obl_001"),),
        required_fact_refs=(_domain("art_graph", "fact", "fact_001"),),
        key_character_refs=(),
        genre_tags=("suspense",),
        editing_profile="dramatic_short",
        target_duration_seconds=DurationRangeSeconds(300, 360, 420),
        teaser_strategy="cold_open",
        material_requirements=(_material_requirement(),),
        candidate_refs=(_domain("art_candidates", "candidate", "cand_001"),),
    )


def _evidence_requirement() -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="er_001",
        source_material_requirement_id="mr_001",
        satisfaction="one_of",
        alternative_sets=(
            CandidateAlternative(
                alternative_id="alt_001",
                event_refs=(_domain("art_events", "event", "event_001"),),
                candidate_refs=(_domain("art_candidates", "candidate", "cand_001"),),
            ),
        ),
        physical_requirements=_physical_requirements(),
    )


def _blueprint() -> EditorialBlueprint:
    return EditorialBlueprint(
        blueprint_id="blueprint_001",
        story_id="story_001",
        proposal_ref=_domain("art_proposals", "proposal", "proposal_001"),
        beats=(
            BlueprintBeat(
                blueprint_beat_id="bpbeat_001",
                narrative_role="setup",
                narrative_function="hook_and_orient",
                summary="Orient the audience.",
                required_obligation_refs=(_domain("art_graph", "obligation", "obl_001"),),
                required_fact_refs=(_domain("art_graph", "fact", "fact_001"),),
                evidence_requirements=(_evidence_requirement(),),
                candidate_preferences=(_domain("art_candidates", "candidate", "cand_001"),),
                span_policy=SpanPolicy(
                    preferred="scene",
                    allowed=("tight", "scene", "context"),
                    fallback_order=("scene", "context", "tight"),
                ),
                duration_seconds=DurationRangeSeconds(18, 24, 35),
            ),
        ),
        story_duration_seconds=DurationRangeSeconds(300, 360, 420),
        pacing="balanced",
        continuity_priority="high",
        teaser_strategy="cold_open",
        teaser_duration_seconds=DurationRangeSeconds(8, 12, 20),
    )


def _walk(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_editing_modes_are_nonempty_closed_unique_and_canonical() -> None:
    mixed = _observation((EditingMode.DIALOGUE, EditingMode.ACTION))

    assert mixed.editing_modes == (EditingMode.DIALOGUE, EditingMode.ACTION)
    assert mixed.to_mapping()["editing_modes"] == ["dialogue", "action"]

    for modes in (
        (),
        (EditingMode.DIALOGUE, EditingMode.DIALOGUE),
        (EditingMode.ACTION, EditingMode.DIALOGUE),
        ("mixed",),
    ):
        with pytest.raises(ProductionModelError):
            _observation(modes)  # type: ignore[arg-type]


def test_candidate_modes_and_observation_hash_cannot_be_overridden() -> None:
    candidate = _candidate((EditingMode.DIALOGUE, EditingMode.ACTION))
    payload = candidate.to_mapping()

    assert candidate.editing_modes == (EditingMode.DIALOGUE, EditingMode.ACTION)
    assert candidate.vlm_observation_sha256 == HASH_B
    assert (
        Candidate.from_mapping(payload, observation=_observation(candidate.editing_modes))
        == candidate
    )

    payload["editing_modes"] = ["action"]
    with pytest.raises(ProductionModelError, match="override"):
        Candidate.from_mapping(payload, observation=_observation(candidate.editing_modes))

    payload = candidate.to_mapping()
    payload["vlm_observation_sha256"] = HASH_D
    with pytest.raises(ProductionModelError, match="override"):
        Candidate.from_mapping(payload, observation=_observation(candidate.editing_modes))


@pytest.mark.parametrize(
    "field,value",
    [
        ("transcript", "forbidden"),
        ("asr_text", "forbidden"),
        ("vad_segments", []),
        ("start_pts", 10),
        ("end_seconds", 1.5),
    ],
)
def test_candidate_closed_decoder_rejects_semantic_and_physical_overrides(
    field: str, value: object
) -> None:
    payload = _candidate().to_mapping()
    payload[field] = value

    with pytest.raises(ProductionModelError):
        Candidate.from_mapping(payload, observation=_observation())


def test_stage1_business_artifacts_are_immutable_and_hash_deterministic() -> None:
    observation = _observation()
    digest = EpisodeDigest(
        episode_id="episode_001",
        ordinal=1,
        summary="An episode summary.",
        source_window_refs=(observation.window_ref,),
        evidence_refs=(observation.observation_ref,),
    )
    digest_set = EpisodeDigestSet("digests_001", (digest,))
    event = EventCard(
        event_id="event_001",
        episode_id="episode_001",
        content="An owner-bound event occurred.",
        observation_refs=(observation,),
    )
    event_set = EventCardSet("events_001", (event,))
    node = NarrativeNode(
        "event_001",
        NarrativeNodeType.EVENT,
        "An owner-bound event occurred.",
        (_domain("art_events", "event", "event_001"),),
    )
    graph = NarrativeGraph(
        "graph_001",
        (node,),
        (NarrativeEdge("edge_001", "supports", "event_001", "event_001", ()),),
    )

    assert digest_set.canonical_hash == EpisodeDigestSet("digests_001", (digest,)).canonical_hash
    assert event_set.canonical_hash == EventCardSet("events_001", (event,)).canonical_hash
    assert (
        graph.canonical_hash
        == NarrativeGraph(
            "graph_001",
            (node,),
            (NarrativeEdge("edge_001", "supports", "event_001", "event_001", ()),),
        ).canonical_hash
    )
    with pytest.raises(FrozenInstanceError):
        digest.summary = "mutated"  # type: ignore[misc]


def test_coverage_ledger_derives_and_enforces_conservation() -> None:
    observation_ref = _observation().observation_ref
    window_ref = _observation().window_ref
    obligation_ref = _domain("art_graph", "obligation", "obl_001")
    rows = (
        CoverageRow(
            "cov_001",
            CoverageUnitType.VLM_OBSERVATION,
            observation_ref,
            CoverageResolution.RESOLVED,
            CoverageDisposition.NARRATIVE,
            (_domain("art_graph", "event", "event_001"),),
            (observation_ref,),
        ),
        CoverageRow(
            "cov_002",
            CoverageUnitType.VLM_WINDOW,
            window_ref,
            CoverageResolution.RESOLVED,
            CoverageDisposition.SUPPORTING,
            (_domain("art_graph", "event", "event_001"),),
            (observation_ref,),
        ),
        CoverageRow(
            "cov_003",
            CoverageUnitType.OBLIGATION,
            obligation_ref,
            CoverageResolution.RESOLVED,
            CoverageDisposition.NARRATIVE,
            (obligation_ref,),
            (observation_ref,),
        ),
    )
    inputs = (observation_ref, window_ref, obligation_ref)

    ledger = CoverageLedger.from_inputs("ledger_001", input_unit_refs=inputs, rows=rows)

    assert ledger.conservation.missing_unit_refs == ()
    assert ledger.conservation.duplicate_unit_refs == ()
    assert ledger.conservation.input_unit_count == ledger.conservation.ledger_unit_count == 3
    with pytest.raises(ProductionModelError, match="conservation"):
        CoverageLedger.from_inputs("ledger_001", input_unit_refs=inputs, rows=rows[:-1])
    with pytest.raises(ProductionModelError, match="duplicate"):
        CoverageLedger.from_inputs("ledger_001", input_unit_refs=inputs, rows=(*rows, rows[0]))


def test_candidate_catalog_portfolio_and_admission_freeze_derived_values() -> None:
    catalog = CandidateCatalog("candidates_001", (_candidate(),))
    proposal = _proposal()
    portfolio = Portfolio.from_selected_proposals(
        portfolio_id="portfolio_001",
        proposal_set_ref=_artifact("art_proposals"),
        selected_proposals=((0, proposal),),
        completion_policy="all_or_nothing",
    )
    admission = PortfolioAdmission.from_portfolio(
        admission_id="adm_portfolio_001",
        portfolio_ref=_artifact("art_portfolio", portfolio.canonical_hash),
        portfolio=portfolio,
        source_usage_ledger_ref=_artifact("art_usage"),
    )

    assert catalog.candidates[0].editing_modes == (EditingMode.DIALOGUE,)
    assert portfolio.target_story_ids == ("story_001",)
    assert portfolio.target_story_ids_hash == admission.target_story_ids_hash
    assert portfolio.target_story_ids == admission.target_story_ids


def test_stage3_has_no_physical_endpoints_or_float_seconds() -> None:
    blueprint = _blueprint()
    mapping = blueprint.to_mapping()

    forbidden_fragments = ("start_pts", "end_pts", "in_tick", "out_tick", "endpoint")
    for key, value in _walk(mapping):
        assert not any(fragment in key for fragment in forbidden_fragments)
        assert not isinstance(value, float)
    assert EditorialBlueprint.from_mapping(mapping) == blueprint

    mapping["start_pts"] = 0
    with pytest.raises(ProductionModelError):
        EditorialBlueprint.from_mapping(mapping)

    with pytest.raises(ProductionModelError):
        DurationRangeSeconds(1, 1.5, 2)  # type: ignore[arg-type]


def test_stage3_closure_context_and_admission_hashes_are_recomputable() -> None:
    blueprint = _blueprint()
    member = EvidenceClosureMember(
        kind="vlm_observation",
        source_ref=_observation().observation_ref,
        object_content_hash=HASH_B,
    )
    closure = EvidenceClosure(
        closure_id="closure_001",
        requirement_id="er_001",
        members=(member,),
        dependency_refs=(_domain("art_graph", "fact", "fact_001"),),
    )
    closure_set = EvidenceClosureSet("closure_set_001", "story_001", (closure,))
    context = ContextManifest(
        context_manifest_id="ctx_001",
        story_id="story_001",
        input_refs=(_artifact("art_graph"), _artifact("art_vlm", HASH_B)),
        evidence_closure_set_ref=_artifact("art_closure", closure_set.canonical_hash),
        required_closures=(RequiredClosure("closure_001", closure.closure_hash),),
        optional_context_refs=(),
        omissions=(),
        budget=ContextBudget("tokens", 15_000, 13_210, "tokenizer", "1.0.0"),
        builder_version="context-builder-2.1.3",
    )
    admission = SemanticFeasibilityAdmission.from_artifacts(
        admission_id="adm_semantic_001",
        story_id="story_001",
        blueprint_ref=_artifact("art_blueprint", blueprint.canonical_hash),
        evidence_closure_set_ref=_artifact("art_closure", closure_set.canonical_hash),
        context_manifest_ref=_artifact("art_context", context.canonical_hash),
        required_obligation_refs=blueprint.required_obligation_refs,
    )

    assert (
        closure.closure_hash
        == EvidenceClosure(
            "closure_001", "er_001", (member,), (_domain("art_graph", "fact", "fact_001"),)
        ).closure_hash
    )
    assert admission.evidence_closure_set_ref.content_hash == closure_set.canonical_hash
    assert context.required_closures == (RequiredClosure("closure_001", closure.closure_hash),)


def test_admission_types_are_stage_specific_and_fail_closed() -> None:
    ledger = CoverageLedger.from_inputs(
        "ledger_001",
        input_unit_refs=(_observation().observation_ref,),
        rows=(
            CoverageRow(
                "cov_001",
                CoverageUnitType.VLM_OBSERVATION,
                _observation().observation_ref,
                CoverageResolution.RESOLVED,
                CoverageDisposition.NARRATIVE,
                (_domain("art_graph", "event", "event_001"),),
                (_observation().observation_ref,),
            ),
        ),
    )

    admission = CoverageAdmission.from_ledger(
        admission_id="adm_coverage_001",
        ledger_ref=_artifact("art_ledger", ledger.canonical_hash),
        ledger=ledger,
        coverage_mode="strict_global",
        dependency_closure_hash=HASH_C,
    )

    assert admission.next_action == "continue"
    with pytest.raises(ProductionModelError):
        CoverageAdmission(
            "adm_coverage_001",
            _artifact("art_ledger", ledger.canonical_hash),
            "strict_global",
            "continue",
            ("taint_001",),
            HASH_C,
        )
