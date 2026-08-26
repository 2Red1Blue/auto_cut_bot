"""Pure BuildNarrativeGraph generation request compilation; no provider I/O."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import cast

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
)
from ..semantic_chain.coverage_analysis import Stage1CoveragePolicy
from ..semantic_chain.dependency_projection import DependencyProjectionPolicy
from ..semantic_chain.draft_provider import decode_draft_request_payload
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

_PROVIDER = "doubao-ark-text-responses-stream"
_STRATEGY = "doubao-ark-text-responses-stream-v1"
_SAFE = 2**53 - 1


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ValueError(f"{name} must be non-empty text")
    value.encode("utf-8")
    return value


def _closed(value: object, expected: set[str], name: str) -> dict[str, object]:
    if type(value) is not dict or set(cast(dict[str, object], value)) != expected:  # noqa: E721
        raise ValueError(f"{name} has missing or unknown fields")
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class Stage1GenerationPolicy:
    provider_id: str
    model_id: str
    prompt_version: str
    prompt_template: str
    adapter_strategy_version: str
    max_output_tokens: int
    temperature: str

    def __post_init__(self) -> None:
        for field in ("provider_id", "model_id", "prompt_version", "prompt_template", "adapter_strategy_version"):
            _text(getattr(self, field), field)
        if self.provider_id != _PROVIDER or self.adapter_strategy_version != _STRATEGY:
            raise ValueError("Stage 1 requires the registered text Responses provider strategy")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 32768:  # noqa: E721
            raise ValueError("max_output_tokens must be a positive provider-bounded integer")
        if (type(self.temperature) is not str or len(self.temperature) > 32  # noqa: E721
                or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?", self.temperature) is None):
            raise ValueError("temperature must be a canonical decimal string")
        # Bound and validate spelling before Decimal; exponent expansion must
        # never allocate unbounded strings, and canonical integer zero is valid.
        if not Decimal("0") <= Decimal(self.temperature) <= Decimal("2"):
            raise ValueError("temperature must be canonical decimal from 0 to 2")

    def to_mapping(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_mapping(cls, value: object) -> Stage1GenerationPolicy:
        item = _closed(value, {field.name for field in fields(cls)}, "generation policy")
        # The exact constructor validates primitives; casts do not grant trust.
        return cls(
            cast(str, item["provider_id"]), cast(str, item["model_id"]),
            cast(str, item["prompt_version"]), cast(str, item["prompt_template"]),
            cast(str, item["adapter_strategy_version"]), cast(int, item["max_output_tokens"]),
            cast(str, item["temperature"]),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class Stage1CommandPolicy:
    """Closed input-free configuration, never committed input or authority.

    Building a request still requires real full predecessor references; policy
    decoding does not synthesize a Job, Source, VLM owner or acceptance claim.
    """

    artifact_revision: int
    generation: Stage1GenerationPolicy
    draft_policy: Stage1DraftPolicy
    coverage_policy: Stage1CoveragePolicy
    dependency_policy: DependencyProjectionPolicy
    retry_policy: GenerationRetryPolicy

    def __post_init__(self) -> None:
        if type(self.artifact_revision) is not int or not 1 <= self.artifact_revision <= _SAFE:  # noqa: E721
            raise ValueError("artifact_revision must be a positive safe integer")
        if any(type(item) is not kind for item, kind in (
            (self.generation, Stage1GenerationPolicy), (self.draft_policy, Stage1DraftPolicy),
            (self.coverage_policy, Stage1CoveragePolicy), (self.dependency_policy, DependencyProjectionPolicy),
            (self.retry_policy, GenerationRetryPolicy),
        )):
            raise ValueError("request policies must be exact registered values")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_revision": self.artifact_revision,
            "generation": self.generation.to_mapping(), "draft_policy": self.draft_policy.to_mapping(),
            "coverage_policy": self.coverage_policy.to_mapping(), "dependency_policy": self.dependency_policy.to_mapping(),
            "retry_policy": self.retry_policy.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Stage1CommandPolicy:
        item = _closed(value, {field.name for field in fields(cls)}, "Stage 1 command policy")
        retry = _closed(item["retry_policy"], {"strategy_version", "max_attempts", "backoff_seconds"}, "retry policy")
        if type(retry["backoff_seconds"]) is not list:  # noqa: E721
            raise ValueError("retry backoff_seconds must be an exact JSON array")
        return cls(
            cast(int, item["artifact_revision"]), Stage1GenerationPolicy.from_mapping(item["generation"]),
            Stage1DraftPolicy(**cast(dict[str, int], _closed(item["draft_policy"], {field.name for field in fields(Stage1DraftPolicy)}, "draft policy"))),
            Stage1CoveragePolicy(**cast(dict[str, str], _closed(item["coverage_policy"], {"minimum_confidence", "coverage_mode"}, "coverage policy"))),
            _dependency_policy(item["dependency_policy"]),
            GenerationRetryPolicy(cast(str, retry["strategy_version"]), cast(int, retry["max_attempts"]), tuple(cast(list[int], retry["backoff_seconds"]))),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())

    def build_request(
        self, inputs: CommittedSemanticInputsRequest, idempotency_key: str,
    ) -> BuildNarrativeGraphRequest:
        return BuildNarrativeGraphRequest(
            inputs, idempotency_key, self.artifact_revision, self.generation,
            self.draft_policy, self.coverage_policy, self.dependency_policy, self.retry_policy,
        )


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
        _text(self.idempotency_key, "idempotency_key")
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
        item = _closed(value, {"inputs", "idempotency_key", "artifact_revision", "generation", "draft_policy", "coverage_policy", "dependency_policy", "retry_policy"}, "BuildNarrativeGraph request")
        inputs = _closed(item["inputs"], {"job", "source_manifest", "vlm_semantic_pack_set"}, "request inputs")
        job = _closed(inputs["job"], {"job_key", "profile"}, "request job")
        policy = Stage1CommandPolicy.from_mapping({field.name: item[field.name] for field in fields(Stage1CommandPolicy)})
        return cls(
            CommittedSemanticInputsRequest(Job(cast(str, job["job_key"]), cast(JobProfile, job["profile"])), CommittedArtifactMemberReference.from_mapping(inputs["source_manifest"]), CommittedArtifactMemberReference.from_mapping(inputs["vlm_semantic_pack_set"])),
            cast(str, item["idempotency_key"]), policy.artifact_revision, policy.generation,
            policy.draft_policy, policy.coverage_policy, policy.dependency_policy, policy.retry_policy,
        )


def _dependency_policy(value: object) -> DependencyProjectionPolicy:
    policy = DependencyProjectionPolicy("semantic-dependencies-v1")
    if value != policy.to_mapping():
        raise ValueError("dependency policy must retain its full registered mapping")
    return policy


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
        "text": {"format": {"type": "json_schema", "json_schema": {"name": "stage1_cross_window_draft_v1", "schema": stage1_draft_response_schema(request.draft_policy), "strict": True}}},
        "max_output_tokens": request.generation.max_output_tokens, "temperature": float(Decimal(request.generation.temperature)), "stream": True, "store": True,
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
