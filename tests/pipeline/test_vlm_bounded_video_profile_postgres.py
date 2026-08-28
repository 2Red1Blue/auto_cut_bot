"""Additive bounded-prompt admission preserves original profiles and validators."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from auto_cut_bot.pipeline.runtime.models import PipelineExecutionProfile
from tests.pipeline.runtime_profile_fixture import execution_profile
from tests.pipeline.test_vlm_bounded_video_profile import _bounded_profile
from tests.pipeline.test_vlm_thinking_profile_postgres import _insert_profile
from tests.pipeline.test_vlm_video_profile import _historical_mapping, _video_mapping

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires coordinated disposable PostgreSQL database")
MIGRATION = Path("packages/autocut-kernel/migrations/0031_vlm_bounded_video_prompt.sql")
PROMPT = "vlm-semantic-pack-v6-bounded-references"
VALIDATOR = "runtime.execution_profile_bounded_video_prompt_is_valid"


def _bounded_mapping(mode: str = "disabled") -> dict[str, Any]:
    # Exercise actual registered bounded-schema bytes, not a relabelled v5
    # fixture; SQL checks its envelope and Runtime closes the complete schema.
    return _bounded_profile(mode).to_mapping()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@pytest.fixture(scope="module")
def migrated_database() -> str:
    from psycopg.conninfo import conninfo_to_dict

    assert DSN is not None
    database_name = conninfo_to_dict(DSN).get("dbname", "")
    if not any(database_name.startswith(prefix) and len(database_name) > len(prefix)
               for prefix in ("autocut_test_", "autocut_resume_check_")):
        pytest.fail("schema-reset tests require a dedicated autocut_test_<name> or autocut_resume_check_<name> database")
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        for migration in sorted(MIGRATION.parent.glob("*.sql")):
            if migration.name >= MIGRATION.name:
                break
            cursor.execute(migration.read_text(encoding="utf-8"))
        old_full = execution_profile()
        old_v3_semantic = PipelineExecutionProfile.from_semantic_policies(
            old_full.to_doubao_policy(), retry_policy=old_full.to_generation_retry_policy(),
        )
        for mapping in (old_full.to_mapping(), old_v3_semantic.to_mapping(), _video_mapping()):
            _insert_profile(cursor, mapping)
        snapshot = """SELECT
            (SELECT jsonb_agg(to_jsonb(run) ORDER BY run_id)::text FROM runtime.pipeline_runs run),
            (SELECT jsonb_agg(to_jsonb(command) ORDER BY command_id)::text FROM runtime.pipeline_commands command),
            (SELECT jsonb_agg(to_jsonb(receipt) ORDER BY receipt_id)::text FROM runtime.pipeline_run_receipts receipt),
            (SELECT jsonb_agg(to_jsonb(outbox) ORDER BY outbox_id)::text FROM runtime.pipeline_run_outbox outbox),
            pg_get_functiondef('runtime.execution_profile_semantic_v10_is_valid(jsonb,text)'::regprocedure),
            pg_get_functiondef('runtime.execution_profile_semantic_v9_is_valid(jsonb,text)'::regprocedure),
            pg_get_functiondef('runtime.guard_historical_execution_profile_write()'::regprocedure)"""
        cursor.execute(snapshot)
        before = cursor.fetchone()
        cursor.execute(
            "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted') IS TRUE",
            (json.dumps(_bounded_mapping()),),
        )
        assert cursor.fetchone() == (False,)
        cursor.execute(MIGRATION.read_text(encoding="utf-8"))
        cursor.execute(snapshot)
        assert cursor.fetchone() == before
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'pipeline_runs_execution_profile_closed_check'",
        )
        definition = cursor.fetchone()
        assert definition is not None
        assert "execution_profile_bounded_video_prompt_is_valid" in definition[0]
        assert "IS TRUE" in definition[0]
    return DSN


@pytest.mark.parametrize("mode", ["enabled", "disabled", "auto"])
def test_bounded_prompt_admits_original_profile_without_projection_writeback(
    migrated_database: str, mode: str,
) -> None:
    mapping = _bounded_mapping(mode)
    raw = _canonical(mapping)
    digest = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with psycopg.connect(migrated_database, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT {VALIDATOR}(%s::jsonb, 'accepted') IS TRUE", (raw,))
        assert cursor.fetchone() == (True,)
        run_id = _insert_profile(cursor, mapping)
        cursor.execute(
            "SELECT execution_profile, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        assert cursor.fetchone() == (mapping, digest)
        # The old function remains an unmodified, independently usable contract.
        cursor.execute(
            "SELECT runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted') IS TRUE",
            (raw,),
        )
        assert cursor.fetchone() == (False,)


@pytest.mark.parametrize("version", range(1, 10))
def test_bounded_prompt_cannot_enter_pre_v10_profile(migrated_database: str, version: int) -> None:
    mapping = _historical_mapping(version)
    mapping["prompt_version"] = PROMPT
    with psycopg.connect(migrated_database, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT {VALIDATOR}(%s::jsonb, 'accepted') IS TRUE", (json.dumps(mapping),))
        assert cursor.fetchone() == (False,)
        if version == 9:
            with pytest.raises(psycopg.errors.CheckViolation, match="pipeline_runs_execution_profile_closed_check"):
                _insert_profile(cursor, mapping)


@pytest.mark.parametrize("mutation", [
    "missing_hash", "null_hash", "wrong_hash_type", "uppercase_hash", "short_hash",
    "provider_mismatch", "null_provider", "missing_provider", "old_parser", "old_stage",
    "old_adapter", "parameter_adapter_mismatch", "old_schema", "float_schema", "bool_schema",
    "missing_thinking", "bad_thinking", "extra_parameter", "extra_field", "physical_policy",
])
def test_bounded_prompt_retains_full_original_contract(migrated_database: str, mutation: str) -> None:
    mapping = _bounded_mapping()
    legacy = execution_profile().to_mapping()
    if mutation == "missing_hash":
        mapping.pop("parser_contract_sha256")
    elif mutation in {"null_hash", "wrong_hash_type", "uppercase_hash", "short_hash"}:
        mapping["parser_contract_sha256"] = {
            "null_hash": None, "wrong_hash_type": 123,
            "uppercase_hash": "sha256:" + "A" * 64, "short_hash": "sha256:123",
        }[mutation]
    elif mutation == "provider_mismatch":
        mapping["provider_id"] = "another-provider"
    elif mutation == "null_provider":
        mapping["provider_id"] = None
    elif mutation == "missing_provider":
        mapping.pop("provider_id")
    elif mutation in {"old_parser", "old_stage", "old_schema"}:
        field = {"old_parser": "kernel_parser_strategy_version", "old_stage": "vlm_stage_strategy_version",
                 "old_schema": "response_schema"}[mutation]
        mapping[field] = legacy[field]
    elif mutation == "old_adapter":
        mapping["adapter_strategy_version"] = legacy["adapter_strategy_version"]
        mapping["request_parameters"]["adapter_strategy_version"] = legacy["adapter_strategy_version"]
        mapping["request_parameters"].pop("thinking_type")
    elif mutation == "parameter_adapter_mismatch":
        mapping["request_parameters"]["adapter_strategy_version"] = legacy["adapter_strategy_version"]
    elif mutation in {"float_schema", "bool_schema"}:
        mapping["response_schema"]["properties"]["schema_version"]["const"] = (
            4.0 if mutation == "float_schema" else True
        )
    elif mutation == "missing_thinking":
        mapping["request_parameters"].pop("thinking_type")
    elif mutation == "bad_thinking":
        mapping["request_parameters"]["thinking_type"] = "unknown"
    elif mutation == "extra_parameter":
        mapping["request_parameters"]["thinking"] = {"type": "disabled"}
    elif mutation == "physical_policy":
        mapping["media_preflight_policy"] = legacy["media_preflight_policy"]
    else:
        mapping["trusted"] = True
    with psycopg.connect(migrated_database, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT {VALIDATOR}(%s::jsonb, 'accepted') IS TRUE", (json.dumps(mapping),))
        assert cursor.fetchone() == (False,)
        with pytest.raises(psycopg.errors.CheckViolation, match="pipeline_runs_execution_profile_closed_check"):
            _insert_profile(cursor, mapping)


@pytest.mark.parametrize("mutation", ["unchanged", "invalid_hash", "provider_mismatch", "extra_field", "missing_prompt"])
def test_old_video_prompt_retains_exact_validator_behavior(migrated_database: str, mutation: str) -> None:
    mapping = _video_mapping()
    if mutation == "invalid_hash":
        mapping["parser_contract_sha256"] = None
    elif mutation == "provider_mismatch":
        mapping["provider_id"] = "another-provider"
    elif mutation == "extra_field":
        mapping["trusted"] = True
    elif mutation == "missing_prompt":
        mapping.pop("prompt_version")
    original = copy.deepcopy(mapping)
    with psycopg.connect(migrated_database, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {VALIDATOR}(%s::jsonb, 'accepted'), "
            "runtime.execution_profile_semantic_v10_is_valid(%s::jsonb, 'accepted')",
            (json.dumps(mapping), json.dumps(mapping)),
        )
        row = cursor.fetchone()
        assert row is not None and row[0] == row[1]
    assert mapping == original


def test_nullable_validation_remains_fail_closed(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as connection, connection.cursor() as cursor:
        for value in (None, {}, {"prompt_version": PROMPT}):
            cursor.execute(f"SELECT {VALIDATOR}(%s::jsonb, 'accepted') IS TRUE", (json.dumps(value),))
            assert cursor.fetchone() == (False,)
