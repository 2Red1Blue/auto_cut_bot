from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from autocut_kernel.store.models import ArtifactScope, CommittedArtifactMemberReference

from auto_cut_bot.pipeline.runtime.recipe_projection import (
    PipelineRecipeNotReady,
    PipelineRecipeReadService,
    PipelineRecipeTimelineReady,
)
from tests.rendering.test_production_render_plan import _recipe

_RUN_ID = "pipeline_run_0123456789abcdef0123456789abcdef"


class _RunStore:
    async def read_run(self, run_id: str):  # type: ignore[no-untyped-def]
        assert run_id == _RUN_ID
        return SimpleNamespace(
            request=SimpleNamespace(profile="test"),
            execution_profile=SimpleNamespace(is_legacy_unresolved=False),
        )


class _RecipeStore:
    def __init__(self, record: object | None) -> None:
        self.record = record
        self.calls: list[tuple[object, object, int]] = []

    def find_committed_production_recipe_set(self, job, *, artifact_scope, artifact_revision):  # type: ignore[no-untyped-def]
        self.calls.append((job, artifact_scope, artifact_revision))
        return self.record


def _record():  # type: ignore[no-untyped-def]
    recipe = _recipe()
    reference = CommittedArtifactMemberReference(
        UUID("11111111-1111-4111-8111-111111111111"),
        UUID("22222222-2222-4222-8222-222222222222"),
        1,
        ArtifactScope("pipeline", "job", _RUN_ID),
        "recipe",
        "production_recipe@story-1",
        1,
        recipe.canonical_hash,
    )
    report = SimpleNamespace(reference=SimpleNamespace())
    recipe_member = SimpleNamespace(reference=reference)
    admission = SimpleNamespace(reference=SimpleNamespace())
    return recipe, SimpleNamespace(members=(report, recipe_member, admission))


@pytest.mark.asyncio
async def test_reads_only_one_exact_recipe_timeline(monkeypatch: pytest.MonkeyPatch) -> None:
    recipe, record = _record()
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.recipe_projection.inspect_committed_production_recipe_set",
        lambda *_args, **_kwargs: SimpleNamespace(recipes=(recipe,)),
    )
    store = _RecipeStore(record)

    result = await PipelineRecipeReadService(_RunStore(), store).get_timeline(_RUN_ID, "story-1", 1)  # type: ignore[arg-type]

    assert isinstance(result, PipelineRecipeTimelineReady)
    assert result.timeline.story_id == "story-1"
    assert store.calls[0][1] == ArtifactScope("pipeline", "job", _RUN_ID)


@pytest.mark.asyncio
async def test_returns_not_ready_without_a_committed_recipe() -> None:
    result = await PipelineRecipeReadService(_RunStore(), _RecipeStore(None)).get_timeline(_RUN_ID, "story-1", 1)  # type: ignore[arg-type]

    assert isinstance(result, PipelineRecipeNotReady)
