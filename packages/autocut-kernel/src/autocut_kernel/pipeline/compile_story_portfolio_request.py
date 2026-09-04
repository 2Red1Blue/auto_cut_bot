"""Pure Stage 2 request preparation over an exact predecessor reader result.

This module neither reads a Store nor executes Stage 1. Types and hashes bind
supplied content, not commitment: the Command must obtain inputs through the
audited predecessor reader before calling this pure preparation boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash, sha256_bytes
from ..semantic_chain.candidate_catalog import CandidateCatalogPolicy
from ..semantic_chain.candidate_projection import (
    CandidateCatalogProjection,
    project_candidate_catalog,
)
from ..semantic_chain.draft_provider import build_draft_text_format, decode_draft_request_payload
from ..semantic_chain.member_refs import SemanticMemberIdentity
from ..semantic_chain.stage1_command_policy import (
    Stage1GenerationPolicy,
    require_closed_mapping,
    require_nonempty_text,
)
from ..semantic_chain.stage1_result import Stage1Values
from ..semantic_chain.story_design_command_policy import Stage2CommandPolicy
from ..semantic_chain.story_design_context import story_design_input_binding
from ..semantic_chain.story_design_draft import (
    StoryDesignDraftPolicy,
    story_design_draft_response_schema,
)
from ..semantic_chain.story_design_models import JobPolicy, StoryDesignPolicy
from ..store.models import (
    ArtifactScope,
    CommandOutcome,
    CommittedSemanticInputs,
    Job,
    PersistedCommittedArtifactSet,
)
from ..vlm.retry_policy import GenerationRetryPolicy
from .build_narrative_graph_command import COMMAND_NAME as STAGE1_COMMAND_NAME
from .build_narrative_graph_command import PersistedNarrativeGraphSet
from .build_narrative_graph_request import BuildNarrativeGraphRequest, prepare_stage1_request
from .committed_outcome import succeeded_outcome_from_mapping, succeeded_outcome_mapping
from .story_design_inputs import CommittedStoryDesignInputs


@dataclass(frozen=True, slots=True)
class CompileStoryPortfolioRequest:
    stage1_request: BuildNarrativeGraphRequest
    stage1_outcome: CommandOutcome
    idempotency_key: str
    artifact_revision: int
    generation: Stage1GenerationPolicy
    max_prompt_bytes: int
    draft_policy: StoryDesignDraftPolicy
    candidate_policy: CandidateCatalogPolicy
    job_policy: JobPolicy
    story_policy: StoryDesignPolicy
    retry_policy: GenerationRetryPolicy

    def __post_init__(self) -> None:
        if type(self.stage1_request) is not BuildNarrativeGraphRequest:  # noqa: E721
            raise ValueError("Stage 2 requires the full frozen Stage 1 request")
        succeeded_outcome_mapping(self.stage1_outcome)
        require_nonempty_text(self.idempotency_key, "idempotency_key")
        _ = self.command_policy

    @property
    def job(self) -> Job:
        return self.stage1_request.job

    @property
    def artifact_scope(self) -> ArtifactScope:
        return self.stage1_request.artifact_scope

    @property
    def command_policy(self) -> Stage2CommandPolicy:
        return Stage2CommandPolicy(
            self.artifact_revision, self.generation, self.max_prompt_bytes, self.draft_policy,
            self.candidate_policy, self.job_policy, self.story_policy, self.retry_policy,
        )

    def to_mapping(self) -> dict[str, object]:
        return {"stage1_request": self.stage1_request.to_mapping(),
                "stage1_outcome": succeeded_outcome_mapping(self.stage1_outcome),
                "idempotency_key": self.idempotency_key, **self.command_policy.to_mapping()}

    @classmethod
    def from_mapping(cls, value: object) -> CompileStoryPortfolioRequest:
        policy_fields = {field.name for field in fields(Stage2CommandPolicy)}
        item = require_closed_mapping(value, policy_fields | {"stage1_request", "stage1_outcome", "idempotency_key"}, "Stage 2 request")
        policy = Stage2CommandPolicy.from_mapping({key: item[key] for key in policy_fields})
        return policy.build_request(BuildNarrativeGraphRequest.from_mapping(item["stage1_request"]),
                                    succeeded_outcome_from_mapping(item["stage1_outcome"]), cast(str, item["idempotency_key"]))


@dataclass(frozen=True, slots=True)
class PreparedStage2Request:
    """Prepared bytes are content, not proof that a Store reader was invoked."""

    request: CompileStoryPortfolioRequest
    input_binding_sha256: str
    provider_payload: bytes
    request_payload: bytes
    projection: CandidateCatalogProjection

    @property
    def request_hash(self) -> str:
        return sha256_bytes(self.request_payload)

    def provider_idempotency_key_for(self, ordinal: int) -> str:
        if type(ordinal) is not int or not 1 <= ordinal <= self.request.retry_policy.max_attempts:  # noqa: E721
            raise ValueError("attempt ordinal exceeds retry policy")
        return canonical_json_hash({"command": "CompileStoryPortfolio", "job_key": self.request.job.job_key,
                                    "idempotency_key": self.request.idempotency_key,
                                    "request_hash": self.request_hash, "attempt_ordinal": ordinal})


def _validate_predecessor(request: CompileStoryPortfolioRequest, inputs: CommittedStoryDesignInputs) -> None:
    if (type(inputs.semantic) is not CommittedSemanticInputs  # noqa: E721
            or type(inputs.narrative) is not PersistedNarrativeGraphSet  # noqa: E721
            or type(inputs.narrative.record) is not PersistedCommittedArtifactSet  # noqa: E721
            or type(inputs.narrative.values) is not Stage1Values):  # noqa: E721
        raise ValueError("Stage 2 preparation requires exact typed predecessor content")
    record, values, outcome = inputs.narrative.record, inputs.narrative.values, request.stage1_outcome
    prepared = prepare_stage1_request(request.stage1_request, inputs.semantic)
    if (record.job != request.job or record.job_id != outcome.job_id
            or record.job_id != inputs.semantic.source_manifest.job_id
            or record.command_slot_id != outcome.command_slot_id or record.receipt_id != outcome.receipt_id
            or record.artifact_set_id != outcome.artifact_set_id or record.command_name != STAGE1_COMMAND_NAME
            or record.execution_kind != "generation" or record.request_hash != prepared.request_hash
            or record.artifacts != values.members
            or any(member.revision != request.stage1_request.artifact_revision for member in record.artifacts)
            or values.admission.input_binding_sha256 != prepared.input_binding_sha256
            or values.admission.draft_policy_sha256 != request.stage1_request.draft_policy.canonical_hash
            or values.admission.coverage_policy_sha256 != request.stage1_request.coverage_policy.canonical_hash
            or values.admission.dependency_policy_sha256 != request.stage1_request.dependency_policy.canonical_hash
            or values.dependency_proof.source_member_ref != SemanticMemberIdentity.from_committed_member_reference(
                request.stage1_request.inputs.source_manifest)):
        raise ValueError("Stage 2 predecessor does not match the exact Stage 1 outcome/request/content")


def prepare_stage2_request(
    request: CompileStoryPortfolioRequest, inputs: CommittedStoryDesignInputs,
) -> PreparedStage2Request:
    if type(request) is not CompileStoryPortfolioRequest or type(inputs) is not CommittedStoryDesignInputs:  # noqa: E721
        raise ValueError("Stage 2 preparation requires exact request and predecessor reader values")
    _validate_predecessor(request, inputs)
    values = inputs.narrative.values
    projection = project_candidate_catalog(inputs.semantic, values, scope=request.artifact_scope,
                                           revision=request.artifact_revision, policy=request.candidate_policy)
    binding = story_design_input_binding(values, projection, job_policy=request.job_policy,
                                        story_policy=request.story_policy, candidate_policy=request.candidate_policy)
    context: dict[str, object] = {
        "schema_version": "stage2-proposal-context-v1", "input_binding_sha256": binding,
        "stage1_members": [reference.to_mapping() for reference in inputs.narrative.record.references],
        "source_grant": inputs.semantic.source_grant.to_mapping(),
        "candidate_catalog": {"member_ref": SemanticMemberIdentity.from_artifact_member(projection.member).to_mapping(),
                              "payload": projection.catalog.to_mapping()},
        "policies": {"candidate_policy": request.candidate_policy.to_mapping(),
                     "job_policy": request.job_policy.to_mapping(), "story_policy": request.story_policy.to_mapping()},
    }
    for kind, payload in (
        ("episode_digest_set", values.coverage.episode_digests.to_mapping()),
        ("event_card_set", values.coverage.event_cards.to_mapping()),
        ("narrative_graph", values.coverage.narrative_graph.to_mapping()),
    ):
        context[kind] = {"member_ref": values.coverage.identity(kind).to_mapping(), "payload": payload}
    prompt = request.generation.prompt_template + "\n\n" + canonical_json_bytes(context).decode("utf-8")
    schema = story_design_draft_response_schema(request.draft_policy)
    body = {
        "model": request.generation.model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "text": build_draft_text_format(request.generation.adapter_strategy_version,
                                        "stage2_story_design_draft_v1", schema),
        "max_output_tokens": request.generation.max_output_tokens,
        "temperature": float(Decimal(request.generation.temperature)), "stream": True, "store": True,
    }
    provider_payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")
    if len(provider_payload) > request.max_prompt_bytes:
        raise ValueError("complete provider request exceeds explicit Stage 2 prompt byte budget")
    decode_draft_request_payload(provider_payload)
    envelope = {
        "schema_version": "stage2-generation-request-v1", "command_request": request.to_mapping(),
        "command_policy_sha256": request.command_policy.canonical_hash,
        "input_binding_sha256": binding, "stage1_request_sha256": inputs.narrative.record.request_hash,
        "provider_request_json": provider_payload.decode("utf-8"),
        "provider_request_sha256": sha256_bytes(provider_payload),
        "response_schema_sha256": canonical_json_hash(schema),
        # The shared Store retry reader consumes these exact top-level fields.
        "retry_policy": request.retry_policy.to_mapping(),
        "retry_policy_sha256": request.retry_policy.canonical_hash,
    }
    return PreparedStage2Request(request, binding, provider_payload, canonical_json_bytes(envelope), projection)
