from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.pipeline.recipe_timeline import (
    RecipeTimelineError,
    diff_recipe_timelines,
    project_recipe_timeline,
)
from autocut_kernel.store.models import (
    ArtifactScope,
    BlobRef,
    CommittedArtifactMemberReference,
)

from tests.rendering.test_production_render_plan import _recipe, _span


def _reference(recipe, *, revision: int = 1) -> CommittedArtifactMemberReference:  # type: ignore[no-untyped-def]
    return CommittedArtifactMemberReference(
        UUID("11111111-1111-4111-8111-111111111111"),
        UUID("22222222-2222-4222-8222-222222222222"),
        1,
        ArtifactScope("pipeline", "job", "run-1"),
        "recipe",
        "production_recipe@" + recipe.story.story_id,
        revision,
        recipe.canonical_hash,
    )


def _blob() -> BlobRef:
    return BlobRef(
        UUID("33333333-3333-4333-8333-333333333333"),
        "sha256:" + "1" * 64,
        4096,
        "video/mp4",
    )


def test_projects_exact_source_and_output_ticks_deterministically() -> None:
    recipe = _recipe()

    first = project_recipe_timeline(recipe, _reference(recipe))
    second = project_recipe_timeline(recipe, _reference(recipe))

    assert first.to_mapping() == second.to_mapping()
    assert first.output_timescale == 90_000
    assert first.duration_ticks == 90
    assert first.clips[0].output_in_tick == 0
    assert first.clips[0].output_out_tick == 90
    assert first.clips[0].video_in_tick == 10
    assert first.clips[0].audio_in_tick == 5
    assert first.clips[0].alternative_id == "alternative-requirement-1"


def test_diff_reports_variant_move_add_and_remove_without_qc_execution() -> None:
    source = _blob()
    base = _recipe(
        _span(ordinal=0, requirement_id="r1", candidate_id="c1", source_blob=source),
        _span(ordinal=1, requirement_id="r2", candidate_id="c2", source_blob=source),
    )
    target = _recipe(
        _span(ordinal=0, requirement_id="r2", candidate_id="c2", source_blob=source),
        _span(ordinal=1, requirement_id="r1", candidate_id="c9", source_blob=source),
        _span(ordinal=2, requirement_id="r3", candidate_id="c3", source_blob=source),
    )

    diff = diff_recipe_timelines(
        project_recipe_timeline(base, _reference(base, revision=1)),
        project_recipe_timeline(target, _reference(target, revision=2)),
    )

    assert diff.requires_qc is True
    assert [item.kind for item in diff.changes] == ["variant_changed", "moved", "added"]
    assert all(item.stable_key[0] == "beat-1" for item in diff.changes)


def test_diff_rejects_different_output_timebases() -> None:
    recipe = _recipe()
    timeline = project_recipe_timeline(recipe, _reference(recipe))
    changed = replace(project_recipe_timeline(recipe, _reference(recipe, revision=2)), output_timescale=48_000)

    with pytest.raises(RecipeTimelineError, match="RECIPE_DIFF_TIME_BASE_MISMATCH"):
        diff_recipe_timelines(timeline, changed)
