"""Pure BuildNarrativeGraph generation request compilation; no provider I/O."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import cast

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
)
from ..semantic_chain.coverage_analysis import Stage1CoveragePolicy
from ..semantic_chain.dependency_projection import DependencyProjectionPolicy
from ..semantic_chain.draft_provider import build_draft_text_format, decode_draft_request_payload
from ..semantic_chain.stage1_command_policy import (
    Stage1CommandPolicy,
    Stage1GenerationPolicy,
    require_closed_mapping,
    require_nonempty_text,
)
from ..semantic_chain.stage1_draft import (
    Stage1DraftPolicy,
    stage1_draft_prompt_inputs,
    stage1_draft_response_schema,
)
from ..store.models import (
    ArtifactScope,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    Job,
    JobProfile,
    canonical_recipe_scope,
)
from ..vlm.retry_policy import GenerationRetryPolicy


@dataclass(frozen=True, slots=True)
class BuildNarrativeGraphRequest:
    inputs: CommittedSemanticInputsRequest
    idempotency_key: str
    artifact_revision: int
    generation: Stage1GenerationPolicy
    draft_policy: Stage1DraftPolicy
    coverage_policy: Stage1CoveragePolicy
    dependency_policy: DependencyProjectionPolicy
    retry_policy: GenerationRetryPolicy

    def __post_init__(self) -> None:
        if type(self.inputs) is not CommittedSemanticInputsRequest:  # noqa: E721
            raise ValueError("request requires exact committed semantic input request")
        require_nonempty_text(self.idempotency_key, "idempotency_key")
        # Policy construction owns the shared exact-type and revision checks.
        _ = self.command_policy
        source = self.inputs.source_manifest
        if (source.artifact_type, source.logical_id, source.member_ordinal, source.scope) != (
            "whole_series_source_manifest", "whole_series_source_manifest", 0, canonical_recipe_scope(self.inputs.job)
        ):
            raise ValueError("request source manifest identity is invalid")
        if self.inputs.job.profile not in ("test", "shadow", "production"):
            raise ValueError("request job profile is unsupported")
        if self.inputs.vlm_semantic_pack_set.scope != canonical_recipe_scope(self.inputs.job):
            raise ValueError("request VLM aggregate must belong to the same Job scope")

    @property
    def job(self) -> Job:
        return self.inputs.job

    @property
    def artifact_scope(self) -> ArtifactScope:
        return canonical_recipe_scope(self.inputs.job)

    @property
    def command_policy(self) -> Stage1CommandPolicy:
        return Stage1CommandPolicy(
            self.artifact_revision, self.generation, self.draft_policy,
            self.coverage_policy, self.dependency_policy, self.retry_policy,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "inputs": {"job": {"job_key": self.job.job_key, "profile": self.job.profile}, "source_manifest": self.inputs.source_manifest.to_mapping(), "vlm_semantic_pack_set": self.inputs.vlm_semantic_pack_set.to_mapping()},
            "idempotency_key": self.idempotency_key, **self.command_policy.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> BuildNarrativeGraphRequest:
        item = require_closed_mapping(value, {"inputs", "idempotency_key", "artifact_revision", "generation", "draft_policy", "coverage_policy", "dependency_policy", "retry_policy"}, "BuildNarrativeGraph request")
        inputs = require_closed_mapping(item["inputs"], {"job", "source_manifest", "vlm_semantic_pack_set"}, "request inputs")
        job = require_closed_mapping(inputs["job"], {"job_key", "profile"}, "request job")
        policy = Stage1CommandPolicy.from_mapping({field.name: item[field.name] for field in fields(Stage1CommandPolicy)})
        return cls(
            CommittedSemanticInputsRequest(Job(cast(str, job["job_key"]), cast(JobProfile, job["profile"])), CommittedArtifactMemberReference.from_mapping(inputs["source_manifest"]), CommittedArtifactMemberReference.from_mapping(inputs["vlm_semantic_pack_set"])),
            cast(str, item["idempotency_key"]), policy.artifact_revision, policy.generation,
            policy.draft_policy, policy.coverage_policy, policy.dependency_policy, policy.retry_policy,
        )


@dataclass(frozen=True, slots=True)
class PreparedStage1Request:
    request: BuildNarrativeGraphRequest
    input_binding_sha256: str
    provider_payload: bytes
    request_payload: bytes

    @property
    def request_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.request_payload).hexdigest()

    def provider_idempotency_key_for(self, ordinal: int) -> str:
        if type(ordinal) is not int or not 1 <= ordinal <= self.request.retry_policy.max_attempts:  # noqa: E721
            raise ValueError("attempt ordinal exceeds retry policy")
        return canonical_json_hash({"command": "BuildNarrativeGraph", "job_key": self.request.job.job_key, "idempotency_key": self.request.idempotency_key, "request_hash": self.request_hash, "attempt_ordinal": ordinal})


def prepare_stage1_request(request: BuildNarrativeGraphRequest, inputs: CommittedSemanticInputs) -> PreparedStage1Request:
    if type(request) is not BuildNarrativeGraphRequest or type(inputs) is not CommittedSemanticInputs:  # noqa: E721
        raise ValueError("prepare requires exact request and committed semantic inputs")
    persisted_source = inputs.source_manifest
    source = CommittedArtifactMemberReference(
        persisted_source.receipt_id, persisted_source.artifact_set_id, 0,
        persisted_source.reference.scope, persisted_source.reference.artifact_type,
        persisted_source.reference.logical_id, persisted_source.reference.revision,
        persisted_source.reference.content_hash,
    )
    aggregate = inputs.vlm_semantic_pack_set
    if (
        request.inputs.source_manifest != source
        or request.inputs.vlm_semantic_pack_set != aggregate
        or source.scope != request.artifact_scope
        or persisted_source.source_job != request.job
    ):
        raise ValueError("supplied semantic inputs do not exactly bind request references")
    projection = stage1_draft_prompt_inputs(inputs, policy=request.draft_policy)
    binding = cast(str, projection["input_binding_sha256"])
    prompt = request.generation.prompt_template + "\n\n" + canonical_json_bytes(projection).decode("utf-8")
    body: dict[str, object] = {
        "model": request.generation.model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "text": build_draft_text_format(request.generation.adapter_strategy_version,
                                        "stage1_cross_window_draft_v1", stage1_draft_response_schema(request.draft_policy)),
        "max_output_tokens": request.generation.max_output_tokens, "thinking": {"type": "disabled"}, "temperature": float(Decimal(request.generation.temperature)), "stream": True, "store": True,
    }
    if not math.isfinite(cast(float, body["temperature"])):
        raise ValueError("generation temperature must be finite")
    provider_payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")
    if len(provider_payload) > request.draft_policy.max_prompt_bytes:
        raise ValueError("complete provider request exceeds explicit prompt byte budget")
    decode_draft_request_payload(provider_payload)
    envelope = {
        "schema_version": "stage1-generation-request-v1", "command_request": request.to_mapping(),
        "input_binding_sha256": binding, "provider_request_json": provider_payload.decode("utf-8"),
        "provider_request_sha256": "sha256:" + hashlib.sha256(provider_payload).hexdigest(),
        "retry_policy": request.retry_policy.to_mapping(), "retry_policy_sha256": request.retry_policy.canonical_hash,
        "compiler_strategy_version": "stage1-semantic-compiler-v1", "evaluation_strategy_version": "stage1-kc-v1",
    }
    return PreparedStage1Request(request, binding, provider_payload, canonical_json_bytes(envelope))
