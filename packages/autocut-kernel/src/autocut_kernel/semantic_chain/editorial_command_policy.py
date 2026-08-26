"""Closed input-free Stage 3 generation policy; never Store authority."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..vlm.retry_policy import GenerationRetryPolicy
from .draft_provider import MAX_DRAFT_REQUEST_BYTES
from .editorial_blueprint import EDITORIAL_BLUEPRINT_STRATEGY_VERSION
from .editorial_context_models import EditorialContextPolicy
from .editorial_draft import EditorialDraftPolicy
from .editorial_feasibility import EditorialFeasibilityPolicy
from .stage1_command_policy import (
    Stage1GenerationPolicy,
    require_closed_mapping,
)

if TYPE_CHECKING:
    from ..pipeline.build_editorial_blueprint_request import BuildEditorialBlueprintRequest
    from ..pipeline.compile_story_portfolio_request import CompileStoryPortfolioRequest
    from ..store.models import CommandOutcome


@dataclass(frozen=True, slots=True)
class Stage3CommandPolicy:
    """Explicit Stage 3 configuration, without Job/input/commit authority."""

    artifact_revision: int
    generation: Stage1GenerationPolicy
    max_prompt_bytes: int
    draft_policy: EditorialDraftPolicy
    context_policy: EditorialContextPolicy
    feasibility_policy: EditorialFeasibilityPolicy
    retry_policy: GenerationRetryPolicy
    blueprint_strategy_version: str

    def __post_init__(self) -> None:
        if type(self.artifact_revision) is not int or not 1 <= self.artifact_revision < 2**53:  # noqa: E721
            raise ValueError("Stage 3 artifact_revision must be a positive safe integer")
        if type(self.max_prompt_bytes) is not int or not 1 <= self.max_prompt_bytes <= MAX_DRAFT_REQUEST_BYTES:  # noqa: E721
            raise ValueError("Stage 3 prompt budget exceeds the text provider byte ceiling")
        if (type(self.blueprint_strategy_version) is not str  # noqa: E721
                or self.blueprint_strategy_version != EDITORIAL_BLUEPRINT_STRATEGY_VERSION):
            raise ValueError("Stage 3 Blueprint strategy is not registered")
        if any(type(value) is not kind for value, kind in (
            (self.generation, Stage1GenerationPolicy),
            (self.draft_policy, EditorialDraftPolicy),
            (self.context_policy, EditorialContextPolicy),
            (self.feasibility_policy, EditorialFeasibilityPolicy),
            (self.retry_policy, GenerationRetryPolicy),
        )):
            raise ValueError("Stage 3 policies must be exact registered typed values")
        canonical_json_bytes(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_revision": self.artifact_revision,
            "generation": self.generation.to_mapping(),
            "max_prompt_bytes": self.max_prompt_bytes,
            "draft_policy": self.draft_policy.to_mapping(),
            "context_policy": self.context_policy.to_mapping(),
            "feasibility_policy": self.feasibility_policy.to_mapping(),
            "retry_policy": self.retry_policy.to_mapping(),
            "blueprint_strategy_version": self.blueprint_strategy_version,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Stage3CommandPolicy:
        item = require_closed_mapping(value, {field.name for field in fields(cls)}, "Stage 3 command policy")
        retry = require_closed_mapping(
            item["retry_policy"], {"strategy_version", "max_attempts", "backoff_seconds"}, "retry policy",
        )
        if type(retry["backoff_seconds"]) is not list:  # noqa: E721
            raise ValueError("retry backoff_seconds must be an exact JSON array")
        return cls(
            cast(int, item["artifact_revision"]),
            Stage1GenerationPolicy.from_mapping(item["generation"]),
            cast(int, item["max_prompt_bytes"]),
            EditorialDraftPolicy.from_mapping(item["draft_policy"]),
            EditorialContextPolicy.from_mapping(item["context_policy"]),
            EditorialFeasibilityPolicy.from_mapping(item["feasibility_policy"]),
            GenerationRetryPolicy(
                cast(str, retry["strategy_version"]), cast(int, retry["max_attempts"]),
                tuple(cast(list[int], retry["backoff_seconds"])),
            ),
            cast(str, item["blueprint_strategy_version"]),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())

    def build_request(
        self, stage2_request: CompileStoryPortfolioRequest, stage2_outcome: CommandOutcome,
        idempotency_key: str,
    ) -> BuildEditorialBlueprintRequest:
        """Create a pending request; an exact committed reader is still required."""
        from ..pipeline.build_editorial_blueprint_request import BuildEditorialBlueprintRequest

        return BuildEditorialBlueprintRequest(
            stage2_request, stage2_outcome, idempotency_key, self.artifact_revision,
            self.generation, self.max_prompt_bytes, self.draft_policy, self.context_policy,
            self.feasibility_policy, self.retry_policy, self.blueprint_strategy_version,
        )
