from __future__ import annotations

from pathlib import Path

import pytest
from autocut_kernel.pipeline import compile_production_recipe_command as command_module
from autocut_kernel.pipeline.compile_production_recipe_command import (
    CompileProductionRecipeCommand,
    CompileProductionRecipeError,
    inspect_committed_production_recipe_set,
)
from autocut_kernel.pipeline.recipe_timeline import RecipeTimelineReadLimits

from tests.authority.editorial_media_fixture import editorial_timed_media_case
from tests.pipeline.test_compile_production_recipe_command import (
    _install_non_dialogue_blueprint_projection,
    _request,
    _Stage4Store,
)


def _record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_unused, resolver, limits = case
    store = _Stage4Store(base)
    request = _request(case)
    outcome = CompileProductionRecipeCommand(store, resolver, limits).execute(request)
    assert outcome.committed is not None
    assert store.stage4_record is not None
    return request, store.stage4_record


def test_inspection_decodes_exact_set_without_recompiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, record = _record(tmp_path, monkeypatch)
    monkeypatch.setattr(command_module, "_compile_and_admit", lambda *_args: (_ for _ in ()).throw(AssertionError()))

    inspected = inspect_committed_production_recipe_set(
        record,
        artifact_scope=request.artifact_scope,
        artifact_revision=request.artifact_revision,
        limits=RecipeTimelineReadLimits(),
    )

    assert tuple(recipe.story.story_id for recipe in inspected.recipes) == tuple(
        subject.story_id for subject in inspected.admission.recipe_subjects
    )


def test_inspection_rejects_wrong_committed_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, record = _record(tmp_path, monkeypatch)
    object.__setattr__(record.members[0].reference, "logical_id", "forged")

    with pytest.raises(CompileProductionRecipeError, match="layout"):
        inspect_committed_production_recipe_set(
            record,
            artifact_scope=request.artifact_scope,
            artifact_revision=request.artifact_revision,
            limits=RecipeTimelineReadLimits(),
        )


def test_inspection_rechecks_member_content_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, record = _record(tmp_path, monkeypatch)
    object.__setattr__(record.members[1], "payload_json", "{}")

    with pytest.raises(CompileProductionRecipeError, match="member identity"):
        inspect_committed_production_recipe_set(
            record,
            artifact_scope=request.artifact_scope,
            artifact_revision=request.artifact_revision,
            limits=RecipeTimelineReadLimits(),
        )


def test_inspection_rechecks_artifact_set_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, record = _record(tmp_path, monkeypatch)
    object.__setattr__(record, "set_hash", "sha256:" + "f" * 64)

    with pytest.raises(CompileProductionRecipeError, match="layout"):
        inspect_committed_production_recipe_set(
            record,
            artifact_scope=request.artifact_scope,
            artifact_revision=request.artifact_revision,
            limits=RecipeTimelineReadLimits(),
        )
