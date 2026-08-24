from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import TypeVar

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.contracts.compiler.refs import ArtifactRef, DomainRef
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.semantic_chain.production_models import (
    BlueprintBeat,
    BlueprintFragment,
    BlueprintMerger,
    CandidateAlternative,
    CommittedCandidateSemanticEvidence,
    CommittedVlmObservation,
    ConflictDiagnostics,
    CoverageAdmissionEvaluator,
    CoverageDisposition,
    CoverageLedger,
    CoveragePolicy,
    CoverageResolution,
    CoverageRow,
    CoverageUnitType,
    DependencyClosureEvaluator,
    DependencyClosureProof,
    DependencyPropagationPolicy,
    DiagnosticItem,
    DurationRangeSeconds,
    EpisodeDigest,
    EpisodeDigestSet,
    EventCard,
    EventCardSet,
    EvidenceClosure,
    EvidenceClosureMember,
    EvidenceDiagnostics,
    EvidenceRequirement,
    GenerationPartition,
    GenerationPartitionPlan,
    MergePolicy,
    NarrativeAttributes,
    NarrativeConfidence,
    NarrativeEdge,
    NarrativeFunction,
    NarrativeGraph,
    NarrativeNode,
    NarrativeNodeType,
    OrderingConstraint,
    PendingBusinessMember,
    PendingBusinessSet,
    PhysicalRequirement,
    PortfolioAdmissionEvaluator,
    PortfolioCompiler,
    ProductionModelError,
    RequiredClosure,
    SemanticAnchor,
    SemanticFeasibilityEvaluator,
    SemanticMeasurement,
    SourceAuthorizationRef,
    SourceRangeRef,
    SpanPolicy,
    TaintSeedProof,
    TimeBaseValue,
    stable_beat_id,
)
from autocut_kernel.vlm.models import (
    MappedSourceInterval,
    VlmObservation,
    VlmObservationKind,
    VlmObservationSet,
    VlmRequestIdentity,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
OBS_ID = "sha256:" + "1" * 64
WINDOW_HASH = "sha256:" + "2" * 64
FRAME_ID = "sha256:" + "3" * 64
T = TypeVar("T")


def _canon(*values: T) -> tuple[T, ...]:
    return tuple(sorted(values, key=lambda item: canonical_json_bytes(item.to_mapping())))  # type: ignore[attr-defined]


def _canon_strings(*values: str) -> tuple[str, ...]:
    return tuple(sorted(values, key=canonical_json_bytes))


def _artifact(name: str, digest: str = HASH_A) -> ArtifactRef:
    return ArtifactRef(name, digest)


def _domain(owner: ArtifactRef, object_type: str, object_id: str) -> DomainRef:
    return DomainRef(owner, object_type, object_id)


def _pending(admission_kind: str, *values: tuple[str, object]) -> PendingBusinessSet:
    members = tuple(
        PendingBusinessMember(
            artifact_type,
            _artifact(f"art_{artifact_type}", value.canonical_hash),  # type: ignore[attr-defined]
        )
        for artifact_type, value in values
    )
    return PendingBusinessSet("pending_001", admission_kind, _canon(*members))


def _raw_vlm() -> tuple[VlmRequestIdentity, VlmObservationSet, VlmObservation, ArtifactRef]:
    request = VlmRequestIdentity(
        window_manifest_sha256=WINDOW_HASH,
        source_id="source_001",
        source_clock_id="source_001:video_pts",
        source_sha256=HASH_A,
        frame_samples_sha256=HASH_B,
        frame_pts_index_set_sha256=HASH_C,
        window_manifest_set_sha256=HASH_D,
        proxy_blob_ref_sha256=HASH_A,
        preprocess_policy_sha256=HASH_B,
        window_sampling_policy_sha256=HASH_C,
        prompt_template_sha256=HASH_D,
        prompt_version="prompt-v1",
        response_schema_sha256=HASH_A,
        model_id="doubao-model",
        provider_id="doubao",
        request_parameters_sha256=HASH_B,
        request_payload_sha256=HASH_C,
        parse_policy_sha256=HASH_D,
    )
    observation = VlmObservation(
        observation_id=OBS_ID,
        kind=VlmObservationKind.CHANGE,
        summary="A character speaks while the scene changes.",
        confidence=Decimal("0.95"),
        supporting_frame_ids=(FRAME_ID,),
        source_interval=MappedSourceInterval(
            coarse_range=TickRange(0, 2_700_000),
            mapping_error_bound_source_pts=0,
            source_time_base=TimeBase(1, 90_000),
            provider_uncertainty_proxy_pts=0,
            proxy_time_base=TimeBase(1, 1_000),
        ),
        request_identity_sha256=request.canonical_hash,
        window_manifest_sha256=WINDOW_HASH,
        core_owned=True,
    )
    observation_set = VlmObservationSet(
        request_identity_sha256=request.canonical_hash,
        window_manifest_sha256=WINDOW_HASH,
        raw_response_sha256=HASH_D,
        observations=(observation,),
    )
    return (
        request,
        observation_set,
        observation,
        _artifact("art_vlm_observations", observation_set.canonical_hash),
    )


def test_raw_or_wrong_owner_vlm_values_cannot_mint_committed_authority() -> None:
    request, observation_set, observation, set_ref = _raw_vlm()
    for source_owner, window_owner in (
        ("art_sources", "art_windows"),
        ("attacker_source_owner", "attacker_window_owner"),
    ):
        with pytest.raises(ProductionModelError, match="Receipt/ArtifactSet-backed"):
            CommittedVlmObservation.from_reader(
                observation_set_ref=set_ref,
                observation_ref=_domain(set_ref, "vlm_observation", OBS_ID),
                source_ref=_domain(_artifact(source_owner), "source", "source_001"),
                window_ref=_domain(_artifact(window_owner), "vlm_window", WINDOW_HASH),
                request_identity=request,
                observation_set=observation_set,
                observation=observation,
            )


def test_caller_selected_vlm_anchors_measurements_and_modes_are_rejected() -> None:
    forged_committed = object.__new__(CommittedVlmObservation)
    observation_ref = _domain(_artifact("art_vlm"), "vlm_observation", OBS_ID)
    policy_ref = _artifact("art_measurement_policy")
    anchor = SemanticAnchor("action", observation_ref)
    measurement = SemanticMeasurement(
        "measurement_001",
        "action_salience",
        "1",
        "1",
        "audited_vlm_generation",
        policy_ref,
        _artifact("attacker_generation"),
        (observation_ref,),
    )
    with pytest.raises(ProductionModelError, match="caller-selected"):
        CommittedCandidateSemanticEvidence.from_reader(
            semantic_evidence_ref=_artifact("forged_semantic_evidence"),
            committed_observation=forged_committed,
            anchors=(anchor,),
            measurements=(measurement,),
            measurement_policy_ref=policy_ref,
        )


Stage1Values = tuple[
    EpisodeDigestSet,
    EventCardSet,
    NarrativeGraph,
    CoverageLedger,
    EvidenceDiagnostics,
    ConflictDiagnostics,
    DependencyClosureProof,
    DependencyPropagationPolicy,
]


def _stage1_values(*, conflicted: bool = False, unrelated_universe: bool = False) -> Stage1Values:
    window_ref = _domain(_artifact("art_windows"), "vlm_window", "window_001")
    observation_ref = _domain(_artifact("art_vlm"), "vlm_observation", "observation_001")
    digests = EpisodeDigestSet(
        "digests_001",
        (EpisodeDigest("episode_001", 1, "Episode.", (window_ref,), (observation_ref,)),),
    )
    event = EventCard(
        "event_001",
        "episode_001",
        "Event.",
        (
            SourceRangeRef(
                "source_001",
                "source_001:video_pts",
                TimeBaseValue(1, 90_000),
                0,
                90_000,
                _artifact("art_sources"),
            ),
        ),
        (observation_ref,),
    )
    events = EventCardSet("events_001", (event,))
    event_set_ref = _artifact("art_event_card_set", events.canonical_hash)
    event_ref = _domain(event_set_ref, "event", "event_001")
    event_node = NarrativeNode(
        "event_001",
        NarrativeNodeType.EVENT,
        "Event",
        NarrativeAttributes.from_mapping(
            {
                "attribute_type": "event",
                "event_card_ref": event_ref.to_mapping(),
                "episode_id": "episode_001",
                "summary": "Event.",
                "source_range_refs": [
                    _domain(event_set_ref, "source_range", "event_001:range:0").to_mapping()
                ],
            }
        ),
        (event_ref,),
        NarrativeConfidence("1", "source"),
    )
    obligation_node = NarrativeNode(
        "obligation_001",
        NarrativeNodeType.OBLIGATION,
        "Obligation",
        NarrativeAttributes.from_mapping(
            {
                "attribute_type": "obligation",
                "description": "Resolve the event.",
                "required_fact_ids": ["fact_001"],
                "success_criteria": "Resolved.",
            }
        ),
        (event_ref,),
        NarrativeConfidence("1", "rule"),
    )
    graph = NarrativeGraph(
        "graph_001",
        _canon(event_node, obligation_node),
        (NarrativeEdge("edge_001", "requires", "event_001", "obligation_001", ()),),
    )
    graph_ref = _artifact("art_narrative_graph", graph.canonical_hash)
    event_node_ref = _domain(graph_ref, "narrative_node", "event_001")
    obligation_ref = _domain(graph_ref, "narrative_node", "obligation_001")
    evidence = EvidenceDiagnostics("evidence_diagnostics_001", ())
    if conflicted:
        conflict_item = DiagnosticItem(
            "conflict_001",
            "fact_value_conflict",
            "error",
            event_ref,
            "KC-COV-004",
            "Competing claims.",
            (observation_ref,),
            (event_ref,),
            _canon(
                _domain(_artifact("art_claims"), "claim", "claim_001"),
                _domain(_artifact("art_claims"), "claim", "claim_002"),
            ),
        )
        conflicts = ConflictDiagnostics("conflict_diagnostics_001", (conflict_item,))
        conflict_ref = _artifact("art_conflict_diagnostics", conflicts.canonical_hash)
        event_row = CoverageRow(
            "coverage_event",
            CoverageUnitType.EVENT,
            event_ref,
            CoverageResolution.CONFLICTED,
            CoverageDisposition.NARRATIVE,
            (event_node_ref,),
            (observation_ref,),
            None,
            (_domain(conflict_ref, "diagnostic", "conflict_001"),),
            (_domain(_artifact("art_dependency_proof"), "taint_seed", "seed_001"),),
        )
    else:
        conflicts = ConflictDiagnostics("conflict_diagnostics_001", ())
        conflict_ref = _artifact("art_conflict_diagnostics", conflicts.canonical_hash)
        event_row = CoverageRow(
            "coverage_event",
            CoverageUnitType.EVENT,
            event_ref,
            CoverageResolution.RESOLVED,
            CoverageDisposition.NARRATIVE,
            (event_node_ref,),
            (observation_ref,),
            None,
            (),
            (),
        )
    rows = (
        event_row,
        CoverageRow(
            "coverage_observation",
            CoverageUnitType.VLM_OBSERVATION,
            observation_ref,
            CoverageResolution.RESOLVED,
            CoverageDisposition.SUPPORTING,
            (),
            (observation_ref,),
            None,
            (),
            (),
        ),
        CoverageRow(
            "coverage_obligation",
            CoverageUnitType.OBLIGATION,
            obligation_ref,
            CoverageResolution.RESOLVED,
            CoverageDisposition.NARRATIVE,
            (obligation_ref,),
            (event_ref,),
            None,
            (),
            (),
        ),
        CoverageRow(
            "coverage_window",
            CoverageUnitType.VLM_WINDOW,
            window_ref,
            CoverageResolution.RESOLVED,
            CoverageDisposition.SUPPORTING,
            (),
            (window_ref,),
            None,
            (),
            (),
        ),
    )
    if unrelated_universe:
        rows = (event_row,)
    canonical_rows = _canon(*rows)
    ledger = CoverageLedger.from_inputs(
        "ledger_001",
        input_unit_refs=_canon(*(item.unit_ref for item in canonical_rows)),
        rows=canonical_rows,
    )
    policy = DependencyPropagationPolicy("dependency-policy-001", "1", ("requires",))
    policy_ref = _artifact("art_dependency_policy", policy.canonical_hash)
    proof = DependencyClosureEvaluator.evaluate(
        proof_id="dependency_proof_001",
        graph_ref=graph_ref,
        graph=graph,
        ledger=ledger,
        evidence_diagnostics_ref=_artifact("art_evidence_diagnostics", evidence.canonical_hash),
        evidence_diagnostics=evidence,
        conflict_diagnostics_ref=conflict_ref,
        conflict_diagnostics=conflicts,
        policy_ref=policy_ref,
        policy=policy,
    )
    return digests, events, graph, ledger, evidence, conflicts, proof, policy


def _coverage_admission(values: Stage1Values, mode: str) -> object:
    digests, events, graph, ledger, evidence, conflicts, proof, dependency_policy = values
    pending = _pending(
        "coverage",
        ("episode_digest_set", digests),
        ("event_card_set", events),
        ("narrative_graph", graph),
        ("coverage_ledger", ledger),
        ("evidence_diagnostics", evidence),
        ("conflict_diagnostics", conflicts),
        ("dependency_closure_proof", proof),
    )
    coverage_policy = CoveragePolicy("coverage-policy-001", mode)
    return CoverageAdmissionEvaluator.evaluate(
        admission_id="coverage_admission_001",
        pending_set=pending,
        episode_digests=digests,
        event_cards=events,
        graph=graph,
        ledger=ledger,
        evidence_diagnostics=evidence,
        conflict_diagnostics=conflicts,
        dependency_proof=proof,
        coverage_policy_ref=_artifact("art_coverage_policy", coverage_policy.canonical_hash),
        coverage_policy=coverage_policy,
        dependency_policy_ref=_artifact("art_dependency_policy", dependency_policy.canonical_hash),
        dependency_policy=dependency_policy,
    )


def test_stage1_recomputes_universe_graph_scc_and_bounded_conflict_closure() -> None:
    clean = _coverage_admission(_stage1_values(), "strict_global")
    assert clean.next_action == "continue"  # type: ignore[attr-defined]
    bounded = _coverage_admission(_stage1_values(conflicted=True), "dependency_scoped")
    assert bounded.next_action == "continue"  # type: ignore[attr-defined]
    assert bounded.taint_seed_ids == ("seed_001",)  # type: ignore[attr-defined]
    strict = _coverage_admission(_stage1_values(conflicted=True), "strict_global")
    assert strict.next_action == "quarantine"  # type: ignore[attr-defined]


def test_unrelated_coverage_universe_and_self_attested_bounded_proof_are_rejected() -> None:
    with pytest.raises(ProductionModelError, match="Coverage universe"):
        _coverage_admission(_stage1_values(unrelated_universe=True), "strict_global")
    digests, events, graph, ledger, evidence, conflicts, proof, policy = _stage1_values(
        conflicted=True
    )
    forged_seed = object.__new__(TaintSeedProof)
    real_seed = proof.seed_proofs[0]
    object.__setattr__(forged_seed, "taint_seed_id", real_seed.taint_seed_id)
    object.__setattr__(forged_seed, "root_refs", real_seed.root_refs)
    object.__setattr__(forged_seed, "affected_refs", real_seed.root_refs)
    object.__setattr__(forged_seed, "frontier_refs", ())
    object.__setattr__(forged_seed, "isolation_status", "bounded")
    object.__setattr__(
        forged_seed,
        "closure_hash",
        canonical_json_hash([item.to_mapping() for item in real_seed.root_refs]),
    )
    forged = object.__new__(DependencyClosureProof)
    for name in (
        "dependency_closure_proof_id",
        "graph_ref",
        "policy_ref",
        "dependency_arcs",
        "sccs",
    ):
        object.__setattr__(forged, name, getattr(proof, name))
    object.__setattr__(forged, "seed_proofs", (forged_seed,))
    with pytest.raises(ProductionModelError, match="not recomputed"):
        _coverage_admission(
            (digests, events, graph, ledger, evidence, conflicts, forged, policy),
            "dependency_scoped",
        )
    assert not hasattr(TaintSeedProof, "evaluate")


def test_fake_source_authorization_and_all_stage2_continue_paths_fail_closed() -> None:
    with pytest.raises(ProductionModelError, match="SourceManifest"):
        SourceAuthorizationRef(_artifact("not_a_source_manifest"), "source_001", "render")
    with pytest.raises(ProductionModelError, match="full JobPolicy"):
        PortfolioCompiler.compile(
            portfolio_id="portfolio_001",
            proposal_set_ref=_artifact("proposal_set"),
            proposal_set=object(),  # type: ignore[arg-type]
            job_policy_ref=_artifact("job_policy"),
            job_policy=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ProductionModelError, match="authoritative Stage 2"):
        PortfolioAdmissionEvaluator.evaluate(
            admission_id="admission_001",
            pending_set=object(),  # type: ignore[arg-type]
            candidate_catalog=object(),
            proposal_set=object(),  # type: ignore[arg-type]
            portfolio=object(),
            source_usage_ledger=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "attack",
    ("distinct_candidates_same_source", "wrong_proposal_count", "job_policy_version_mismatch"),
)
def test_stage2_policy_mismatch_vectors_cannot_reach_portfolio_continue(attack: str) -> None:
    with pytest.raises(ProductionModelError, match="full JobPolicy"):
        PortfolioCompiler.compile(
            portfolio_id=f"portfolio_{attack}",
            proposal_set_ref=_artifact(f"proposal_set_{attack}"),
            proposal_set=object(),  # type: ignore[arg-type]
            job_policy_ref=_artifact(f"job_policy_{attack}"),
            job_policy=object(),  # type: ignore[arg-type]
        )


def _physical() -> tuple[PhysicalRequirement, ...]:
    return _canon(
        PhysicalRequirement("dialogue_integrity", "complete"),
        PhysicalRequirement("subtitle_clearance", "protect_detected_cues"),
        PhysicalRequirement("visual_validity", "endpoint_and_stable_region"),
    )


def _beat(story_id: str, partition_id: str, ordinal: int, suffix: str) -> BlueprintBeat:
    event_ref = _domain(_artifact("art_events"), "event", f"event_{suffix}")
    candidate_ref = _domain(_artifact("art_candidates"), "candidate", f"candidate_{suffix}")
    requirement = EvidenceRequirement(
        f"requirement_{suffix}",
        f"material_{suffix}",
        "one_of",
        (CandidateAlternative(f"alternative_{suffix}", (event_ref,), (candidate_ref,)),),
        _physical(),
    )
    return BlueprintBeat(
        f"beat_{suffix}",
        stable_beat_id(story_id, partition_id, ordinal),
        "setup",
        NarrativeFunction.HOOK_AND_ORIENT,
        f"Beat {suffix}.",
        (_domain(_artifact("art_graph"), "obligation", f"obligation_{suffix}"),),
        (_domain(_artifact("art_graph"), "fact", f"fact_{suffix}"),),
        (requirement,),
        (candidate_ref,),
        SpanPolicy(
            "scene", _canon_strings("context", "scene", "tight"), ("scene", "context", "tight")
        ),
        DurationRangeSeconds(1, 2, 3),
    )


def _partition(partition_id: str, beats: tuple[BlueprintBeat, ...]) -> GenerationPartition:
    obligations = _canon_strings(
        *(ref.object_id for beat in beats for ref in beat.required_obligation_refs)
    )
    requirements = _canon_strings(
        *(item.requirement_id for beat in beats for item in beat.evidence_requirements)
    )
    closures = tuple(
        sorted(
            (
                RequiredClosure(f"closure_{item.removeprefix('requirement_')}", HASH_A)
                for item in requirements
            ),
            key=lambda item: canonical_json_bytes(item.to_mapping()),
        )
    )
    return GenerationPartition(partition_id, obligations, requirements, closures, (), HASH_B, 1_000)


def _fragment(
    story_id: str,
    partition_id: str,
    beats: tuple[BlueprintBeat, ...],
    constraints: tuple[OrderingConstraint, ...],
) -> BlueprintFragment:
    return BlueprintFragment.from_normalized_beats(
        blueprint_fragment_id=f"fragment_{partition_id}",
        story_id=story_id,
        partition_id=partition_id,
        generation_invocation_ref=_artifact(f"generation_{partition_id}"),
        parse_normalization_record_ref=_artifact(f"parse_{partition_id}"),
        beats=beats,
        ordering_constraints=constraints,
    )


def _merge(
    plan: GenerationPartitionPlan,
    fragments: tuple[BlueprintFragment, ...],
    *,
    merge_policy_ref: ArtifactRef,
) -> object:
    return BlueprintMerger.merge(
        blueprint_id="blueprint_001",
        story_id=plan.story_id,
        proposal_ref=_domain(_artifact("art_proposals"), "proposal", "proposal_001"),
        partition_plan_ref=_artifact("art_partition_plan", plan.canonical_hash),
        partition_plan=plan,
        merge_policy_ref=merge_policy_ref,
        merge_policy=MergePolicy(),
        fragments=fragments,
        story_duration_seconds=DurationRangeSeconds(1, 10, 100),
        pacing="balanced",
        continuity_priority="high",
        teaser_strategy="cold_open",
        teaser_duration_seconds=DurationRangeSeconds(1, 2, 3),
    )


def test_merge_rejects_policy_ref_writer_mismatch_and_non_adjacent_beats() -> None:
    story_id, partition_id = "story_001", "partition_001"
    beats = tuple(_beat(story_id, partition_id, index, str(index)) for index in range(3))
    ordered_ids = sorted((item.stable_beat_id for item in beats), key=canonical_json_bytes)
    fragment = _fragment(
        story_id,
        partition_id,
        beats,
        (OrderingConstraint("adjacent", ordered_ids[0], ordered_ids[-1]),),
    )
    merge_policy_ref = _artifact("merge_policy", MergePolicy().canonical_hash)
    partition = _partition(partition_id, beats)
    plan = GenerationPartitionPlan(
        "plan_001", story_id, (partition_id,), (partition,), 1_000, merge_policy_ref
    )
    with pytest.raises(ProductionModelError, match="different MergePolicy refs"):
        _merge(
            plan,
            (fragment,),
            merge_policy_ref=_artifact("other_merge_policy", MergePolicy().canonical_hash),
        )
    with pytest.raises(ProductionModelError, match="not consecutive"):
        _merge(plan, (fragment,), merge_policy_ref=merge_policy_ref)
    wrong_partition = replace(
        partition, writer_requirement_ids=(partition.writer_requirement_ids[0],)
    )
    wrong_plan = GenerationPartitionPlan(
        "plan_wrong", story_id, (partition_id,), (wrong_partition,), 1_000, merge_policy_ref
    )
    with pytest.raises(ProductionModelError, match="partition writer assignment"):
        _merge(wrong_plan, (fragment,), merge_policy_ref=merge_policy_ref)


def test_cross_fragment_constraint_is_representable_and_globally_validated() -> None:
    story_id = "story_cross"
    first = _beat(story_id, "partition_a", 0, "a")
    second = _beat(story_id, "partition_b", 0, "b")
    cross = OrderingConstraint("precedes", first.stable_beat_id, second.stable_beat_id)
    fragment_a = _fragment(story_id, "partition_a", (first,), (cross,))
    fragment_b = _fragment(story_id, "partition_b", (second,), ())
    merge_policy_ref = _artifact("merge_policy", MergePolicy().canonical_hash)
    partitions = (_partition("partition_a", (first,)), _partition("partition_b", (second,)))
    plan = GenerationPartitionPlan(
        "plan_cross",
        story_id,
        ("partition_a", "partition_b"),
        partitions,
        2_000,
        merge_policy_ref,
    )
    blueprint = _merge(plan, (fragment_b, fragment_a), merge_policy_ref=merge_policy_ref)
    assert tuple(item.stable_beat_id for item in blueprint.beats) == (  # type: ignore[attr-defined]
        first.stable_beat_id,
        second.stable_beat_id,
    )


def test_metadata_only_or_fake_owner_closure_cannot_mint_semantic_continue() -> None:
    metadata = EvidenceClosureMember(
        "candidate_metadata", _artifact("art_candidates"), "candidate_001", HASH_A
    )
    with pytest.raises(ProductionModelError, match="omits mandatory"):
        EvidenceClosure("closure_001", "requirement_001", (metadata,), ())
    members = _canon(
        EvidenceClosureMember("fact", _artifact("attacker"), "fact_001", HASH_A),
        EvidenceClosureMember("event", _artifact("attacker"), "event_001", HASH_A),
        EvidenceClosureMember("vlm_observation", _artifact("attacker"), "observation_001", HASH_A),
        EvidenceClosureMember("character_state", _artifact("attacker"), "character_001", HASH_A),
        metadata,
        EvidenceClosureMember(
            "dependency_closure", _artifact("attacker"), "dependency_001", HASH_A
        ),
    )
    EvidenceClosure("closure_001", "requirement_001", members, ())
    with pytest.raises(ProductionModelError, match="authoritative Graph/Event/VLM"):
        SemanticFeasibilityEvaluator.evaluate(
            admission_id="semantic_001",
            pending_set=object(),
            blueprint=object(),  # type: ignore[arg-type]
            closure_set=object(),
            context_manifest=object(),
            partition_plan=object(),  # type: ignore[arg-type]
            proposal_set=object(),
            portfolio_ref=_artifact("portfolio"),  # type: ignore[arg-type]
            portfolio=object(),
            portfolio_admission_ref=_artifact("portfolio_admission"),  # type: ignore[arg-type]
            portfolio_admission=object(),
            source_usage_ledger_ref=_artifact("source_usage"),  # type: ignore[arg-type]
            source_usage_ledger=object(),
            candidate_catalog_ref=_artifact("candidate_catalog"),  # type: ignore[arg-type]
            candidate_catalog=object(),  # type: ignore[arg-type]
        )


def test_canonical_decimal_ordering_and_closed_wire_types() -> None:
    with pytest.raises(ProductionModelError, match="canonical decimal"):
        NarrativeConfidence("0.0", "rule")
    first, second = "sha256:" + "4" * 64, "sha256:" + "5" * 64
    with pytest.raises(ProductionModelError):
        OrderingConstraint.from_mapping(
            {
                "constraint_type": "max_gap",
                "before_beat_id": first,
                "after_beat_id": second,
                "maximum_gap": {"tick": 1.5, "time_base": {"num": 1, "den": 90_000}},
            }
        )
    with pytest.raises(ProductionModelError):
        OrderingConstraint.from_mapping(
            {
                "constraint_type": "precedes",
                "before_beat_id": first,
                "after_beat_id": second,
                "unknown": True,
            }
        )
