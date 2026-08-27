"""Closed v10 thinking parameters and immutable history on disposable PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from auto_cut_bot.pipeline.runtime import (
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineStageResult,
    PostgresPipelineRunStore,
)
from auto_cut_bot.pipeline.vlm.request_factory import (
    DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION,
)
from tests.pipeline.runtime_profile_fixture import execution_profile

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set AUTOCUT_TEST_POSTGRES_DSN to a disposable PostgreSQL database",
)
MIGRATION = Path("packages/autocut-kernel/migrations/0029_vlm_explicit_thinking.sql")
EXPLICIT_ADAPTER = "doubao-ark-files-responses-stream-v5"


@pytest.fixture
def database_before_thinking() -> None:
    assert DSN is not None
    from psycopg.conninfo import conninfo_to_dict

    database_name = conninfo_to_dict(DSN).get("dbname", "")
    if not any(
        database_name.startswith(prefix) and len(database_name) > len(prefix)
        for prefix in ("autocut_test_", "autocut_resume_check_")
    ):
        pytest.fail(
            "PostgreSQL schema-reset tests require a dedicated database named "
            "autocut_test_<name> or autocut_resume_check_<name>; no schemas were changed"
        )
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        for migration in sorted(MIGRATION.parent.glob("*.sql")):
            if migration.name >= MIGRATION.name:
                break
            cursor.execute(migration.read_text(encoding="utf-8"))


def _apply_thinking_migration() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(MIGRATION.read_text(encoding="utf-8"))


@pytest.fixture
def migrated_database(database_before_thinking: None) -> None:
    _apply_thinking_migration()


def _semantic_mapping(adapter: str = EXPLICIT_ADAPTER) -> dict[str, object]:
    full = execution_profile()
    profile = PipelineExecutionProfile.from_semantic_policies(
        full.to_doubao_policy(), retry_policy=full.to_generation_retry_policy(),
    )
    mapping = profile.to_mapping()
    mapping["adapter_strategy_version"] = adapter
    if adapter == "doubao-ark-files-responses-stream-v2":
        mapping["vlm_stage_strategy_version"] = DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION
    elif adapter == "doubao-ark-files-responses-stream-v3":
        mapping["vlm_stage_strategy_version"] = DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION
    parameters = mapping["request_parameters"]
    assert isinstance(parameters, dict)
    parameters["adapter_strategy_version"] = adapter
    if adapter == EXPLICIT_ADAPTER:
        parameters["thinking_type"] = "disabled"
    return mapping


def _insert_profile(cursor, mapping: dict[str, object]) -> str:
    raw = json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    profile_hash = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    run_id = "pipeline_run_" + uuid4().hex
    request = PipelineRunRequest("test", source_reference="synthetic-source")
    cursor.execute(
        """INSERT INTO runtime.pipeline_runs
            (run_id, idempotency_key, request_hash, profile, source_kind,
             source_value, execution_profile, execution_profile_hash, state, version)
        VALUES (%s, %s, %s, 'test', 'reference', %s, %s::jsonb, %s, 'accepted', 0)""",
        (run_id, uuid4().hex, request.request_hash, request.source_reference, raw, profile_hash),
    )
    return run_id


def _assert_rejected(mapping: dict[str, object]) -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted') IS TRUE",
            (json.dumps(mapping),),
        )
        assert cursor.fetchone() == (False,)
        with pytest.raises(psycopg.errors.CheckViolation, match="pipeline_runs_execution_profile_closed_check"):
            _insert_profile(cursor, mapping)
        cursor.execute("SELECT count(*) FROM runtime.pipeline_runs")
        assert cursor.fetchone() == (0,)


@pytest.mark.parametrize("mode", ["enabled", "disabled", "auto"])
def test_v5_accepts_each_explicit_thinking_mode(migrated_database: None, mode: str) -> None:
    assert DSN is not None
    mapping = _semantic_mapping()
    parameters = mapping["request_parameters"]
    assert isinstance(parameters, dict)
    parameters["thinking_type"] = mode
    profile = PipelineExecutionProfile.from_mapping(mapping)
    assert profile.to_doubao_policy().thinking_type == mode
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted')",
            (json.dumps(mapping),),
        )
        assert cursor.fetchone() == (True,)
        run_id = _insert_profile(cursor, mapping)
        cursor.execute(
            "SELECT execution_profile, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        assert cursor.fetchone() == (mapping, profile.canonical_hash)


@pytest.mark.parametrize("mode", [None, True, False, 0, 1.0, [], {}, "", "Disabled", " disabled", "unknown"])
def test_v5_rejects_wrong_typed_or_unknown_mode(migrated_database: None, mode: object) -> None:
    mapping = _semantic_mapping()
    parameters = mapping["request_parameters"]
    assert isinstance(parameters, dict)
    parameters["thinking_type"] = mode
    _assert_rejected(mapping)


@pytest.mark.parametrize("mutation", [
    "missing_mode", "missing_base", "extra", "mismatched_adapter", "top_level_mode",
    "null_profile_adapter", "null_parameter_adapter", "missing_profile_adapter",
    "missing_parameter_adapter", "null_parameters",
])
def test_v5_closes_parameter_keys_and_adapter_identity(migrated_database: None, mutation: str) -> None:
    mapping = _semantic_mapping()
    parameters = mapping["request_parameters"]
    assert isinstance(parameters, dict)
    if mutation == "missing_mode":
        del parameters["thinking_type"]
    elif mutation == "missing_base":
        del parameters["video_fps"]
    elif mutation == "extra":
        parameters["thinking"] = {"type": "disabled"}
    elif mutation == "mismatched_adapter":
        parameters["adapter_strategy_version"] = "doubao-ark-files-responses-stream-v4"
    elif mutation == "top_level_mode":
        mapping["thinking_type"] = parameters.pop("thinking_type")
    elif mutation == "null_profile_adapter":
        mapping["adapter_strategy_version"] = None
    elif mutation == "null_parameter_adapter":
        parameters["adapter_strategy_version"] = None
    elif mutation == "missing_profile_adapter":
        del mapping["adapter_strategy_version"]
    elif mutation == "missing_parameter_adapter":
        del parameters["adapter_strategy_version"]
    else:
        mapping["request_parameters"] = None
    _assert_rejected(mapping)


@pytest.mark.parametrize("adapter", [
    "doubao-ark-files-responses-stream-v2", "doubao-ark-files-responses-stream-v3",
    "doubao-ark-files-responses-stream-v4",
])
@pytest.mark.parametrize("with_thinking", [False, True])
def test_old_adapters_keep_exact_four_parameter_shape(
    migrated_database: None, adapter: str, with_thinking: bool,
) -> None:
    assert DSN is not None
    mapping = _semantic_mapping(adapter)
    if with_thinking:
        parameters = mapping["request_parameters"]
        assert isinstance(parameters, dict)
        parameters["thinking_type"] = "disabled"
        _assert_rejected(mapping)
    else:
        with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
            _insert_profile(cursor, mapping)


@pytest.mark.parametrize("adapter_location", ["profile", "parameters", "both"])
@pytest.mark.parametrize("mode", ["missing", None, "enabled", "disabled", "auto"])
def test_v9_rejects_v5_with_or_without_thinking(
    migrated_database: None, adapter_location: str, mode: str | None,
) -> None:
    mapping = execution_profile().to_mapping()
    parameters = mapping["request_parameters"]
    assert isinstance(parameters, dict)
    if adapter_location in ("profile", "both"):
        mapping["adapter_strategy_version"] = EXPLICIT_ADAPTER
    if adapter_location in ("parameters", "both"):
        parameters["adapter_strategy_version"] = EXPLICIT_ADAPTER
    if mode != "missing":
        parameters["thinking_type"] = mode
    _assert_rejected(mapping)


def _frozen_history(cursor):
    cursor.execute(
        """SELECT
            (SELECT jsonb_agg(to_jsonb(run) ORDER BY run_id)::text FROM runtime.pipeline_runs run),
            (SELECT jsonb_agg(to_jsonb(command) ORDER BY command_id)::text FROM runtime.pipeline_commands command),
            (SELECT jsonb_agg(to_jsonb(receipt) ORDER BY receipt_id)::text FROM runtime.pipeline_run_receipts receipt),
            (SELECT jsonb_agg(to_jsonb(outbox) ORDER BY outbox_id)::text FROM runtime.pipeline_run_outbox outbox),
            pg_get_functiondef('runtime.execution_profile_semantic_v9_is_valid(jsonb,text)'::regprocedure),
            pg_get_functiondef('runtime.guard_historical_execution_profile_write()'::regprocedure)"""
    )
    return cursor.fetchone()


def test_migration_preserves_original_v3_v4_and_v9_history(database_before_thinking: None) -> None:
    assert DSN is not None
    store = PostgresPipelineRunStore(lambda: psycopg.connect(DSN))
    request = PipelineRunRequest("test", source_reference="synthetic-source")
    profiles = (
        PipelineExecutionProfile.from_mapping(_semantic_mapping("doubao-ark-files-responses-stream-v3")),
        PipelineExecutionProfile.from_mapping(_semantic_mapping("doubao-ark-files-responses-stream-v4")),
        execution_profile(),
    )
    for profile in profiles:
        run_id = "pipeline_run_" + uuid4().hex
        store._claim_run_sync(run_id, uuid4().hex, request, request.request_hash, profile)
        command = store._claim_next_pending_sync(run_id, 0, "source")
        assert command is not None and command.stage == "source_prep"
        store._record_result_sync(
            run_id, PipelineStageResult(command.command_id, "succeeded", uuid4()),
            command.version, "source",
        )
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        before = _frozen_history(cursor)
        assert before is not None and all(value is not None for value in before)
        cursor.execute(
            "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted') IS TRUE",
            (json.dumps(_semantic_mapping()),),
        )
        assert cursor.fetchone() == (False,)
        cursor.execute(MIGRATION.read_text(encoding="utf-8"))
        assert _frozen_history(cursor) == before
        cursor.execute(
            "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted')",
            (json.dumps(_semantic_mapping()),),
        )
        assert cursor.fetchone() == (True,)
        cursor.execute(MIGRATION.read_text(encoding="utf-8"))
        assert _frozen_history(cursor) == before
