"""Input-free Stage 2 configuration, not Store authority or an Admission."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..store.models import CommandOutcome
from ..vlm.retry_policy import GenerationRetryPolicy
from .candidate_catalog import CandidateCatalogPolicy
from .draft_provider import MAX_DRAFT_REQUEST_BYTES
from .stage1_command_policy import Stage1GenerationPolicy, require_closed_mapping
from .story_design_draft import StoryDesignDraftPolicy
from .story_design_models import JobPolicy, StoryDesignPolicy

if TYPE_CHECKING:
    from ..pipeline.build_narrative_graph_request import BuildNarrativeGraphRequest
    from ..pipeline.compile_story_portfolio_request import CompileStoryPortfolioRequest


@dataclass(frozen=True, slots=True)
class Stage2CommandPolicy:
    artifact_revision: int
    generation: Stage1GenerationPolicy
    max_prompt_bytes: int
    draft_policy: StoryDesignDraftPolicy
    candidate_policy: CandidateCatalogPolicy
    job_policy: JobPolicy
    story_policy: StoryDesignPolicy
    retry_policy: GenerationRetryPolicy

    def __post_init__(self) -> None:
        if type(self.artifact_revision) is not int or not 1 <= self.artifact_revision < 2**53:  # noqa: E721
            raise ValueError("Stage 2 artifact_revision must be a positive safe integer")
        if type(self.max_prompt_bytes) is not int or not 1 <= self.max_prompt_bytes <= MAX_DRAFT_REQUEST_BYTES:  # noqa: E721
            raise ValueError("Stage 2 prompt budget exceeds the text provider byte ceiling")
        if any(type(value) is not kind for value, kind in (
            (self.generation, Stage1GenerationPolicy), (self.draft_policy, StoryDesignDraftPolicy),
            (self.candidate_policy, CandidateCatalogPolicy), (self.job_policy, JobPolicy),
            (self.story_policy, StoryDesignPolicy), (self.retry_policy, GenerationRetryPolicy),
        )):
            raise ValueError("Stage 2 policies must be exact registered typed values")
        if self.job_policy.story_design_policy_sha256 != self.story_policy.canonical_hash:
            raise ValueError("Stage 2 Job policy does not bind the supplied Story policy")
        # This also closes JSON-safe retry integers; no lossy numeric coercion.
        canonical_json_bytes(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_revision": self.artifact_revision, "generation": self.generation.to_mapping(),
            "max_prompt_bytes": self.max_prompt_bytes, "draft_policy": self.draft_policy.to_mapping(),
            "candidate_policy": self.candidate_policy.to_mapping(), "job_policy": self.job_policy.to_mapping(),
            "story_policy": self.story_policy.to_mapping(), "retry_policy": self.retry_policy.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Stage2CommandPolicy:
        item = require_closed_mapping(value, {field.name for field in fields(cls)}, "Stage 2 command policy")
        retry = require_closed_mapping(item["retry_policy"], {"strategy_version", "max_attempts", "backoff_seconds"}, "retry policy")
        if type(retry["backoff_seconds"]) is not list:  # noqa: E721
            raise ValueError("retry backoff_seconds must be an exact JSON array")
        return cls(
            cast(int, item["artifact_revision"]), Stage1GenerationPolicy.from_mapping(item["generation"]),
            cast(int, item["max_prompt_bytes"]), StoryDesignDraftPolicy.from_mapping(item["draft_policy"]),
            CandidateCatalogPolicy.from_mapping(item["candidate_policy"]), JobPolicy.from_mapping(item["job_policy"]),
            StoryDesignPolicy.from_mapping(item["story_policy"]),
            GenerationRetryPolicy(cast(str, retry["strategy_version"]), cast(int, retry["max_attempts"]),
                                  tuple(cast(list[int], retry["backoff_seconds"]))),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())

    def build_request(
        self, stage1_request: BuildNarrativeGraphRequest, stage1_outcome: CommandOutcome,
        idempotency_key: str,
    ) -> CompileStoryPortfolioRequest:
        from ..pipeline.compile_story_portfolio_request import CompileStoryPortfolioRequest

        return CompileStoryPortfolioRequest(
            stage1_request, stage1_outcome, idempotency_key, self.artifact_revision,
            self.generation, self.max_prompt_bytes, self.draft_policy, self.candidate_policy,
            self.job_policy, self.story_policy, self.retry_policy,
        )
