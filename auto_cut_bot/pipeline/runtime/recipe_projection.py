"""Read-only timeline projections for exact committed Stage 4 Recipes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from autocut_kernel.pipeline import (
    RecipeDiff,
    RecipeTimeline,
    RecipeTimelineError,
    RecipeTimelineReadLimits,
    diff_recipe_timelines,
    inspect_committed_production_recipe_set,
    project_recipe_timeline,
)
from autocut_kernel.store import (
    ArtifactScope,
    Job,
    RuntimeStoreError,
)
from autocut_kernel.store.models import PersistedCommittedArtifactSet, canonical_recipe_scope

from .errors import PipelineRunNotFoundError, PipelineRunValidationError
from .models import PipelineRunSnapshot, validate_run_id
from .ports import PipelineRunStore

_STORY_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,256}")


class PipelineRecipeProjectionStore(Protocol):
    """The narrow Store query needed by the read-only Recipe projection."""

    def find_committed_production_recipe_set(
        self,
        job: Job,
        *,
        artifact_scope: ArtifactScope,
        artifact_revision: int,
    ) -> PersistedCommittedArtifactSet | None: ...


@dataclass(frozen=True, slots=True)
class PipelineRecipeTimelineReady:
    timeline: RecipeTimeline
    status: Literal["ready"] = "ready"

    def to_mapping(self) -> dict[str, object]:
        return {"status": self.status, "timeline": self.timeline.to_mapping()}


@dataclass(frozen=True, slots=True)
class PipelineRecipeDiffReady:
    diff: RecipeDiff
    status: Literal["ready"] = "ready"

    def to_mapping(self) -> dict[str, object]:
        return {"status": self.status, "diff": self.diff.to_mapping()}


@dataclass(frozen=True, slots=True)
class PipelineRecipeNotReady:
    status: Literal["not_ready"] = "not_ready"

    def to_mapping(self) -> dict[str, str]:
        return {"status": self.status}


PipelineRecipeTimelineResult: TypeAlias = PipelineRecipeTimelineReady | PipelineRecipeNotReady
PipelineRecipeDiffResult: TypeAlias = PipelineRecipeDiffReady | PipelineRecipeNotReady


class PipelineRecipeReadService:
    """Project one immutable Stage 4 Recipe belonging to one durable run."""

    def __init__(
        self,
        run_store: PipelineRunStore,
        store: PipelineRecipeProjectionStore,
        *,
        limits: RecipeTimelineReadLimits = RecipeTimelineReadLimits(),
    ) -> None:
        if type(limits) is not RecipeTimelineReadLimits:  # noqa: E721
            raise ValueError("recipe projection requires exact timeline read limits")
        self._run_store = run_store
        self._store = store
        self._limits = limits

    async def get_timeline(
        self,
        run_id: str,
        story_id: str,
        revision: int,
    ) -> PipelineRecipeTimelineResult:
        validate_run_id(run_id)
        _validate_story_id(story_id)
        _validate_revision(revision)
        snapshot = await self._read_snapshot(run_id)
        job = Job(run_id, snapshot.request.profile)
        try:
            record = self._store.find_committed_production_recipe_set(
                job,
                artifact_scope=canonical_recipe_scope(job),
                artifact_revision=revision,
            )
        except RuntimeStoreError as error:
            raise PipelineRunValidationError("committed Recipe closure is unavailable") from error
        if record is None:
            return PipelineRecipeNotReady()
        try:
            inspected = inspect_committed_production_recipe_set(
                record,
                artifact_scope=canonical_recipe_scope(job),
                artifact_revision=revision,
                limits=self._limits,
            )
            matches = tuple(
                (member.reference, recipe)
                for member, recipe in zip(record.members[1:-1], inspected.recipes, strict=True)
                if recipe.story.story_id == story_id
            )
            if len(matches) != 1:
                raise RecipeTimelineError("committed Recipe story identity is unavailable or ambiguous")
            reference, recipe = matches[0]
            return PipelineRecipeTimelineReady(project_recipe_timeline(recipe, reference, limits=self._limits))
        except (RecipeTimelineError, RuntimeStoreError, ValueError) as error:
            raise PipelineRunValidationError("committed Recipe closure is invalid") from error

    async def get_diff(
        self,
        run_id: str,
        story_id: str,
        base_revision: int,
        target_revision: int,
    ) -> PipelineRecipeDiffResult:
        base = await self.get_timeline(run_id, story_id, base_revision)
        target = await self.get_timeline(run_id, story_id, target_revision)
        if isinstance(base, PipelineRecipeNotReady) or isinstance(target, PipelineRecipeNotReady):
            return PipelineRecipeNotReady()
        try:
            return PipelineRecipeDiffReady(diff_recipe_timelines(base.timeline, target.timeline))
        except RecipeTimelineError as error:
            raise PipelineRunValidationError("committed Recipe diff is unavailable") from error

    async def _read_snapshot(self, run_id: str) -> PipelineRunSnapshot:
        snapshot = await self._run_store.read_run(run_id)
        if snapshot is None:
            raise PipelineRunNotFoundError(run_id)
        if snapshot.execution_profile.is_legacy_unresolved:
            raise PipelineRunValidationError("committed Recipe view requires a frozen execution profile")
        return snapshot


def _validate_story_id(story_id: str) -> None:
    if type(story_id) is not str or _STORY_ID.fullmatch(story_id) is None:  # noqa: E721
        raise PipelineRunValidationError("Recipe story identifier is invalid")


def _validate_revision(revision: int) -> None:
    if type(revision) is not int or revision < 1:  # noqa: E721
        raise PipelineRunValidationError("Recipe revision is invalid")


__all__ = (
    "PipelineRecipeDiffReady",
    "PipelineRecipeDiffResult",
    "PipelineRecipeNotReady",
    "PipelineRecipeProjectionStore",
    "PipelineRecipeReadService",
    "PipelineRecipeTimelineReady",
    "PipelineRecipeTimelineResult",
)
