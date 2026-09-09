"""V12 composition joins three protected authorities before side effects."""

from __future__ import annotations

import pytest
from autocut_kernel.media.types import TimeBase
from autocut_kernel.physical_edit.candidate_exact_span import CandidateExactSpanPolicy
from autocut_kernel.physical_edit.editorial_exact_span import (
    EDITORIAL_EXACT_SPAN_STRATEGY,
    EditorialExactSpanPolicy,
)
from autocut_kernel.pipeline.compile_production_recipe_command import (
    ProductionRecipeCompilationLimits,
)
from autocut_kernel.registry.installed_runtime import InstalledLocalRunProfileResolver

from auto_cut_bot.pipeline.runtime.composition import (
    FUNASR_SHARED_TOKEN_ENV,
    PIPELINE_MEDIA_PREFLIGHT_POLICY_ENV,
    PIPELINE_PLAN_ENV,
    SEMANTIC_STORY_MEDIA_PLAN,
    PipelineRuntimeConfigurationError,
    compose_pipeline_runtime_from_environment,
)
from auto_cut_bot.pipeline.runtime.stage4_authority import (
    Stage4RecipeAuthorityError,
    Stage4RecipeAuthorityProfile,
)
from auto_cut_bot.pipeline.runtime.stage4_recipe_stage import Stage4RecipePipelineStage
from tests.pipeline.installed_profile_fixture import synthetic_installed_resource
from tests.pipeline.test_pipeline_runtime_composition import _environment


def _stage4_authority() -> Stage4RecipeAuthorityProfile:
    return Stage4RecipeAuthorityProfile(
        1,
        EditorialExactSpanPolicy(
            EDITORIAL_EXACT_SPAN_STRATEGY,
            90_000,
            TimeBase(1, 90_000),
        ),
        CandidateExactSpanPolicy(100_000, 100_000, 1, 1, 0),
        ProductionRecipeCompilationLimits(10_000, 8_000_000, 32_000_000),
    )


def _v12_environment(tmp_path) -> dict[str, str]:
    environment = _environment(tmp_path)
    environment[PIPELINE_PLAN_ENV] = SEMANTIC_STORY_MEDIA_PLAN
    environment[FUNASR_SHARED_TOKEN_ENV] = "v12-composition-test-token"
    return environment


def _install_v12_authorities(monkeypatch: pytest.MonkeyPatch) -> tuple[object, Stage4RecipeAuthorityProfile]:
    from auto_cut_bot.pipeline.runtime import composition

    media = InstalledLocalRunProfileResolver(synthetic_installed_resource())
    stage4 = _stage4_authority()
    monkeypatch.setattr(composition, "load_installed_local_run_resolver", lambda: media)
    monkeypatch.setattr(composition, "load_installed_stage4_recipe_authority", lambda: stage4)
    return media, stage4


def test_v12_composes_eight_shared_registry_and_reconciler_ports(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_authority, stage4_authority = _install_v12_authorities(monkeypatch)
    environment = _v12_environment(tmp_path)

    runtime = compose_pipeline_runtime_from_environment(environment)

    assert runtime is not None
    assert runtime.execution_profile.is_semantic_story_media
    assert runtime.authority_profile_resolver is media_authority
    registry = runtime.worker._runner._registry
    assert registry.stage_names == (
        "source_prep",
        "context_prepare",
        "vlm",
        "stage1_narrative",
        "stage2_portfolio",
        "stage3_blueprint",
        "media_preflight",
        "stage4_recipe",
    )
    for name in registry.stage_names:
        assert runtime.worker._reconciler._require(name) is registry.require(name)
    stage4 = registry.require("stage4_recipe")
    assert type(stage4) is Stage4RecipePipelineStage
    assert stage4._authority_profile is stage4_authority
    assert stage4._media_stage is registry.require("media_preflight")
    assert stage4._cpu_authority_resolver is media_authority
    assert stage4._cuda_authority_resolver is not None
    assert not {"render", "qc", "render_qc"} & set(registry.stage_names)


def test_v12_missing_stage4_authority_stops_before_store_or_provider_construction(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from auto_cut_bot.pipeline.runtime import composition

    media = InstalledLocalRunProfileResolver(synthetic_installed_resource())
    monkeypatch.setattr(composition, "load_installed_local_run_resolver", lambda: media)

    def unavailable() -> Stage4RecipeAuthorityProfile:
        raise Stage4RecipeAuthorityError("installed Stage 4 authority is unavailable")

    def forbidden(*args: object, **kwargs: object) -> object:
        pytest.fail("invalid Stage 4 authority reached side-effecting composition")

    monkeypatch.setattr(composition, "load_installed_stage4_recipe_authority", unavailable)
    monkeypatch.setattr(composition, "PostgresRuntimeStore", forbidden)
    monkeypatch.setattr(composition, "DoubaoArkVlmProvider", forbidden)
    environment = _v12_environment(tmp_path)

    with pytest.raises(PipelineRuntimeConfigurationError, match="semantic-story-media"):
        compose_pipeline_runtime_from_environment(environment)


def test_v12_missing_media_configuration_stops_before_authority_or_side_effects(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from auto_cut_bot.pipeline.runtime import composition

    def forbidden(*args: object, **kwargs: object) -> object:
        pytest.fail("incomplete V12 configuration reached authority or side-effecting composition")

    monkeypatch.setattr(composition, "load_installed_stage4_recipe_authority", forbidden)
    monkeypatch.setattr(composition, "PostgresRuntimeStore", forbidden)
    monkeypatch.setattr(composition, "DoubaoArkVlmProvider", forbidden)
    environment = _v12_environment(tmp_path)
    del environment[PIPELINE_MEDIA_PREFLIGHT_POLICY_ENV]

    with pytest.raises(PipelineRuntimeConfigurationError, match="incomplete"):
        compose_pipeline_runtime_from_environment(environment)


def test_v12_stage4_authority_has_no_environment_override(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_v12_authorities(monkeypatch)
    environment = _v12_environment(tmp_path)
    environment["AUTO_CUT_BOT_PIPELINE_STAGE4_RECIPE_AUTHORITY_JSON"] = "not-an-authority"

    runtime = compose_pipeline_runtime_from_environment(environment)

    assert runtime is not None
    assert runtime.execution_profile.is_semantic_story_media
