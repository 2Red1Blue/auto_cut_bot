"""V4 is a semantic-only contract; old profiles and reuse hashes remain frozen."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from autocut_kernel.vlm import GenerationRetryPolicy
from autocut_kernel.vlm.models import VlmValidationError
from autocut_kernel.vlm.parser_contract import vlm_parser_contract_sha256
from autocut_kernel.vlm.reuse_identity import VlmSemanticPolicyIdentityV1
from autocut_kernel.vlm.semantic_contracts import VLM_PARSER_V4, parser_contract_sha256_for

from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.models import PipelineExecutionProfile
from auto_cut_bot.pipeline.vlm.doubao_ark_provider import (
    DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
)
from auto_cut_bot.pipeline.vlm.request_factory import (
    DOUBAO_VLM_VIDEO_STAGE_STRATEGY_VERSION,
    DoubaoVlmRequestPolicy,
)
from auto_cut_bot.pipeline.vlm.video_prompt import (
    VLM_VIDEO_PROMPT_VERSION,
    vlm_video_response_schema_json,
)
from tests.pipeline.runtime_profile_fixture import execution_profile
from tests.vlm.test_reuse_identity import _TEMPLATE, _identity, _ProviderScope, _request

MIGRATION = Path("packages/autocut-kernel/migrations/0030_vlm_video_semantic_profile.sql")
DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _video_policy(mode: str = "disabled") -> DoubaoVlmRequestPolicy:
    return DoubaoVlmRequestPolicy(
        model_id="doubao-seed-2-1-pro-260628",
        adapter_strategy_version=DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
        prompt_version=VLM_VIDEO_PROMPT_VERSION,
        parser_strategy_version=VLM_PARSER_V4,
        parser_contract_sha256=parser_contract_sha256_for(VLM_PARSER_V4),
        response_schema_json=vlm_video_response_schema_json(),
        stage_strategy_version=DOUBAO_VLM_VIDEO_STAGE_STRATEGY_VERSION,
        thinking_type=mode,
    )


def _video_mapping(mode: str = "disabled") -> dict[str, Any]:
    return PipelineExecutionProfile.from_semantic_policies(
        _video_policy(mode), retry_policy=GenerationRetryPolicy("generation-retry-v1", 1, ()),
    ).to_mapping()


def _historical_mapping(version: int) -> dict[str, Any]:
    result = execution_profile().to_mapping()
    result["schema_version"] = f"pipeline-execution-profile-v{version}"
    for minimum_version, field in (
        (9, "evidence_read_limits"), (8, "stage3_command_policy"),
        (7, "stage2_command_policy"), (6, "stage1_command_policy"),
        (5, "materialization_limits"), (3, "media_preflight_policy"),
        (3, "media_preflight_policy_hash"), (2, "generation_retry_policy"),
    ):
        if version < minimum_version:
            result.pop(field)
    if version < 4:
        result["parse_policy"] = {
            "max_observations": 64, "max_response_bytes": 64_000,
            "max_summary_characters": 512, "max_total_summary_characters": 8_192,
            "minimum_confidence": "0.80",
        }
    return result


@pytest.mark.parametrize("mode", ["enabled", "disabled", "auto"])
def test_video_v10_roundtrips_exact_registered_policy(mode: str) -> None:
    mapping = _video_mapping(mode)
    profile = PipelineExecutionProfile.from_mapping(mapping)
    assert profile.is_semantic_only
    assert not profile.has_media_preflight_policy
    assert profile.to_mapping() == mapping
    assert profile.to_doubao_policy() == _video_policy(mode)
    assert profile.parser_contract_sha256 == parser_contract_sha256_for(VLM_PARSER_V4)
    assert profile.canonical_json == _canonical(mapping)
    with pytest.raises(PipelineRunValidationError):
        profile.build_stage1_command_policy()


def test_video_modes_are_distinct_semantic_profiles() -> None:
    assert len({PipelineExecutionProfile.from_mapping(_video_mapping(mode)).canonical_hash
                for mode in ("enabled", "disabled", "auto")}) == 3


@pytest.mark.parametrize("version", range(1, 10))
def test_historical_profiles_keep_exact_mapping_and_hash(version: int) -> None:
    mapping = _historical_mapping(version)
    original_bytes = _canonical(mapping).encode("utf-8")
    profile = PipelineExecutionProfile.from_mapping(mapping)
    assert profile.to_mapping() == mapping
    assert profile.canonical_json.encode("utf-8") == original_bytes
    assert profile.canonical_hash == "sha256:" + hashlib.sha256(original_bytes).hexdigest()
    assert profile.kernel_parser_strategy_version == "strict-semantic-pack-v3"
    assert profile.parser_contract_sha256 is None
    assert "parser_contract_sha256" not in mapping
    if version == 9:
        assert profile.canonical_hash == "sha256:8fff3bd9acec4bcfa1935eec441e08b318b672ae9edde96b93022c5ce3c6b7f8"


@pytest.mark.parametrize("version", range(1, 10))
def test_historical_profile_rejects_v4_even_with_legacy_adapter(version: int) -> None:
    mapping = _historical_mapping(version)
    mapping["kernel_parser_strategy_version"] = VLM_PARSER_V4
    assert mapping["adapter_strategy_version"] == "doubao-ark-files-responses-stream-v4"
    with pytest.raises(PipelineRunValidationError, match="requires semantic-only execution profile v10"):
        PipelineExecutionProfile.from_mapping(mapping)


@pytest.mark.parametrize("mutation", [
    "old_parser", "old_prompt", "old_stage", "old_schema", "old_adapter", "unknown_parser",
    "missing_thinking", "extra_thinking", "physical_policy", "unknown_field",
])
def test_v10_rejects_mixed_video_contract(mutation: str) -> None:
    mapping = _video_mapping()
    old = execution_profile().to_mapping()
    if mutation in {"old_parser", "old_prompt", "old_stage", "old_schema", "old_adapter"}:
        field = {
            "old_parser": "kernel_parser_strategy_version", "old_prompt": "prompt_version",
            "old_stage": "vlm_stage_strategy_version", "old_schema": "response_schema",
            "old_adapter": "adapter_strategy_version",
        }[mutation]
        mapping[field] = old[field]
        if mutation == "old_adapter":
            mapping["request_parameters"]["adapter_strategy_version"] = old[field]
            mapping["request_parameters"].pop("thinking_type")
    elif mutation == "unknown_parser":
        mapping["kernel_parser_strategy_version"] = "strict-semantic-pack-v99"
    elif mutation == "missing_thinking":
        mapping["request_parameters"].pop("thinking_type")
    elif mutation == "extra_thinking":
        mapping["request_parameters"]["thinking"] = {"type": "disabled"}
    elif mutation == "physical_policy":
        mapping["media_preflight_policy"] = old["media_preflight_policy"]
    else:
        mapping["accept_video_support"] = True
    with pytest.raises(PipelineRunValidationError):
        PipelineExecutionProfile.from_mapping(mapping)


def test_legacy_v10_still_rebuilds_exact_v3_policy() -> None:
    old = execution_profile()
    profile = PipelineExecutionProfile.from_semantic_policies(
        old.to_doubao_policy(), retry_policy=old.to_generation_retry_policy(),
    )
    restored = PipelineExecutionProfile.from_mapping(profile.to_mapping())
    assert restored == profile
    assert restored.canonical_hash == profile.canonical_hash
    assert restored.to_doubao_policy() == old.to_doubao_policy()
    assert "parser_contract_sha256" not in restored.to_mapping()


@pytest.mark.parametrize("digest", [None, True, 1, {}, [], "", "sha256:" + "A" * 64, "sha256:" + "a" * 64])
def test_v4_profile_rejects_missing_wrong_typed_or_stale_implementation(digest: object) -> None:
    mapping = _video_mapping()
    mapping["parser_contract_sha256"] = digest
    with pytest.raises(PipelineRunValidationError):
        PipelineExecutionProfile.from_mapping(mapping)
    mapping.pop("parser_contract_sha256")
    with pytest.raises(PipelineRunValidationError, match="missing fields"):
        PipelineExecutionProfile.from_mapping(mapping)


@pytest.mark.parametrize("version", range(1, 11))
def test_v3_profile_rejects_implementation_field_even_if_null(version: int) -> None:
    if version == 10:
        old = execution_profile()
        mapping = PipelineExecutionProfile.from_semantic_policies(
            old.to_doubao_policy(), retry_policy=old.to_generation_retry_policy(),
        ).to_mapping()
    else:
        mapping = _historical_mapping(version)
    mapping["parser_contract_sha256"] = None
    with pytest.raises(PipelineRunValidationError, match="unsupported fields"):
        PipelineExecutionProfile.from_mapping(mapping)


def test_v3_reuse_identity_keeps_fixed_original_hashes() -> None:
    identity = _identity(_request())
    assert identity.semantic_policy.parser_contract_sha256 is None
    assert "parser_contract_sha256" not in identity.semantic_policy.to_mapping()
    assert identity.semantic_policy.canonical_hash == "sha256:f976fa62c813ecc88a7ddb2b8d2f88e8175d083b763a3fbb5bf9039cd2ddd5a2"
    assert identity.canonical_hash == "sha256:e9d905885f7445f207733c1c07c72bf457835fbe0578a68c196a3b08078afb38"
    assert parser_contract_sha256_for("strict-semantic-pack-v3") == vlm_parser_contract_sha256()


def test_v4_reuse_identity_binds_installed_parser_implementation() -> None:
    request = replace(
        _request(), parser_strategy_version=VLM_PARSER_V4,
        response_schema_json=vlm_video_response_schema_json(),
        parser_contract_sha256=parser_contract_sha256_for(VLM_PARSER_V4),
    )
    identity = _identity(request)
    expected = parser_contract_sha256_for(request.parser_strategy_version)
    assert identity.semantic_policy.parser_contract_sha256 == expected
    assert identity.semantic_policy.to_mapping()["parser_contract_sha256"] == expected
    assert expected != vlm_parser_contract_sha256()
    assert identity.canonical_hash != _identity(_request()).canonical_hash
    with pytest.raises(VlmValidationError, match="differs"):
        # A caller-asserted digest cannot replace the registered implementation.
        replace(identity.semantic_policy, parser_contract_sha256="sha256:" + "a" * 64)


def test_v4_parser_bundle_change_invalidates_reuse_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    request = replace(
        _request(), parser_strategy_version=VLM_PARSER_V4,
        response_schema_json=vlm_video_response_schema_json(),
        parser_contract_sha256=parser_contract_sha256_for(VLM_PARSER_V4),
    )
    original = _identity(request).semantic_policy
    monkeypatch.setattr(
        "autocut_kernel.vlm.reuse_identity.parser_contract_sha256_for", lambda _: "sha256:" + "a" * 64,
    )
    with pytest.raises(VlmValidationError, match="different parser implementation"):
        VlmSemanticPolicyIdentityV1.from_request(
            request, provider_scope=_ProviderScope(), prompt_template=_TEMPLATE,
        )
    assert original.parser_contract_sha256 == request.parser_contract_sha256


def test_video_migration_closes_old_adapter_bypass_without_rewriting_history() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION runtime.execution_profile_semantic_v10_is_valid" in sql
    legacy_branch = sql.split("ELSE runtime.execution_profile_semantic_v9_is_valid", 1)[1]
    assert "'kernel_parser_strategy_version') IS DISTINCT FROM\n             'strict-semantic-pack-v4'" in legacy_branch
    assert "CREATE OR REPLACE FUNCTION runtime.execution_profile_semantic_v9_is_valid" not in sql
    assert "UPDATE runtime.pipeline_runs" not in sql
    assert "profile_value ? 'parser_contract_sha256'" in sql
    assert "'^sha256:[0-9a-f]{64}$'" in sql
    for required in (VLM_PARSER_V4, VLM_VIDEO_PROMPT_VERSION, DOUBAO_VLM_VIDEO_STAGE_STRATEGY_VERSION):
        assert required in sql


@pytest.mark.skipif(not DSN, reason="requires coordinated disposable PostgreSQL database")
def test_postgres_video_migration_closes_versions_and_preserves_legacy_rows() -> None:
    """Run only after the shared disposable database has been released by its owner."""
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    from tests.pipeline.test_vlm_thinking_profile_postgres import _insert_profile

    assert DSN is not None
    database_name = conninfo_to_dict(DSN).get("dbname", "")
    if not any(database_name.startswith(prefix) and len(database_name) > len(prefix)
               for prefix in ("autocut_test_", "autocut_resume_check_")):
        pytest.fail("schema-reset tests require a dedicated autocut_test_<name> database")
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        for migration in sorted(MIGRATION.parent.glob("*.sql")):
            if migration.name >= MIGRATION.name:
                break
            cursor.execute(migration.read_text(encoding="utf-8"))
        old = execution_profile()
        legacy_video = PipelineExecutionProfile.from_semantic_policies(
            old.to_doubao_policy(), retry_policy=old.to_generation_retry_policy(),
        )
        for profile in (old, legacy_video):
            _insert_profile(cursor, profile.to_mapping())
        snapshot_sql = """SELECT
            (SELECT jsonb_agg(to_jsonb(run) ORDER BY run_id)::text FROM runtime.pipeline_runs run),
            pg_get_functiondef('runtime.execution_profile_semantic_v9_is_valid(jsonb,text)'::regprocedure),
            pg_get_functiondef('runtime.guard_historical_execution_profile_write()'::regprocedure)"""
        cursor.execute(snapshot_sql)
        before = cursor.fetchone()
        cursor.execute(MIGRATION.read_text(encoding="utf-8"))
        cursor.execute(snapshot_sql)
        assert cursor.fetchone() == before
        for mode in ("enabled", "disabled", "auto"):
            mapping = _video_mapping(mode)
            cursor.execute(
                "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted') IS TRUE",
                (json.dumps(mapping),),
            )
            assert cursor.fetchone() == (True,)
            _insert_profile(cursor, mapping)
        for version in range(1, 10):
            mapping = _historical_mapping(version)
            mapping["kernel_parser_strategy_version"] = VLM_PARSER_V4
            cursor.execute(
                "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted') IS TRUE",
                (json.dumps(mapping),),
            )
            assert cursor.fetchone() == (False,)
        forged_v9 = _historical_mapping(9)
        forged_v9["kernel_parser_strategy_version"] = VLM_PARSER_V4
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_profile(cursor, forged_v9)
        old_mapping = cast(dict[str, Any], legacy_video.to_mapping())
        for field in ("kernel_parser_strategy_version", "prompt_version", "vlm_stage_strategy_version", "response_schema"):
            mixed = _video_mapping()
            mixed[field] = old_mapping[field]
            cursor.execute(
                "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted') IS TRUE",
                (json.dumps(mixed),),
            )
            assert cursor.fetchone() == (False,)
        for digest in (None, True, 1, {}, [], "", "sha256:" + "A" * 64):
            malformed = _video_mapping()
            malformed["parser_contract_sha256"] = digest
            cursor.execute(
                "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted') IS TRUE",
                (json.dumps(malformed),),
            )
            assert cursor.fetchone() == (False,)
        missing = _video_mapping()
        missing.pop("parser_contract_sha256")
        cursor.execute(
            "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted') IS TRUE",
            (json.dumps(missing),),
        )
        assert cursor.fetchone() == (False,)
        for legacy in (old.to_mapping(), legacy_video.to_mapping()):
            legacy["parser_contract_sha256"] = None
            cursor.execute(
                "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted') IS TRUE",
                (json.dumps(legacy),),
            )
            assert cursor.fetchone() == (False,)
