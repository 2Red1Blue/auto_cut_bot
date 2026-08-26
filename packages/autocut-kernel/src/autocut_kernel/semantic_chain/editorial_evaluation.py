"""Independent Stage 3 business evaluation from raw draft and actual evidence.

This repeats the registered context, projection, material and timing algorithms
over the audited predecessors. It neither reads Store nor fabricates input,
request-audit or whole-provider-body checks. Invalid declared evidence is a
causal ValueError, never a filtered candidate pool or an implicit rule pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from ..contracts.compiler.canonical import sha256_bytes
from ..store.models import ArtifactMember, CommittedSemanticInputs
from .editorial_admission import EditorialCheck, EditorialStoryDecision
from .editorial_blueprint import EditorialBlueprint, project_editorial_blueprints
from .editorial_command_policy import Stage3CommandPolicy
from .editorial_context import EditorialContextBatch, build_editorial_contexts
from .editorial_draft import decode_editorial_draft
from .editorial_feasibility import (
    EditorialFeasibilityResult,
    editorial_material_requirements,
    evaluate_editorial_feasibility,
    verify_editorial_feasibility,
)
from .editorial_material_search import (
    MaterialSearchRequirement,
    verify_editorial_material_assignment,
)
from .editorial_members import (
    EditorialBusinessValues,
    compose_editorial_business_members,
    decode_editorial_business_members,
)
from .editorial_timing import verify_editorial_timing
from .member_refs import SemanticObjectRef
from .stage1_result import Stage1Values
from .story_design_command_policy import Stage2CommandPolicy
from .story_design_result import StoryDesignValues


@dataclass(frozen=True, slots=True)
class EditorialBusinessEvaluation:
    contexts: EditorialContextBatch
    expected_business: EditorialBusinessValues
    raw_draft_sha256: str
    canonical_draft_sha256: str
    feasibility: EditorialFeasibilityResult
    batch_checks: tuple[EditorialCheck, ...]
    story_checks: tuple[EditorialStoryDecision, ...]


def _check(rule: str, condition: bool, reason: str) -> EditorialCheck:
    return EditorialCheck(rule, "pass" if condition else "fail", () if condition else (reason,))


def _local_coverage(requirement: MaterialSearchRequirement) -> bool:
    # A complete alternative can select its entire nonempty pool. Same-Story
    # reuse is legal, so union of *already full-Event edges* is sufficient.
    # This never unions time fragments or assigns cross-Story Source ownership.
    alternatives = tuple(
        set(alternative.required_event_keys) <= {event for candidate in alternative.candidates for event in candidate.event_keys}
        for alternative in requirement.alternatives
    )
    return all(alternatives) if requirement.satisfaction == "all_of" else any(alternatives)


def _references(blueprint: EditorialBlueprint) -> tuple[SemanticObjectRef, ...]:
    return tuple(ref for beat in blueprint.beats for ref in (
        *beat.required_obligation_refs, *beat.required_fact_refs,
        *(ref for row in beat.evidence_requirements for ref in (
            row.obligation_ref, *row.required_fact_refs,
            *(ref for alt in row.alternatives for ref in (*alt.event_refs, *alt.candidate_refs)),
        )),
    ))


def _story_checks(
    actual: EditorialBlueprint, expected: EditorialBlueprint, *, locally_supported: bool, timing_supported: bool,
) -> EditorialStoryDecision:
    # Each comparison binds the actual persisted declaration to the independently
    # parsed/rebuilt raw declaration whose owners, semantics and proofs were just
    # checked. No check is filled from a producer status or a default-pass map.
    a, e = actual.beats, expected.beats
    refs_equal = _references(actual) == _references(expected)
    # Explicit typed local functions avoid passing untyped dictionaries through
    # the evaluator; the closed Blueprint owns validation of each field shape.
    return EditorialStoryDecision(actual.story_id, (
        _check("SS-REF-001", actual.proposal_ref == expected.proposal_ref and refs_equal, "raw_reference_mismatch"),
        _check("SS-ENUM-001", tuple((b.narrative_role, b.narrative_function) for b in a)
               == tuple((b.narrative_role, b.narrative_function) for b in e)
               and actual.editing_intent == expected.editing_intent and actual.teaser_intent == expected.teaser_intent,
               "raw_intent_mismatch"),
        _check("SS-OBL-001", tuple((b.required_obligation_refs, b.required_fact_refs) for b in a)
               == tuple((b.required_obligation_refs, b.required_fact_refs) for b in e), "raw_required_content_mismatch"),
        _check("SS-EV-001", locally_supported and tuple(b.evidence_requirements for b in a)
               == tuple(b.evidence_requirements for b in e), "complete_event_assignment_not_supported"),
        _check("SS-EV-002", tuple((row.material_requirement_id, row.satisfaction, row.alternatives) for b in a for row in b.evidence_requirements)
               == tuple((row.material_requirement_id, row.satisfaction, row.alternatives) for b in e for row in b.evidence_requirements),
               "raw_evidence_alternatives_mismatch"),
        _check("SS-CAND-CAP-001", tuple((b.narrative_function, tuple(row.alternatives for row in b.evidence_requirements)) for b in a)
               == tuple((b.narrative_function, tuple(row.alternatives for row in b.evidence_requirements)) for b in e),
               "raw_candidate_capability_mismatch"),
        _check("SS-PHYS-DEFER-001", tuple((row.material_requirement_id, row.minimum_usable_seconds, row.physical_requirements,
                                          row.physical_requirements_hash, row.source_constraints) for b in a for row in b.evidence_requirements)
               == tuple((row.material_requirement_id, row.minimum_usable_seconds, row.physical_requirements,
                         row.physical_requirements_hash, row.source_constraints) for b in e for row in b.evidence_requirements),
               "frozen_physical_requirements_mismatch"),
        _check("SS-PREF-001", tuple(b.candidate_preferences for b in a) == tuple(b.candidate_preferences for b in e),
               "raw_candidate_preferences_mismatch"),
        _check("SS-SPAN-001", tuple(b.span_policy for b in a) == tuple(b.span_policy for b in e), "raw_span_intent_mismatch"),
        _check("SS-CTX-001", actual.proposal_ref == expected.proposal_ref and refs_equal, "frozen_context_reference_mismatch"),
        _check("SS-CTX-002", refs_equal, "actual_raw_owner_reference_mismatch"),
        _check("SS-HASH-001", actual == expected, "raw_blueprint_content_mismatch"),
        _check("SS-DUR-002", timing_supported and actual.story_duration_seconds == expected.story_duration_seconds
               and tuple(b.duration_seconds for b in a) == tuple(b.duration_seconds for b in e)
               and actual.ordering_constraints == expected.ordering_constraints, "joint_rational_intent_not_supported"),
        _check("SS-TAINT-001", refs_equal, "actual_material_taint_binding_mismatch"),
    ))


def evaluate_editorial_business_members(
    semantic: CommittedSemanticInputs, stage1: Stage1Values, stage2: StoryDesignValues, raw_draft: bytes, *,
    members: tuple[ArtifactMember, ...], command_policy: Stage3CommandPolicy, stage2_policy: Stage2CommandPolicy,
) -> EditorialBusinessEvaluation:
    """Recompute business truth, not receipt/attempt/provider audit authority.

Malformed members, invalid declared evidence or incomplete predecessor closure
raise ValueError. Schema-valid rewrites are compared with actual raw output and
produce explicit non-pass checks. Exhausted search is never local infeasibility.
"""
    if (type(semantic) is not CommittedSemanticInputs or type(stage1) is not Stage1Values  # noqa: E721
            or type(stage2) is not StoryDesignValues or type(command_policy) is not Stage3CommandPolicy  # noqa: E721
            or type(stage2_policy) is not Stage2CommandPolicy or type(raw_draft) is not bytes):  # noqa: E721
        raise ValueError("Stage 3 evaluation requires exact raw/predecessor/policy values")
    if stage2.admission.draft_policy_sha256 != stage2_policy.draft_policy.canonical_hash:
        raise ValueError("Stage 3 predecessor draft policy differs from frozen Stage 2 policy")
    contexts = build_editorial_contexts(
        semantic, stage1, stage2, policy=command_policy.context_policy, scope=semantic.source_manifest.reference.scope,
        revision=command_policy.artifact_revision, job_policy=stage2_policy.job_policy,
        story_policy=stage2_policy.story_policy, candidate_policy=stage2_policy.candidate_policy,
    )
    draft = decode_editorial_draft(raw_draft, expected_input_binding_sha256=contexts.input_binding_sha256,
                                   expected_target_story_ids=contexts.target_story_ids, policy=command_policy.draft_policy)
    projection = project_editorial_blueprints(stage1, stage2, draft, expected_input_binding_sha256=contexts.input_binding_sha256,
                                              strategy_version=command_policy.blueprint_strategy_version)
    expected = compose_editorial_business_members(contexts, projection)
    actual = decode_editorial_business_members(members, contexts=contexts)
    requirements = editorial_material_requirements(stage1, stage2, projection, semantic=semantic,
        job_policy=stage2_policy.job_policy, policy=command_policy.feasibility_policy)
    feasibility = evaluate_editorial_feasibility(stage1, stage2, projection, semantic=semantic,
        job_policy=stage2_policy.job_policy, policy=command_policy.feasibility_policy)
    if feasibility.status == "feasible":
        verify_editorial_feasibility(stage1, stage2, projection, feasibility, semantic=semantic,
            job_policy=stage2_policy.job_policy, policy=command_policy.feasibility_policy)
    local = {story.story_id: all(_local_coverage(row) for row in requirements if row.story_id == story.story_id)
             for story in projection.blueprints}
    search = feasibility.material_search
    if search.status == "feasible":
        verify_editorial_material_assignment(requirements, search.choices,
            source_reuse=cast(Literal["allow", "forbid"], stage2_policy.job_policy.source_reuse_policy))
        reuse = EditorialCheck("SS-REUSE-001", "pass", ())
    elif search.status == "infeasible" and all(local.values()):
        reuse = EditorialCheck("SS-REUSE-001", "fail", ("joint_source_assignment_conflict",))
    else:
        reuse = EditorialCheck("SS-REUSE-001", "indeterminate", ("joint_source_assignment_not_proven",))
    search_check = EditorialCheck("SS-SEARCH-001",
        "pass" if search.status == "feasible" else "fail" if search.status == "infeasible" else "indeterminate",
        () if search.status == "feasible" else ("material_search_infeasible",) if search.status == "infeasible" else ("material_search_budget_exhausted",))
    decisions: list[EditorialStoryDecision] = []
    for actual_story, expected_story, timing in zip(actual.projection.blueprints, projection.blueprints,
                                                    feasibility.timing_witnesses, strict=True):
        if timing.durations is not None:
            verify_editorial_timing(tuple(beat.duration_seconds for beat in expected_story.beats),
                expected_story.story_duration_seconds, expected_story.ordering_constraints, timing.durations)
        decisions.append(_story_checks(actual_story, expected_story, locally_supported=local[expected_story.story_id],
                                       timing_supported=timing.durations is not None))
    return EditorialBusinessEvaluation(contexts, expected, sha256_bytes(raw_draft), draft.canonical_hash, feasibility,
        (EditorialCheck("SS-BATCH-001", "pass", ()), reuse, search_check), tuple(decisions))
