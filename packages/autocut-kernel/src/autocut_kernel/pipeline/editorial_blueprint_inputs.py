"""Read exact admitted Stage 1/2 inputs without generation or mutable heads."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ..semantic_chain.member_refs import SemanticMemberIdentity
from ..store.models import CommandOutcome, CommittedSemanticInputs
from .build_narrative_graph_command import NarrativeGraphStore, PersistedNarrativeGraphSet
from .compile_story_portfolio_command import (
    PersistedStoryPortfolioSet,
    read_committed_story_portfolio,
)
from .compile_story_portfolio_request import CompileStoryPortfolioRequest, prepare_stage2_request
from .story_design_inputs import read_committed_story_design_inputs


@dataclass(frozen=True, slots=True)
class CommittedEditorialBlueprintInputs:
    """Reader result, not a capability conferred by direct construction.

    Stage 3 consumes these exact admitted predecessors and their owner-bound
    raw semantic evidence. This value exposes no ASR/VAD or physical endpoints.
    """

    semantic: CommittedSemanticInputs
    narrative: PersistedNarrativeGraphSet
    portfolio: PersistedStoryPortfolioSet


def read_committed_editorial_blueprint_inputs(
    store: NarrativeGraphStore, *, stage2_request: CompileStoryPortfolioRequest,
    stage2_outcome: CommandOutcome,
) -> CommittedEditorialBlueprintInputs:
    """Audit the five-member result and rejoin its exact eight-member parent.

    The existing readers own raw-response/attempt/independent-rule validation
    and Source operation authorization. This seam does not execute an upstream
    Command, claim a slot, invoke a provider, or substitute caller-built values
    for persistence. All identities are exact references, never latest heads.
    """
    if type(stage2_request) is not CompileStoryPortfolioRequest:  # noqa: E721
        raise ValueError("Stage 3 requires the exact frozen Stage 2 request")
    if (type(stage2_outcome) is not CommandOutcome  # noqa: E721
            or type(stage2_outcome.state) is not str or stage2_outcome.state != "succeeded"  # noqa: E721
            or type(stage2_outcome.is_fresh_claim) is not bool  # noqa: E721
            or stage2_outcome.failure_code is not None or stage2_outcome.failure_detail_json is not None
            or any(type(value) is not UUID for value in (
                stage2_outcome.job_id, stage2_outcome.command_slot_id,
                stage2_outcome.receipt_id, stage2_outcome.artifact_set_id,
            ))):
        raise ValueError("Stage 3 requires an exact succeeded Stage 2 Job/slot/Receipt/Set")
    # CommandOutcome equality deliberately excludes job_id. Do not use it to
    # join even apparently equal transport outcomes from different Jobs.
    if stage2_outcome.job_id != stage2_request.stage1_outcome.job_id:
        raise ValueError("Stage 3 predecessors belong to different Jobs")
    portfolio = read_committed_story_portfolio(store, stage2_request, stage2_outcome)
    predecessors = read_committed_story_design_inputs(
        store, stage1_request=stage2_request.stage1_request,
        stage1_outcome=stage2_request.stage1_outcome,
    )
    prepared = prepare_stage2_request(stage2_request, predecessors)
    record, values = portfolio.record, portfolio.values
    business = values.business
    catalog_identity = SemanticMemberIdentity.from_artifact_member(prepared.projection.member)
    if (record.job != stage2_request.job or record.job_id != stage2_outcome.job_id
            or record.job_id != predecessors.narrative.record.job_id
            or record.command_slot_id != stage2_outcome.command_slot_id
            or record.receipt_id != stage2_outcome.receipt_id
            or record.artifact_set_id != stage2_outcome.artifact_set_id
            or record.request_hash != prepared.request_hash
            or values.admission.input_binding_sha256 != prepared.input_binding_sha256
            or business.proposal_set.input_binding_sha256 != prepared.input_binding_sha256
            or SemanticMemberIdentity.from_artifact_member(record.artifacts[0]) != catalog_identity
            or business.proposal_set.candidate_catalog_ref != catalog_identity
            or business.candidate_catalog != prepared.projection.catalog):
        raise ValueError("Stage 3 predecessors differ from the audited Stage 2 request/input/catalog")
    targets = business.portfolio.target_story_ids
    if (not targets or len(targets) != stage2_request.job_policy.selected_story_count
            or targets != business.source_usage_ledger.target_story_ids
            or targets != values.admission.target_story_ids):
        raise ValueError("Stage 3 requires the complete frozen Portfolio/Usage/Admission target order")
    return CommittedEditorialBlueprintInputs(predecessors.semantic, predecessors.narrative, portfolio)
