"""Pure Stage 3 generation request preparation over exact committed inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash, sha256_bytes
from ..semantic_chain.draft_provider import decode_draft_request_payload
from ..semantic_chain.editorial_command_policy import Stage3CommandPolicy
from ..semantic_chain.editorial_context import EditorialContextBatch, build_editorial_contexts
from ..semantic_chain.editorial_context_models import EditorialContextPolicy
from ..semantic_chain.editorial_draft import (
    EditorialDraftPolicy,
    editorial_draft_response_schema,
)
from ..semantic_chain.editorial_feasibility import EditorialFeasibilityPolicy
from ..semantic_chain.stage1_command_policy import (
    Stage1GenerationPolicy,
    require_closed_mapping,
    require_nonempty_text,
)
from ..semantic_chain.stage1_result import Stage1Values
from ..semantic_chain.story_design_result import StoryDesignValues
from ..store.models import (
    ArtifactScope,
    CommandOutcome,
    CommittedSemanticInputs,
    Job,
    PersistedCommittedArtifactSet,
)
from ..vlm.retry_policy import GenerationRetryPolicy
from .build_narrative_graph_command import PersistedNarrativeGraphSet
from .committed_outcome import succeeded_outcome_from_mapping, succeeded_outcome_mapping
from .compile_story_portfolio_command import COMMAND_NAME as STAGE2_COMMAND_NAME
from .compile_story_portfolio_command import PersistedStoryPortfolioSet
from .compile_story_portfolio_request import (
    CompileStoryPortfolioRequest,
    PreparedStage2Request,
    prepare_stage2_request,
)
from .editorial_blueprint_inputs import CommittedEditorialBlueprintInputs
from .story_design_inputs import CommittedStoryDesignInputs


@dataclass(frozen=True, slots=True)
class BuildEditorialBlueprintRequest:
    """Frozen Stage 3 request; it does not itself establish predecessor truth."""

    stage2_request: CompileStoryPortfolioRequest
    stage2_outcome: CommandOutcome
    idempotency_key: str
    artifact_revision: int
    generation: Stage1GenerationPolicy
    max_prompt_bytes: int
    draft_policy: EditorialDraftPolicy
    context_policy: EditorialContextPolicy
    feasibility_policy: EditorialFeasibilityPolicy
    retry_policy: GenerationRetryPolicy
    blueprint_strategy_version: str

    def __post_init__(self) -> None:
        if type(self.stage2_request) is not CompileStoryPortfolioRequest:  # noqa: E721
            raise ValueError("Stage 3 requires the full frozen Stage 2 request")
        succeeded_outcome_mapping(self.stage2_outcome)
        require_nonempty_text(self.idempotency_key, "idempotency_key")
        _ = self.command_policy

    @property
    def job(self) -> Job:
        return self.stage2_request.job

    @property
    def artifact_scope(self) -> ArtifactScope:
        return self.stage2_request.artifact_scope

    @property
    def command_policy(self) -> Stage3CommandPolicy:
        return Stage3CommandPolicy(
            self.artifact_revision, self.generation, self.max_prompt_bytes,
            self.draft_policy, self.context_policy, self.feasibility_policy,
            self.retry_policy, self.blueprint_strategy_version,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "stage2_request": self.stage2_request.to_mapping(),
            "stage2_outcome": succeeded_outcome_mapping(self.stage2_outcome),
            "idempotency_key": self.idempotency_key,
            **self.command_policy.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> BuildEditorialBlueprintRequest:
        policy_fields = {field.name for field in fields(Stage3CommandPolicy)}
        item = require_closed_mapping(
            value, policy_fields | {"stage2_request", "stage2_outcome", "idempotency_key"},
            "BuildEditorialBlueprint request",
        )
        policy = Stage3CommandPolicy.from_mapping({key: item[key] for key in policy_fields})
        return policy.build_request(
            CompileStoryPortfolioRequest.from_mapping(item["stage2_request"]),
            succeeded_outcome_from_mapping(item["stage2_outcome"]),
            cast(str, item["idempotency_key"]),
        )


@dataclass(frozen=True, slots=True)
class PreparedStage3Request:
    """Exact durable bytes and the complete batch context used to make them."""

    request: BuildEditorialBlueprintRequest
    input_binding_sha256: str
    provider_payload: bytes
    request_payload: bytes
    contexts: EditorialContextBatch

    @property
    def request_hash(self) -> str:
        return sha256_bytes(self.request_payload)

    def provider_idempotency_key_for(self, ordinal: int) -> str:
        if type(ordinal) is not int or not 1 <= ordinal <= self.request.retry_policy.max_attempts:  # noqa: E721
            raise ValueError("attempt ordinal exceeds retry policy")
        return canonical_json_hash({
            "command": "BuildEditorialBlueprint",
            "job_key": self.request.job.job_key,
            "idempotency_key": self.request.idempotency_key,
            "request_hash": self.request_hash,
            "attempt_ordinal": ordinal,
        })


def _validate_predecessor(
    request: BuildEditorialBlueprintRequest, inputs: CommittedEditorialBlueprintInputs,
) -> PreparedStage2Request:
    """Rebuild Stage 2 bytes and compare all persistence identities explicitly."""
    if (type(inputs.semantic) is not CommittedSemanticInputs  # noqa: E721
            or type(inputs.narrative) is not PersistedNarrativeGraphSet  # noqa: E721
            or type(inputs.portfolio) is not PersistedStoryPortfolioSet):  # noqa: E721
        raise ValueError("Stage 3 preparation requires exact committed predecessor content")
    record, values = inputs.portfolio.record, inputs.portfolio.values
    if (type(record) is not PersistedCommittedArtifactSet  # noqa: E721
            or type(values) is not StoryDesignValues
            or type(inputs.narrative.values) is not Stage1Values):
        raise ValueError("Stage 3 preparation requires exact decoded Stage 1/2 record values")
    stage2_inputs = CommittedStoryDesignInputs(inputs.semantic, inputs.narrative)
    prepared_stage2 = prepare_stage2_request(request.stage2_request, stage2_inputs)
    outcome = request.stage2_outcome
    if (
        record.job != request.job
        or record.job_id != outcome.job_id
        or record.job_id != inputs.narrative.record.job_id
        or record.command_slot_id != outcome.command_slot_id
        or record.receipt_id != outcome.receipt_id
        or record.artifact_set_id != outcome.artifact_set_id
        or record.command_name != STAGE2_COMMAND_NAME
        or record.execution_kind != "generation"
        or record.request_hash != prepared_stage2.request_hash
        or record.artifacts != values.members
        or any(member.revision != request.stage2_request.artifact_revision for member in record.artifacts)
        or values.admission.input_binding_sha256 != prepared_stage2.input_binding_sha256
        or values.admission.next_action != "continue"
    ):
        raise ValueError("Stage 3 predecessor does not match the exact Stage 2 outcome/request/content")
    if (values.admission.draft_policy_sha256 != request.stage2_request.draft_policy.canonical_hash
            or values.admission.candidate_policy_sha256 != request.stage2_request.candidate_policy.canonical_hash
            or values.admission.job_policy_sha256 != request.stage2_request.job_policy.canonical_hash
            or values.admission.story_policy_sha256 != request.stage2_request.story_policy.canonical_hash):
        raise ValueError("Stage 3 predecessor Stage 2 policy bindings differ from the full request")
    return prepared_stage2


def prepare_stage3_request(
    request: BuildEditorialBlueprintRequest, inputs: CommittedEditorialBlueprintInputs,
) -> PreparedStage3Request:
    """Make one full-batch provider/durable request without reading or executing a Command."""
    if type(request) is not BuildEditorialBlueprintRequest or type(inputs) is not CommittedEditorialBlueprintInputs:  # noqa: E721
        raise ValueError("Stage 3 preparation requires exact request and committed reader values")
    prepared_stage2 = _validate_predecessor(request, inputs)
    contexts = build_editorial_contexts(
        inputs.semantic, inputs.narrative.values, inputs.portfolio.values,
        policy=request.context_policy, scope=request.artifact_scope,
        revision=request.artifact_revision,
        job_policy=request.stage2_request.job_policy,
        story_policy=request.stage2_request.story_policy,
        candidate_policy=request.stage2_request.candidate_policy,
    )
    prompt = request.generation.prompt_template + "\n\n" + contexts.prompt_payload.decode("utf-8")
    schema = editorial_draft_response_schema(
        request.draft_policy, target_story_ids=contexts.target_story_ids,
    )
    body: dict[str, object] = {
        "model": request.generation.model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "text": {"format": {"type": "json_schema", "json_schema": {
            "name": "stage3_editorial_blueprint_draft_v1", "schema": schema, "strict": True,
        }}},
        "max_output_tokens": request.generation.max_output_tokens,
        "temperature": float(Decimal(request.generation.temperature)),
        "stream": True,
        "store": True,
    }
    provider_payload = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False,
    ).encode("utf-8")
    if len(provider_payload) > request.max_prompt_bytes:
        raise ValueError("complete provider request exceeds explicit Stage 3 prompt byte budget")
    decode_draft_request_payload(provider_payload)
    stage2_outcome = succeeded_outcome_mapping(request.stage2_outcome)
    envelope = {
        "schema_version": "stage3-generation-request-v1",
        "command_request": request.to_mapping(),
        "command_policy_sha256": request.command_policy.canonical_hash,
        "input_binding_sha256": contexts.input_binding_sha256,
        "context_sha256": contexts.canonical_hash,
        "stage2_request_sha256": prepared_stage2.request_hash,
        "stage2_outcome": stage2_outcome,
        "stage2_outcome_sha256": canonical_json_hash(stage2_outcome),
        "provider_request_json": provider_payload.decode("utf-8"),
        "provider_request_sha256": sha256_bytes(provider_payload),
        "response_schema_sha256": canonical_json_hash(schema),
        "retry_policy": request.retry_policy.to_mapping(),
        "retry_policy_sha256": request.retry_policy.canonical_hash,
    }
    return PreparedStage3Request(
        request, contexts.input_binding_sha256, provider_payload,
        canonical_json_bytes(envelope), contexts,
    )
