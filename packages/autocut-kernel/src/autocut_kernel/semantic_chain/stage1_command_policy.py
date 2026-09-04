"""Input-free Stage 1 policy values; importing them never loads a Command or DB adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from ..contracts.compiler.canonical import canonical_json_hash
from ..store.models import CommittedSemanticInputsRequest
from ..vlm.retry_policy import GenerationRetryPolicy
from .coverage_analysis import Stage1CoveragePolicy
from .dependency_projection import DependencyProjectionPolicy
from .draft_provider import DRAFT_SUPPORTED_ADAPTER_STRATEGY_VERSIONS
from .stage1_draft import Stage1DraftPolicy

if TYPE_CHECKING:
    from ..pipeline.build_narrative_graph_request import BuildNarrativeGraphRequest

_PROVIDER = "doubao-ark-text-responses-stream"
_SAFE = 2**53 - 1


def require_nonempty_text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ValueError(f"{name} must be non-empty text")
    value.encode("utf-8")
    return value


def require_closed_mapping(value: object, expected: set[str], name: str) -> dict[str, object]:
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
            require_nonempty_text(getattr(self, field), field)
        if self.provider_id != _PROVIDER or self.adapter_strategy_version not in DRAFT_SUPPORTED_ADAPTER_STRATEGY_VERSIONS:
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
        item = require_closed_mapping(value, {field.name for field in fields(cls)}, "generation policy")
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
        item = require_closed_mapping(value, {field.name for field in fields(cls)}, "Stage 1 command policy")
        retry = require_closed_mapping(item["retry_policy"], {"strategy_version", "max_attempts", "backoff_seconds"}, "retry policy")
        if type(retry["backoff_seconds"]) is not list:  # noqa: E721
            raise ValueError("retry backoff_seconds must be an exact JSON array")
        return cls(
            cast(int, item["artifact_revision"]), Stage1GenerationPolicy.from_mapping(item["generation"]),
            Stage1DraftPolicy(**cast(dict[str, int], require_closed_mapping(item["draft_policy"], {field.name for field in fields(Stage1DraftPolicy)}, "draft policy"))),
            Stage1CoveragePolicy(**cast(dict[str, str], require_closed_mapping(item["coverage_policy"], {"minimum_confidence", "coverage_mode"}, "coverage policy"))),
            _dependency_policy(item["dependency_policy"]),
            GenerationRetryPolicy(cast(str, retry["strategy_version"]), cast(int, retry["max_attempts"]), tuple(cast(list[int], retry["backoff_seconds"]))),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())

    def build_request(
        self, inputs: CommittedSemanticInputsRequest, idempotency_key: str,
    ) -> BuildNarrativeGraphRequest:
        # Request construction is the explicit execution-layer boundary.
        from ..pipeline.build_narrative_graph_request import BuildNarrativeGraphRequest

        return BuildNarrativeGraphRequest(
            inputs, idempotency_key, self.artifact_revision, self.generation,
            self.draft_policy, self.coverage_policy, self.dependency_policy, self.retry_policy,
        )


def _dependency_policy(value: object) -> DependencyProjectionPolicy:
    policy = DependencyProjectionPolicy("semantic-dependencies-v1")
    if value != policy.to_mapping():
        raise ValueError("dependency policy must retain its full registered mapping")
    return policy
