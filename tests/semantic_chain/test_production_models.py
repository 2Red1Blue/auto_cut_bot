from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
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
    Candidate,
    CandidateAlternative,
    CandidateCapabilityEvaluator,
    CandidateCapabilityPolicy,
    CandidateCatalog,
    CandidateMeasurementPolicy,
    CapabilityPredicate,
    CapabilityRule,
    CommittedVlmObservation,
    ConflictDiagnostics,
    ContextBudget,
    ContextManifest,
    CoverageAdmission,
    CoverageAdmissionEvaluator,
    CoverageDisposition,
    CoverageLedger,
    CoverageResolution,
    CoverageRow,
    CoverageUnitType,
    DeclaredSpan,
    DependencyClosureProof,
    DependencyProjection,
    DependencyScc,
    DiagnosticItem,
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
    EvidenceDiagnostics,
    EvidenceRequirement,
    GenerationPartition,
    GenerationPartitionPlan,
    MaterialRequirement,
    MaterialSupportEvaluator,
    MergePolicy,
    NarrativeAttributes,
    NarrativeConfidence,
    NarrativeFunction,
    NarrativeGraph,
    NarrativeNode,
    NarrativeNodeType,
    OrderingConstraint,
    OwnerBoundVlmObservationRef,
    PendingBusinessMember,
    PendingBusinessSet,
    PhysicalRequirement,
    Portfolio,
    PortfolioAdmission,
    PortfolioAdmissionEvaluator,
    PortfolioCompiler,
    PortfolioPolicy,
    ProductionModelError,
    Proposal,
    ProposalDisposition,
    ProposalSet,
    RequiredClosure,
    RuleResult,
    SemanticAnchor,
    SemanticBatchEvaluator,
    SemanticFeasibilityAdmission,
    SemanticFeasibilityEvaluator,
    SemanticMeasurement,
    SemanticStoryEvaluation,
    SourceAuthorizationRef,
    SourceRangeRef,
    SourceUsageLedger,
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


def _domain(owner: str, object_type: str, object_id: str, digest: str = HASH_A) -> DomainRef:
    return DomainRef(_artifact(owner, digest), object_type, object_id)


def _request_identity() -> VlmRequestIdentity:
    return VlmRequestIdentity(
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


def _committed_observation() -> CommittedVlmObservation:
    request = _request_identity()
    interval = MappedSourceInterval(
        coarse_range=TickRange(0, 2_700_000),
        mapping_error_bound_source_pts=0,
        source_time_base=TimeBase(1, 90_000),
        provider_uncertainty_proxy_pts=0,
        proxy_time_base=TimeBase(1, 1_000),
    )
    observation = VlmObservation(
        observation_id=OBS_ID,
        kind=VlmObservationKind.CHANGE,
        summary="A character speaks while an action changes the scene.",
        confidence=Decimal("0.95"),
        supporting_frame_ids=(FRAME_ID,),
        source_interval=interval,
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
    set_ref = _artifact("art_vlm_observations", observation_set.canonical_hash)
    return CommittedVlmObservation.from_reader(
        observation_set_ref=set_ref,
        observation_ref=DomainRef(set_ref, "vlm_observation", OBS_ID),
        source_ref=_domain("art_sources", "source", "source_001"),
        window_ref=_domain("art_windows", "vlm_window", WINDOW_HASH),
        request_identity=request,
        observation_set=observation_set,
        observation=observation,
    )


def _measurement_policy() -> tuple[ArtifactRef, CandidateMeasurementPolicy]:
    policy = CandidateMeasurementPolicy(
        "candidate-measurement-1.0.0",
        "1.0.0",
        "0.80",
        _canon_strings("hook_strength", "dialogue_salience", "action_salience"),
        _canon_strings("audited_vlm_generation", "deterministic_vlm_projection"),
    )
    return _artifact("art_measurement_policy", policy.canonical_hash), policy


def _capability_policy() -> tuple[ArtifactRef, CandidateCapabilityPolicy]:
    rules = (
        CapabilityRule(
            "cap_action_mode",
            "editing_mode",
            "action",
            (CapabilityPredicate("anchor_role_exists", anchor_role="action"),),
        ),
        CapabilityRule(
            "cap_dialogue_mode",
            "editing_mode",
            "dialogue",
            (CapabilityPredicate("anchor_role_exists", anchor_role="dialogue_semantic"),),
        ),
        CapabilityRule(
            "cap_hook_and_orient",
            "narrative_function",
            "hook_and_orient",
            (
                CapabilityPredicate(
                    "measurement_at_least",
                    measurement_kind="hook_strength",
                    threshold="0.80",
                ),
            ),
        ),
    )
    policy = CandidateCapabilityPolicy("candidate-capability-1.0.0", "1.0.0", rules)
    return _artifact("art_capability_policy", policy.canonical_hash), policy


def _authority_parts() -> tuple[
    CommittedVlmObservation,
    tuple[SemanticAnchor, ...],
    tuple[SemanticMeasurement, ...],
    OwnerBoundVlmObservationRef,
]:
    committed = _committed_observation()
    anchors = _canon(
        SemanticAnchor("semantic_center", committed.observation_ref),
        SemanticAnchor("dialogue_semantic", committed.observation_ref),
        SemanticAnchor("action", committed.observation_ref),
    )
    measurement_policy_ref, measurement_policy = _measurement_policy()
    measurement = SemanticMeasurement(
        "measurement_001",
        "hook_strength",
        "0.90",
        "0.95",
        "audited_vlm_generation",
        measurement_policy_ref,
        _artifact("art_generation_invocation"),
        (committed.observation_ref,),
    )
    measurements = (measurement,)
    capability_policy_ref, capability_policy = _capability_policy()
    authority = CandidateCapabilityEvaluator.evaluate(
        committed=committed,
        anchors=anchors,
        measurements=measurements,
        measurement_policy_ref=measurement_policy_ref,
        measurement_policy=measurement_policy,
        capability_policy_ref=capability_policy_ref,
        capability_policy=capability_policy,
    )
    return committed, anchors, measurements, authority


def _candidate() -> Candidate:
    committed, anchors, measurements, authority = _authority_parts()
    return Candidate.from_evaluation(
        candidate_id="cand_001",
        event_refs=(_domain("art_events", "event", "event_001"),),
        committed=committed,
        authority=authority,
        declared_spans=(
            DeclaredSpan(
                "source_001:video_pts",
                TimeBaseValue(1, 90_000),
                0,
                2_700_000,
            ),
        ),
        anchors=anchors,
        measurements=measurements,
        authorization_ref=SourceAuthorizationRef(
            _artifact("art_sources"), "source_001", "render"
        ),
    )


def _physical() -> tuple[PhysicalRequirement, ...]:
    return _canon(
        PhysicalRequirement("dialogue_integrity", "complete"),
        PhysicalRequirement("subtitle_clearance", "protect_detected_cues"),
        PhysicalRequirement("visual_validity", "endpoint_and_stable_region"),
    )


def _material(requirement_id: str) -> MaterialRequirement:
    return MaterialRequirement(
        requirement_id,
        _domain("art_graph", "obligation", "obl_001"),
        12,
        _physical(),
        (),
        (),
    )


def _stage2() -> tuple[
    CandidateCatalog,
    ProposalSet,
    Portfolio,
    SourceUsageLedger,
    PortfolioAdmission,
]:
    candidate = _candidate()
    catalog = CandidateCatalog("candidates_001", (candidate,))
    catalog_ref = _artifact("art_candidate_catalog", catalog.canonical_hash)
    requirements = _canon(_material("mr_001"), _material("mr_002"))
    support = MaterialSupportEvaluator.evaluate(
        requirements=requirements,
        candidate_catalog_ref=catalog_ref,
        candidate_catalog=catalog,
    )
    proposal = Proposal(
        "proposal_001",
        "story_001",
        "Owner-bound story",
        "The committed event supports a closed Story.",
        (_domain("art_graph", "story_thread", "thread_001"),),
        (_domain("art_graph", "obligation", "obl_001"),),
        (_domain("art_graph", "fact", "fact_001"),),
        (),
        ("suspense",),
        "dramatic_short",
        DurationRangeSeconds(30, 45, 60),
        "cold_open",
        requirements,
        support,
        DependencyProjection(
            _canon(
                _domain("art_graph", "story_thread", "thread_001"),
                _domain("art_graph", "obligation", "obl_001"),
                _domain("art_graph", "fact", "fact_001"),
            ),
            (),
            (),
        ),
        (),
    )
    proposal_set = ProposalSet(
        "proposals_001",
        "job-policy-1.0.0",
        (proposal,),
        (ProposalDisposition("proposal_001", "accepted", ()),),
    )
    proposal_ref = _artifact("art_proposal_set", proposal_set.canonical_hash)
    policy = PortfolioPolicy("portfolio-policy-001", 1, "all_or_nothing")
    policy_ref = _artifact("art_job_policy", policy.canonical_hash)
    hard_rules = _rules({"SD-HARD-001"}, proposal.canonical_hash)
    portfolio = PortfolioCompiler.compile(
        portfolio_id="portfolio_001",
        proposal_set_ref=proposal_ref,
        proposal_set=proposal_set,
        job_policy_ref=policy_ref,
        job_policy=policy,
        hard_constraint_results=((proposal.proposal_id, hard_rules),),
    )
    usage = SourceUsageLedger.for_portfolio("usage_001", portfolio)
    pending = _pending(
        "portfolio",
        ("candidate_catalog", catalog),
        ("proposal_set", proposal_set),
        ("portfolio", portfolio),
        ("source_usage_ledger", usage),
    )
    admission = PortfolioAdmissionEvaluator.evaluate(
        admission_id="adm_portfolio_001",
        pending_set=pending,
        candidate_catalog=catalog,
        proposal_set=proposal_set,
        portfolio=portfolio,
        source_usage_ledger=usage,
    )
    return catalog, proposal_set, portfolio, usage, admission


def _rules(rule_ids: set[str], subject_hash: str) -> tuple[RuleResult, ...]:
    return _canon(*(RuleResult(rule_id, "pass", subject_hash) for rule_id in rule_ids))


def _pending(
    admission_kind: str, *values: tuple[str, object], pending_set_id: str = "pending_001"
) -> PendingBusinessSet:
    members = tuple(
        PendingBusinessMember(
            artifact_type,
            _artifact(f"art_{artifact_type}", value.canonical_hash),  # type: ignore[attr-defined]
        )
        for artifact_type, value in values
    )
    return PendingBusinessSet(pending_set_id, admission_kind, _canon(*members))


def _stage1_clean() -> tuple[
    EpisodeDigestSet,
    EventCardSet,
    NarrativeGraph,
    CoverageLedger,
    EvidenceDiagnostics,
    ConflictDiagnostics,
    DependencyClosureProof,
]:
    committed = _committed_observation()
    digest = EpisodeDigest(
        "episode_001",
        1,
        "A committed episode.",
        (committed.window_ref,),
        (committed.observation_ref,),
    )
    digests = EpisodeDigestSet("digests_001", (digest,))
    event = EventCard(
        "event_001",
        "episode_001",
        "A fact-layer event occurred.",
        (
            SourceRangeRef(
                "source_001",
                "source_001:video_pts",
                TimeBaseValue(1, 90_000),
                0,
                2_700_000,
                _artifact("art_sources"),
            ),
        ),
        (committed.observation_ref,),
    )
    events = EventCardSet("events_001", (event,))
    event_ref = _artifact("art_event_card_set", events.canonical_hash)
    attributes = NarrativeAttributes.from_mapping(
        {
            "attribute_type": "event",
            "episode_id": "episode_001",
            "event_card_ref": DomainRef(event_ref, "event", "event_001").to_mapping(),
            "source_range_refs": [
                DomainRef(event_ref, "source_range", "event_001:range:0").to_mapping()
            ],
            "summary": "A fact-layer event occurred.",
        }
    )
    node = NarrativeNode(
        "event_001",
        NarrativeNodeType.EVENT,
        "Event",
        attributes,
        (DomainRef(event_ref, "event", "event_001"),),
        NarrativeConfidence("0.95", "model"),
    )
    graph = NarrativeGraph("graph_001", (node,), ())
    unit_ref = DomainRef(event_ref, "event", "event_001")
    graph_node_ref = _domain("art_graph", "event", "event_001")
    row = CoverageRow(
        "cov_001",
        CoverageUnitType.EVENT,
        unit_ref,
        CoverageResolution.RESOLVED,
        CoverageDisposition.NARRATIVE,
        (graph_node_ref,),
        (committed.observation_ref,),
        None,
        (),
        (),
    )
    ledger = CoverageLedger.from_inputs(
        "ledger_001", input_unit_refs=(unit_ref,), rows=(row,)
    )
    evidence = EvidenceDiagnostics("evidence_diag_001", ())
    conflicts = ConflictDiagnostics("conflict_diag_001", ())
    graph_ref = _artifact("art_narrative_graph", graph.canonical_hash)
    scc = DependencyScc.from_nodes((graph_node_ref,), ())
    proof = DependencyClosureProof(
        "dep_proof_001", graph_ref, _artifact("art_dep_policy"), (), (scc,), ()
    )
    return digests, events, graph, ledger, evidence, conflicts, proof


def _semantic_fixture() -> tuple[
    EditorialBlueprint,
    EvidenceClosureSet,
    ContextManifest,
    GenerationPartitionPlan,
]:
    catalog, proposal_set, portfolio, _, _ = _stage2()
    proposal_ref = DomainRef(portfolio.proposal_set_ref, "proposal", "proposal_001")
    candidate_ref = DomainRef(
        _artifact("art_candidate_catalog", catalog.canonical_hash), "candidate", "cand_001"
    )
    event_ref = catalog.candidates[0].event_refs[0]
    requirement_1 = EvidenceRequirement(
        "er_001",
        "mr_001",
        "one_of",
        (CandidateAlternative("alt_001", (event_ref,), (candidate_ref,)),),
        _physical(),
    )
    requirement_2 = EvidenceRequirement(
        "er_002",
        "mr_002",
        "one_of",
        (CandidateAlternative("alt_002", (event_ref,), (candidate_ref,)),),
        _physical(),
    )
    story_id = "story_001"
    partition_id = "part_001"
    beat_1 = BlueprintBeat(
        "bpbeat_001",
        stable_beat_id(story_id, partition_id, 0),
        "setup",
        NarrativeFunction.HOOK_AND_ORIENT,
        "Orient the audience.",
        (_domain("art_graph", "obligation", "obl_001"),),
        (_domain("art_graph", "fact", "fact_001"),),
        (requirement_1,),
        (candidate_ref,),
        SpanPolicy("scene", ("context", "scene", "tight"), ("scene", "context", "tight")),
        DurationRangeSeconds(10, 12, 15),
    )
    beat_2 = BlueprintBeat(
        "bpbeat_002",
        stable_beat_id(story_id, partition_id, 1),
        "payoff",
        NarrativeFunction.HOOK_AND_ORIENT,
        "Pay off the committed event.",
        (_domain("art_graph", "obligation", "obl_001"),),
        (_domain("art_graph", "fact", "fact_001"),),
        (requirement_2,),
        (candidate_ref,),
        SpanPolicy("scene", ("context", "scene", "tight"), ("scene", "context", "tight")),
        DurationRangeSeconds(10, 12, 15),
    )
    constraint = OrderingConstraint(
        "precedes", beat_1.stable_beat_id, beat_2.stable_beat_id
    )
    fragment = BlueprintFragment.from_normalized_beats(
        blueprint_fragment_id="fragment_001",
        story_id=story_id,
        partition_id=partition_id,
        generation_invocation_ref=_artifact("art_generation_invocation"),
        parse_normalization_record_ref=_artifact("art_parse_record"),
        beats=(beat_1, beat_2),
        ordering_constraints=(constraint,),
    )
    member = EvidenceClosureMember(
        "candidate_metadata",
        _artifact("art_candidate_catalog", catalog.canonical_hash),
        "cand_001",
        catalog.candidates[0].canonical_hash,
    )
    closure_1 = EvidenceClosure("closure_001", "er_001", (member,), ())
    closure_2 = EvidenceClosure("closure_002", "er_002", (member,), ())
    closure_set = EvidenceClosureSet(
        "closures_001", story_id, _canon(closure_1, closure_2)
    )
    merge_policy = MergePolicy()
    merge_policy_ref = _artifact("art_merge_policy", merge_policy.canonical_hash)
    partition = GenerationPartition(
        partition_id,
        ("obl_001",),
        ("er_001", "er_002"),
        _canon(
            RequiredClosure("closure_001", closure_1.closure_hash),
            RequiredClosure("closure_002", closure_2.closure_hash),
        ),
        (),
        HASH_A,
        5_000,
    )
    plan = GenerationPartitionPlan(
        "plan_001", story_id, (partition_id,), (partition,), 5_000, merge_policy_ref
    )
    plan_ref = _artifact("art_generation_partition_plan", plan.canonical_hash)
    blueprint = BlueprintMerger.merge(
        blueprint_id="blueprint_001",
        story_id=story_id,
        proposal_ref=proposal_ref,
        partition_plan_ref=plan_ref,
        partition_plan=plan,
        merge_policy_ref=merge_policy_ref,
        merge_policy=merge_policy,
        fragments=(fragment,),
        story_duration_seconds=DurationRangeSeconds(30, 45, 60),
        pacing="balanced",
        continuity_priority="high",
        teaser_strategy="cold_open",
        teaser_duration_seconds=DurationRangeSeconds(5, 8, 10),
    )
    closure_ref = _artifact("art_evidence_closure_set", closure_set.canonical_hash)
    context = ContextManifest.for_closure_set(
        context_manifest_id="context_001",
        story_id=story_id,
        input_refs=_canon(_artifact("art_graph"), _artifact("art_candidates")),
        evidence_closure_set_ref=closure_ref,
        closure_set=closure_set,
        optional_context_refs=(),
        omissions=(),
        budget=ContextBudget("tokens", 10_000, 5_000, "tokenizer", "1.0.0"),
        builder_version="context-builder-2.1.3",
    )
    assert proposal_set.proposals[0].story_id == blueprint.story_id
    return blueprint, closure_set, context, plan


def test_vlm_authority_is_reader_and_evaluator_derived_with_mixed_modes() -> None:
    committed, _, _, authority = _authority_parts()

    assert authority.vlm_observation_sha256 == canonical_json_hash(
        committed.observation.to_mapping()
    )
    assert authority.editing_modes == (EditingMode.DIALOGUE, EditingMode.ACTION)
    assert authority.supported_narrative_functions == (NarrativeFunction.HOOK_AND_ORIENT,)

    with pytest.raises(TypeError):
        OwnerBoundVlmObservationRef(  # type: ignore[call-arg]
            committed.observation_ref,
            HASH_D,
            committed.source_ref,
            committed.window_ref,
            authority.capability_assessment,
        )
    with pytest.raises(ProductionModelError, match="exact committed set"):
        CommittedVlmObservation.from_reader(
            observation_set_ref=_artifact("art_vlm_observations", HASH_D),
            observation_ref=committed.observation_ref,
            source_ref=committed.source_ref,
            window_ref=committed.window_ref,
            request_identity=committed.request_identity,
            observation_set=committed.observation_set,
            observation=committed.observation,
        )


def test_capability_evaluator_rejects_unrelated_measurement_provenance() -> None:
    committed, anchors, measurements, _ = _authority_parts()
    measurement_ref, measurement_policy = _measurement_policy()
    capability_ref, capability_policy = _capability_policy()
    forged = replace(
        measurements[0], evidence_refs=(_domain("art_vlm", "vlm_observation", "other"),)
    )

    with pytest.raises(ProductionModelError, match="exact committed observation"):
        CandidateCapabilityEvaluator.evaluate(
            committed=committed,
            anchors=anchors,
            measurements=(forged,),
            measurement_policy_ref=measurement_ref,
            measurement_policy=measurement_policy,
            capability_policy_ref=capability_ref,
            capability_policy=capability_policy,
        )


def test_candidate_contains_essential_capability_provenance_and_exact_duration_proof() -> None:
    candidate = _candidate()

    assert candidate.anchor_refs
    assert candidate.semantic_measurements
    assert candidate.duration_proof.total_ticks == 2_700_000
    assert candidate.duration_proof.supports_seconds(30)
    assert not candidate.duration_proof.supports_seconds(31)
    assert candidate.to_mapping()["vlm_observation_sha256"] == canonical_json_hash(
        _committed_observation().observation.to_mapping()
    )


def test_stage1_fact_layer_attributes_and_diagnostics_are_closed() -> None:
    _, events, graph, _, evidence, conflicts, proof = _stage1_clean()

    event_mapping = events.events[0].to_mapping()
    assert "source_range_refs" in event_mapping
    assert "editing_modes" not in event_mapping
    assert graph.nodes[0].attributes.to_mapping()["event_card_ref"]
    assert evidence.items == ()
    assert conflicts.items == ()
    assert proof.arc_set_hash == canonical_json_hash([])

    invalid = graph.nodes[0].attributes.to_mapping()
    invalid["hook_strength"] = "0.9"
    with pytest.raises(ProductionModelError):
        NarrativeAttributes.from_mapping(invalid)


def test_coverage_admission_binds_exact_set_and_unresolved_never_continues() -> None:
    digests, events, graph, ledger, evidence, conflicts, proof = _stage1_clean()
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
    clean = CoverageAdmissionEvaluator.evaluate(
        admission_id="adm_coverage_001",
        pending_set=pending,
        episode_digests=digests,
        event_cards=events,
        graph=graph,
        ledger=ledger,
        evidence_diagnostics=evidence,
        conflict_diagnostics=conflicts,
        dependency_proof=proof,
        coverage_mode="strict_global",
    )
    assert clean.next_action == "continue"

    event_ref = ledger.rows[0].unit_ref
    seed_ref = _domain("art_dep_proof", "taint_seed", "taint_001")
    unresolved_row = CoverageRow(
        "cov_001",
        CoverageUnitType.EVENT,
        event_ref,
        CoverageResolution.UNRESOLVED,
        CoverageDisposition.UNASSIGNED,
        (),
        ledger.rows[0].evidence_refs,
        None,
        (),
        (seed_ref,),
    )
    unresolved_ledger = CoverageLedger.from_inputs(
        "ledger_unresolved", input_unit_refs=(event_ref,), rows=(unresolved_row,)
    )
    seed = TaintSeedProof.evaluate(
        taint_seed_id="taint_001",
        root_refs=(event_ref,),
        affected_refs=(event_ref,),
        frontier_refs=(),
        isolation_status="bounded",
    )
    unresolved_proof = DependencyClosureProof(
        "dep_proof_unresolved",
        proof.graph_ref,
        proof.policy_ref,
        (),
        proof.sccs,
        (seed,),
    )
    unresolved_pending = _pending(
        "coverage",
        ("episode_digest_set", digests),
        ("event_card_set", events),
        ("narrative_graph", graph),
        ("coverage_ledger", unresolved_ledger),
        ("evidence_diagnostics", evidence),
        ("conflict_diagnostics", conflicts),
        ("dependency_closure_proof", unresolved_proof),
        pending_set_id="pending_unresolved",
    )
    admission = CoverageAdmissionEvaluator.evaluate(
        admission_id="adm_coverage_unresolved",
        pending_set=unresolved_pending,
        episode_digests=digests,
        event_cards=events,
        graph=graph,
        ledger=unresolved_ledger,
        evidence_diagnostics=evidence,
        conflict_diagnostics=conflicts,
        dependency_proof=unresolved_proof,
        coverage_mode="dependency_scoped",
    )
    assert admission.next_action == "quarantine"
    with pytest.raises(TypeError):
        CoverageAdmission()  # type: ignore[call-arg]


def test_portfolio_compiler_and_admission_reject_unrelated_sets_and_incomplete_joins() -> None:
    catalog, proposal_set, portfolio, usage, admission = _stage2()

    assert portfolio.selection_records[0].proposal_index == 0
    assert admission.target_story_ids == ("story_001",)
    unrelated_proposals = replace(proposal_set, proposal_set_id="proposals_unrelated")
    unrelated = _pending(
        "portfolio",
        ("candidate_catalog", catalog),
        ("proposal_set", unrelated_proposals),
        ("portfolio", portfolio),
        ("source_usage_ledger", usage),
        pending_set_id="pending_unrelated",
    )
    with pytest.raises(ProductionModelError, match="exact pending ProposalSet"):
        PortfolioAdmissionEvaluator.evaluate(
            admission_id="adm_forged",
            pending_set=unrelated,
            candidate_catalog=catalog,
            proposal_set=unrelated_proposals,
            portfolio=portfolio,
            source_usage_ledger=usage,
        )
    with pytest.raises(TypeError):
        Portfolio()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        PortfolioAdmission()  # type: ignore[call-arg]


def test_ordering_variants_are_closed_and_merge_rejects_cycles() -> None:
    blueprint, _, _, plan = _semantic_fixture()
    first, second = (item.stable_beat_id for item in blueprint.beats)
    mappings = (
        {"constraint_type": "precedes", "before_beat_id": first, "after_beat_id": second},
        {"constraint_type": "adjacent", "first_beat_id": first, "second_beat_id": second},
        {
            "constraint_type": "max_gap",
            "before_beat_id": first,
            "after_beat_id": second,
            "maximum_gap": {"tick": 90_000, "time_base": {"num": 1, "den": 90_000}},
        },
    )
    assert [OrderingConstraint.from_mapping(item).constraint_type for item in mappings] == [
        "precedes",
        "adjacent",
        "max_gap",
    ]
    with pytest.raises(ProductionModelError):
        OrderingConstraint.from_mapping({**mappings[0], "unknown": True})
    with pytest.raises(ProductionModelError):
        OrderingConstraint.from_mapping(
            {"constraint_type": "max_gap", "before_beat_id": first, "after_beat_id": second}
        )

    fragment = blueprint.fragments[0]
    cycle_fragment = BlueprintFragment.from_normalized_beats(
        blueprint_fragment_id="fragment_cycle",
        story_id=blueprint.story_id,
        partition_id=fragment.partition_id,
        generation_invocation_ref=fragment.generation_invocation_ref,
        parse_normalization_record_ref=fragment.parse_normalization_record_ref,
        beats=tuple(item.normalized_beat for item in fragment.beats),
        ordering_constraints=(
            OrderingConstraint("precedes", first, second),
            OrderingConstraint("precedes", second, first),
        ),
    )
    merge_policy = MergePolicy()
    with pytest.raises(ProductionModelError, match="cycle"):
        BlueprintMerger.merge(
            blueprint_id="blueprint_cycle",
            story_id=blueprint.story_id,
            proposal_ref=blueprint.proposal_ref,
            partition_plan_ref=blueprint.generation_partition_plan_ref,
            partition_plan=plan,
            merge_policy_ref=blueprint.merge_policy_ref,
            merge_policy=merge_policy,
            fragments=(cycle_fragment,),
            story_duration_seconds=blueprint.story_duration_seconds,
            pacing=blueprint.pacing,
            continuity_priority=blueprint.continuity_priority,
            teaser_strategy=blueprint.teaser_strategy,
            teaser_duration_seconds=blueprint.teaser_duration_seconds,
        )


def test_context_projection_and_semantic_admission_join_every_authority() -> None:
    catalog, proposal_set, portfolio, usage, portfolio_admission = _stage2()
    blueprint, closure_set, context, plan = _semantic_fixture()
    pending = _pending(
        "semantic_feasibility",
        ("editorial_blueprint", blueprint),
        ("evidence_closure_set", closure_set),
        ("context_manifest", context),
        ("generation_partition_plan", plan),
    )
    admission = SemanticFeasibilityEvaluator.evaluate(
        admission_id="adm_semantic_001",
        pending_set=pending,
        blueprint=blueprint,
        closure_set=closure_set,
        context_manifest=context,
        partition_plan=plan,
        proposal_set=proposal_set,
        portfolio_ref=_artifact("art_portfolio", portfolio.canonical_hash),
        portfolio=portfolio,
        portfolio_admission=portfolio_admission,
        source_usage_ledger=usage,
        candidate_catalog=catalog,
    )
    assert admission.next_action == "continue"
    assert context.required_closures == tuple(
        RequiredClosure(item.closure_id, item.closure_hash) for item in closure_set.closures
    )
    result = SemanticStoryEvaluation("story_001", admission)
    assert SemanticBatchEvaluator.require_complete(portfolio, (result,)) == (result,)
    with pytest.raises(TypeError):
        SemanticFeasibilityAdmission()  # type: ignore[call-arg]

    incomplete_closure = EvidenceClosureSet(
        "closures_incomplete", closure_set.story_id, (closure_set.closures[0],)
    )
    incomplete_context = ContextManifest.for_closure_set(
        context_manifest_id="context_incomplete",
        story_id=blueprint.story_id,
        input_refs=context.input_refs,
        evidence_closure_set_ref=_artifact(
            "art_evidence_closure_set", incomplete_closure.canonical_hash
        ),
        closure_set=incomplete_closure,
        optional_context_refs=(),
        omissions=(),
        budget=context.budget,
        builder_version=context.builder_version,
    )
    incomplete_pending = _pending(
        "semantic_feasibility",
        ("editorial_blueprint", blueprint),
        ("evidence_closure_set", incomplete_closure),
        ("context_manifest", incomplete_context),
        ("generation_partition_plan", plan),
        pending_set_id="pending_incomplete",
    )
    with pytest.raises(ProductionModelError, match="exactly cover"):
        SemanticFeasibilityEvaluator.evaluate(
            admission_id="adm_incomplete",
            pending_set=incomplete_pending,
            blueprint=blueprint,
            closure_set=incomplete_closure,
            context_manifest=incomplete_context,
            partition_plan=plan,
            proposal_set=proposal_set,
            portfolio_ref=_artifact("art_portfolio", portfolio.canonical_hash),
            portfolio=portfolio,
            portfolio_admission=portfolio_admission,
            source_usage_ledger=usage,
            candidate_catalog=catalog,
        )


def test_canonical_set_order_uses_jcs_bytes_and_values_are_immutable() -> None:
    source_a = _domain("art_sources", "source", "source_a")
    source_b = _domain("art_sources", "source", "source_b")
    canonical = _canon(source_a, source_b)
    noncanonical = tuple(reversed(canonical))
    if noncanonical != canonical:
        with pytest.raises(ProductionModelError, match="JCS-byte order"):
            MaterialRequirement(
                "mr_noncanonical",
                _domain("art_graph", "obligation", "obl_001"),
                1,
                _physical(),
                noncanonical,
                (),
            )
    with pytest.raises(ProductionModelError, match="overlap"):
        MaterialRequirement(
            "mr_overlap",
            _domain("art_graph", "obligation", "obl_001"),
            1,
            _physical(),
            (source_a,),
            (source_a,),
        )
    candidate = _candidate()
    with pytest.raises(FrozenInstanceError):
        candidate.candidate_id = "forged"  # type: ignore[misc]


def test_unknown_narrative_function_and_binary_float_fail_closed() -> None:
    with pytest.raises(ValueError):
        NarrativeFunction("payoff")
    first = "sha256:" + "4" * 64
    second = "sha256:" + "5" * 64
    with pytest.raises(ProductionModelError):
        OrderingConstraint.from_mapping(
            {
                "constraint_type": "max_gap",
                "before_beat_id": first,
                "after_beat_id": second,
                "maximum_gap": {"tick": 1.5, "time_base": {"num": 1, "den": 90_000}},
            }
        )


def test_conflict_diagnostic_requires_two_claims() -> None:
    with pytest.raises(ProductionModelError, match="at least two"):
        DiagnosticItem(
            "diag_001",
            "fact_value_conflict",
            "error",
            _domain("art_graph", "fact", "fact_001"),
            "KC-COV-004",
            "Two claims conflict.",
            (),
            (_domain("art_graph", "fact", "fact_001"),),
            (_domain("art_conflicts", "claim", "claim_001"),),
        )
