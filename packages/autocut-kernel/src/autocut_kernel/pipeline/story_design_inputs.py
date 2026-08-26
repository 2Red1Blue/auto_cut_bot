"""Read Stage 2 predecessors without executing or regenerating Stage 1."""

from __future__ import annotations

from dataclasses import dataclass

from ..semantic_chain.member_refs import SemanticMemberIdentity
from ..store.models import CommandOutcome, CommittedSemanticInputs
from .build_narrative_graph_command import (
    NarrativeGraphStore,
    PersistedNarrativeGraphSet,
    read_committed_narrative_graph,
)
from .build_narrative_graph_request import BuildNarrativeGraphRequest, prepare_stage1_request


@dataclass(frozen=True, slots=True)
class CommittedStoryDesignInputs:
    """Reader result; manually constructing this value does not grant authority."""

    semantic: CommittedSemanticInputs
    narrative: PersistedNarrativeGraphSet


def read_committed_story_design_inputs(
    store: NarrativeGraphStore, *, stage1_request: BuildNarrativeGraphRequest,
    stage1_outcome: CommandOutcome,
) -> CommittedStoryDesignInputs:
    """Replay the exact eight-member result and re-read its immutable inputs.

    Stage 1's reader owns audit/raw-response/independent-rule verification. This
    seam never accepts caller-built Stage1Values as a substitute, never claims a
    Command, and never invokes a provider. Stage 2 additionally requires source
    rendering authorization; it still cannot choose physical cut endpoints.
    """
    if type(stage1_request) is not BuildNarrativeGraphRequest:  # noqa: E721
        raise ValueError("Stage 2 requires the exact frozen Stage 1 request")
    narrative = read_committed_narrative_graph(store, stage1_request, stage1_outcome)
    semantic = store.read_committed_semantic_inputs(stage1_request.inputs)
    prepared = prepare_stage1_request(stage1_request, semantic)
    if (
        prepared.request_hash != narrative.record.request_hash
        or prepared.input_binding_sha256 != narrative.values.admission.input_binding_sha256
        or narrative.values.dependency_proof.source_member_ref
        != SemanticMemberIdentity.from_committed_member_reference(stage1_request.inputs.source_manifest)
    ):
        raise ValueError("Stage 2 predecessors differ from the admitted exact Stage 1 inputs")
    semantic.source_grant.require_purpose("render_source")
    granted = {(item.source_id, item.content_sha256) for item in semantic.source_grant.sources}
    if any((item.source_window.source_id, item.source_window.source_sha256) not in granted for item in semantic.inputs):
        raise ValueError("Stage 2 source/window does not match its committed operation grant")
    return CommittedStoryDesignInputs(semantic, narrative)
