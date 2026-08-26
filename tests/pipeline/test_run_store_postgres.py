from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer

from auto_cut_bot.api.server import create_app
from auto_cut_bot.pipeline.runtime import (
    DurablePipelineRunService,
    IdempotencyConflictError,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineRunValidationError,
    PipelineStageContext,
    PipelineStageReconciler,
    PipelineStageResult,
    PostgresPipelineRunStore,
    PostgresPipelineScheduler,
    ResumeNotAllowedError,
    SourceDeniedError,
    StaleRunVersionError,
)
from auto_cut_bot.pipeline.runtime.composition import ConfiguredSourceCatalog, SourceCatalogEntry
from auto_cut_bot.pipeline.runtime.models import EvidenceReadLimits
from auto_cut_bot.pipeline.source_prep import (
    AuthorizedSeriesSourceRoot,
    SourceOperationPolicy,
)
from tests.pipeline.runtime_profile_fixture import (
    execution_profile as frozen_execution_profile,
)
from tests.pipeline.runtime_profile_fixture import media_preflight_policy, stage1_command_policy

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to a disposable PostgreSQL database",
)


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for migration in sorted(Path("packages/autocut-kernel/migrations").glob("*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("draft_policy", "max_response_bytes"), None),
        (("context_policy", "max_batch_context_bytes"), None),
        (("generation", "model_id"), None),
        (("generation", "max_output_tokens"), 0.5),
        (("draft_policy", "max_response_bytes"), 0.5),
        (("context_policy", "max_source_members"), True),
        (("feasibility_policy", "max_search_states"), -1),
        (("retry_policy", "max_attempts"), 0),
        (("retry_policy", "backoff_seconds"), [0.5]),
        (("retry_policy", "backoff_seconds"), [2**53]),
    ),
)
def test_0021_sql_guard_rejects_nested_stage3_policy_invalid_leaves(path, value) -> None:
    """Remote-only executable SQL guard; collection never contacts PostgreSQL."""
    assert DSN is not None
    profile = _v8_execution_profile().to_mapping()
    nested = profile["stage3_command_policy"]
    for key in path[:-1]:
        nested = nested[key]
    nested[path[-1]] = value
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT runtime.execution_profile_semantic_v8_is_valid(%s::jsonb, 'accepted')",
            (_v8_execution_profile().canonical_json,),
        )
        assert cursor.fetchone() == (True,)
        cursor.execute(
            "SELECT runtime.execution_profile_semantic_v8_is_valid(%s::jsonb, 'accepted')",
            (json.dumps(profile),),
        )
        assert cursor.fetchone() == (False,)


def _composition(source_root: Path, *additional_source_roots: Path):
    assert DSN is not None

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory)
    scheduler = PostgresPipelineScheduler(factory)
    authority = _source_catalog(source_root / "input", *additional_source_roots)
    return (
        DurablePipelineRunService(
            store,
            scheduler,
            authority,
            execution_profile=_execution_profile(),
        ),
        store,
        scheduler,
    )


def _source_catalog(
    source_root: Path,
    *additional_source_roots: Path,
) -> ConfiguredSourceCatalog:
    roots = (source_root, *additional_source_roots)
    return ConfiguredSourceCatalog(
        tuple(
            SourceCatalogEntry(
                AuthorizedSeriesSourceRoot(
                    root=root.resolve(),
                    policy=SourceOperationPolicy(
                        f"source:fixture-{index}",
                        f"series:fixture-{index}",
                        1,
                        ("semantic_analysis", "render_source"),
                    ),
                )
            )
            for index, root in enumerate(roots, start=1)
        )
    )


def _execution_profile(
    *,
    model_id: str = "doubao-seed-2-1-pro-260628",
    asr_model_revision: str = "v1.0.0",
    stage1_revision: int = 1,
) -> PipelineExecutionProfile:
    return frozen_execution_profile(
        model_id=model_id,
        media_policy=media_preflight_policy(
            asr_model_revision=asr_model_revision,
            timed_speech_service_sha256="sha256:" + "2" * 64,
        ),
        stage1_policy=replace(stage1_command_policy(), artifact_revision=stage1_revision),
    )


def _historical_execution_profile(
    schema_version: str,
) -> PipelineExecutionProfile:
    mapping = _execution_profile().to_mapping()
    mapping["schema_version"] = schema_version
    del mapping["stage1_command_policy"]
    del mapping["stage2_command_policy"]
    del mapping["stage3_command_policy"]
    del mapping["evidence_read_limits"]
    mapping["parse_policy"] = {
        "max_observations": 64,
        "max_response_bytes": 64_000,
        "max_summary_characters": 512,
        "max_total_summary_characters": 8_192,
        "minimum_confidence": "0.80",
    }
    del mapping["materialization_limits"]
    if schema_version in {
        "pipeline-execution-profile-v1",
        "pipeline-execution-profile-v2",
    }:
        del mapping["media_preflight_policy"]
        del mapping["media_preflight_policy_hash"]
    if schema_version == "pipeline-execution-profile-v1":
        del mapping["generation_retry_policy"]
    return PipelineExecutionProfile.from_mapping(mapping)


def _v4_execution_profile() -> PipelineExecutionProfile:
    mapping = _execution_profile().to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v4"
    del mapping["stage1_command_policy"]
    del mapping["stage2_command_policy"]
    del mapping["stage3_command_policy"]
    del mapping["evidence_read_limits"]
    del mapping["materialization_limits"]
    return PipelineExecutionProfile.from_mapping(mapping)


def _v5_execution_profile() -> PipelineExecutionProfile:
    mapping = _execution_profile().to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v5"
    del mapping["stage1_command_policy"]
    del mapping["stage2_command_policy"]
    del mapping["stage3_command_policy"]
    del mapping["evidence_read_limits"]
    return PipelineExecutionProfile.from_mapping(mapping)


def _v6_execution_profile() -> PipelineExecutionProfile:
    mapping = _execution_profile().to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v6"
    del mapping["stage2_command_policy"]
    del mapping["stage3_command_policy"]
    del mapping["evidence_read_limits"]
    return PipelineExecutionProfile.from_mapping(mapping)


def _v7_execution_profile() -> PipelineExecutionProfile:
    mapping = _execution_profile().to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v7"
    del mapping["stage3_command_policy"]
    del mapping["evidence_read_limits"]
    return PipelineExecutionProfile.from_mapping(mapping)


def _v8_execution_profile() -> PipelineExecutionProfile:
    mapping = _execution_profile().to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v8"
    del mapping["evidence_read_limits"]
    return PipelineExecutionProfile.from_mapping(mapping)


def _insert_profile_for_guard(cursor, mapping: object) -> str:
    """Raw SQL guard probe with matching bytes/hash, not a typed-policy bypass."""
    request = PipelineRunRequest("test", source_root="/migration-0019/synthetic")
    raw = json.dumps(mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    run_id = f"pipeline_run_{uuid4().hex}"
    cursor.execute(
        """INSERT INTO runtime.pipeline_runs
            (run_id, idempotency_key, request_hash, profile, source_kind,
             source_value, execution_profile, execution_profile_hash, state, version)
        VALUES (%s, %s, %s, 'test', 'root', %s, %s::jsonb, %s, 'accepted', 0)""",
        (run_id, f"migration-0019-{uuid4().hex}", request.request_hash, request.source_root,
         raw, "sha256:" + hashlib.sha256(raw.encode()).hexdigest()),
    )
    return run_id


@pytest.mark.parametrize(("path", "value"), [
    (("stage1_command_policy",), None),
    (("stage1_command_policy",), []),
    (("stage1_command_policy", "artifact_revision"), None),
    (("stage1_command_policy", "artifact_revision"), True),
    (("stage1_command_policy", "artifact_revision"), 1.0),
    (("stage1_command_policy", "artifact_revision"), 0),
    (("stage1_command_policy", "artifact_revision"), 2**53),
    (("stage1_command_policy", "generation", "max_output_tokens"), 32769),
    (("stage1_command_policy", "generation", "temperature"), "2.1"),
    (("stage1_command_policy", "draft_policy", "max_response_bytes"), -1),
    (("stage1_command_policy", "draft_policy", "max_input_windows"), "4"),
    (("stage1_command_policy", "retry_policy", "max_attempts"), 4),
    (("stage1_command_policy", "retry_policy", "backoff_seconds"), [2]),
    (("stage1_command_policy", "retry_policy", "backoff_seconds"), [False, 8]),
    (("stage1_command_policy", "retry_policy", "backoff_seconds"), [-1, 8]),
])
def test_0019_sql_guard_rejects_null_types_and_invalid_numeric_bounds(path, value) -> None:
    """Remote PostgreSQL acceptance probe; no real provider/model evidence."""
    assert DSN is not None
    mapping = _execution_profile().to_mapping()
    target = mapping
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.CheckViolation, match="pipeline_runs_execution_profile_closed_check"):
            _insert_profile_for_guard(cursor, mapping)


@pytest.mark.parametrize("path", [
    (), ("stage1_command_policy",), ("stage1_command_policy", "generation"),
    ("stage1_command_policy", "draft_policy"), ("stage1_command_policy", "coverage_policy"),
    ("stage1_command_policy", "dependency_policy"), ("stage1_command_policy", "retry_policy"),
])
@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_0019_sql_guard_closes_every_policy_object(path, mutation) -> None:
    assert DSN is not None
    mapping = _execution_profile().to_mapping()
    target = mapping
    for key in path:
        target = target[key]
    if mutation == "missing":
        # Keep v9 so the closed-policy CHECK, not the historical-version trigger, denies.
        target.pop("stage1_command_policy" if not path else next(iter(target)))
    else:
        target["unregistered"] = True
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.CheckViolation, match="pipeline_runs_execution_profile_closed_check"):
            _insert_profile_for_guard(cursor, mapping)


def test_current_sql_null_cannot_escape_closed_policy_guard() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT runtime.stage1_command_policy_shape_is_valid(NULL)")
        assert cursor.fetchone() == (False,)
        with pytest.raises(psycopg.errors.RaiseException, match="new pre-v9 execution profile rows are forbidden"):
            _insert_profile_for_guard(cursor, None)


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7, 8])
def test_current_sql_guard_rejects_new_old_profiles_even_with_valid_old_policy(version) -> None:
    assert DSN is not None
    profile = (
        _historical_execution_profile(f"pipeline-execution-profile-v{version}")
        if version <= 3 else _v4_execution_profile() if version == 4
        else _v5_execution_profile() if version == 5 else _v6_execution_profile() if version == 6
        else _v7_execution_profile() if version == 7 else _v8_execution_profile()
    )
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.RaiseException, match="new pre-v9 execution profile rows are forbidden"):
            _insert_profile_for_guard(cursor, profile.to_mapping())


def test_current_sql_guard_accepts_v9_but_freezes_complete_policy_and_hash() -> None:
    assert DSN is not None
    original = _execution_profile()
    changed = _execution_profile(stage1_revision=2)
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        run_id = _insert_profile_for_guard(cursor, original.to_mapping())
        cursor.execute(
            "SELECT runtime.execution_profile_semantic_v9_is_valid(%s::jsonb, 'accepted')",
            (original.canonical_json,),
        )
        assert cursor.fetchone() == (True,)
        with pytest.raises(psycopg.errors.RaiseException, match="pipeline run identity is immutable"):
            cursor.execute(
                """UPDATE runtime.pipeline_runs SET execution_profile = %s::jsonb,
                    execution_profile_hash = %s, version = version + 1,
                    updated_at = transaction_timestamp() WHERE run_id = %s""",
                (changed.canonical_json, changed.canonical_hash, run_id),
            )
        cursor.execute(
            "SELECT execution_profile, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        mapping, persisted_hash = cursor.fetchone()
        if isinstance(persisted_hash, bytes):
            persisted_hash = persisted_hash.decode()
        assert mapping == original.to_mapping()
        assert persisted_hash == original.canonical_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [EvidenceReadLimits(1, 1), EvidenceReadLimits(2**53 - 1, 2**53 - 1)])
async def test_0022_v9_budget_roundtrips_through_real_store_claim_and_replay(limits) -> None:
    """Remote-only Store integration, not an evidence reader/model acceptance test."""
    assert DSN is not None
    store = PostgresPipelineRunStore(lambda: psycopg.connect(DSN))
    profile = frozen_execution_profile(evidence_limits=limits)
    request = PipelineRunRequest("test", source_root="/migration-0022/synthetic")
    run_id = f"pipeline_run_{uuid4().hex}"
    key = f"migration-0022-{uuid4().hex}"
    claim = await store.claim_run(
        run_id=run_id, idempotency_key=key, request=request,
        request_hash=request.request_hash, execution_profile=profile,
    )
    replay = await store.claim_run(
        run_id=run_id, idempotency_key=key, request=request,
        request_hash=request.request_hash, execution_profile=profile,
    )
    assert not claim.replayed and replay.replayed
    assert replay.snapshot == claim.snapshot
    assert claim.snapshot.execution_profile == profile
    assert claim.snapshot.execution_profile.to_evidence_read_limits() == limits
    assert claim.snapshot.execution_profile_hash == profile.canonical_hash
    assert tuple(command.stage for command in claim.snapshot.commands) == (
        "source_prep", "vlm", "stage1_narrative", "stage2_portfolio",
        "stage3_blueprint", "media_preflight",
    )
    restarted = PostgresPipelineRunStore(store.connection_factory)
    assert await restarted.read_run(run_id) == claim.snapshot
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT execution_profile -> 'evidence_read_limits', execution_profile_hash,
                runtime.execution_profile_semantic_v9_is_valid(execution_profile, state)
                FROM runtime.pipeline_runs WHERE run_id = %s""",
            (run_id,),
        )
        budget, persisted_hash, valid = cursor.fetchone()
        assert budget == limits.to_mapping() and valid is True
        assert (persisted_hash.decode() if isinstance(persisted_hash, bytes) else persisted_hash) == profile.canonical_hash


def _assert_evidence_budget_rejected(cursor, mapping) -> None:
    # Real predicates plus INSERT with a freshly matching whole-profile hash:
    # a hash mismatch or an old-version guard cannot explain the rejection.
    cursor.execute(
        """SELECT runtime.evidence_read_limits_shape_is_valid(%s::jsonb),
            runtime.execution_profile_semantic_v9_is_valid(%s::jsonb, 'accepted')""",
        (json.dumps(mapping.get("evidence_read_limits")), json.dumps(mapping)),
    )
    assert cursor.fetchone() == (False, False)
    with pytest.raises(psycopg.errors.CheckViolation, match="pipeline_runs_execution_profile_closed_check"):
        _insert_profile_for_guard(cursor, mapping)


@pytest.mark.parametrize("limits", [
    None, [], "{}", {}, {"max_blob_bytes": 1}, {"max_total_blob_bytes": 2},
    {"max_blob_bytes": 1, "max_total_blob_bytes": 2, "default": 1},
    {"max_blob_bytes": 3, "max_total_blob_bytes": 2},
])
def test_0022_sql_budget_requires_closed_object_and_per_blob_within_total(limits) -> None:
    assert DSN is not None
    mapping = _execution_profile().to_mapping()
    mapping["evidence_read_limits"] = limits
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        _assert_evidence_budget_rejected(cursor, mapping)


def test_0022_sql_missing_budget_has_no_default() -> None:
    assert DSN is not None
    mapping = _execution_profile().to_mapping()
    del mapping["evidence_read_limits"]
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT runtime.evidence_read_limits_shape_is_valid(NULL)")
        assert cursor.fetchone() == (False,)
        _assert_evidence_budget_rejected(cursor, mapping)


@pytest.mark.parametrize("field", ["max_blob_bytes", "max_total_blob_bytes"])
@pytest.mark.parametrize("value", [None, True, False, 1.0, 0, -1, "1", [], {}, 2**53])
def test_0022_sql_budget_leaves_are_positive_safe_integers(field, value) -> None:
    assert DSN is not None
    mapping = _execution_profile().to_mapping()
    mapping["evidence_read_limits"][field] = value
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        _assert_evidence_budget_rejected(cursor, mapping)


@pytest.mark.parametrize("active_state", ["accepted", "running"])
def test_0019_migration_rolls_back_active_v5_then_preserves_terminal_history(active_state) -> None:
    """Remote-only migration transaction test on the explicit disposable DSN."""
    assert DSN is not None
    migration_root = Path("packages/autocut-kernel/migrations")
    migration_sql = (migration_root / "0019_stage1_pipeline_profile.sql").read_text(encoding="utf-8")
    profile = _v5_execution_profile()
    stages = ("source_prep", "vlm", "media_preflight")
    command_ids = tuple(uuid4() for _ in stages)

    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        for migration in sorted(migration_root.glob("*.sql")):
            if migration.name >= "0019_stage1_pipeline_profile.sql":
                break
            cursor.execute(migration.read_text(encoding="utf-8"))
        with connection.transaction():
            run_id = _insert_profile_for_guard(cursor, profile.to_mapping())
            for ordinal, (command_id, stage) in enumerate(zip(command_ids, stages, strict=True)):
                cursor.execute(
                    """INSERT INTO runtime.pipeline_commands
                        (command_id, run_id, ordinal, stage, state, version)
                    VALUES (%s, %s, %s, %s, 'pending', 0)""",
                    (command_id, run_id, ordinal, stage),
                )
            if active_state == "running":
                cursor.execute(
                    """UPDATE runtime.pipeline_runs SET state = 'running', version = version + 1,
                        updated_at = transaction_timestamp() WHERE run_id = %s""",
                    (run_id,),
                )

        def read_frozen_history():
            # Compare PostgreSQL's persisted JSONB text byte-for-byte, not a
            # replacement profile or regenerated plan. Include command IDs/state.
            cursor.execute(
                """SELECT execution_profile::text, execution_profile_hash,
                    (SELECT jsonb_agg(to_jsonb(command) ORDER BY ordinal)::text
                       FROM runtime.pipeline_commands command WHERE command.run_id = run.run_id)
                    FROM runtime.pipeline_runs run WHERE run_id = %s""",
                (run_id,),
            )
            return cursor.fetchone()

        cursor.execute(
            """SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid = 'runtime.pipeline_runs'::regclass
                  AND conname = 'pipeline_runs_execution_profile_closed_check'"""
        )
        original_constraint = cursor.fetchone()
        cursor.execute("SELECT pg_get_functiondef('runtime.guard_historical_execution_profile_write()'::regprocedure)")
        original_guard = cursor.fetchone()
        active_history = read_frozen_history()
        assert original_constraint is not None and original_guard is not None
        persisted_hash = active_history[1]
        assert (persisted_hash.decode() if isinstance(persisted_hash, bytes) else persisted_hash) == profile.canonical_hash
        with pytest.raises(psycopg.errors.RaiseException, match="0019 refuses accepted/running pre-v6 runs"):
            cursor.execute(migration_sql)
        connection.rollback()

        cursor.execute(
            """SELECT to_regprocedure('runtime.stage1_policy_closed_object(jsonb,text[])'),
                to_regprocedure('runtime.stage1_command_policy_shape_is_valid(jsonb)'),
                to_regprocedure('runtime.execution_profile_semantic_v6_is_valid(jsonb,text)')"""
        )
        assert cursor.fetchone() == (None, None, None)
        cursor.execute(
            """SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid = 'runtime.pipeline_runs'::regclass
                  AND conname = 'pipeline_runs_execution_profile_closed_check'"""
        )
        assert cursor.fetchone() == original_constraint
        cursor.execute("SELECT pg_get_functiondef('runtime.guard_historical_execution_profile_write()'::regprocedure)")
        assert cursor.fetchone() == original_guard
        assert read_frozen_history() == active_history
        cursor.execute("SELECT state FROM runtime.pipeline_runs WHERE run_id = %s", (run_id,))
        state = cursor.fetchone()[0]
        assert (state.decode() if isinstance(state, bytes) else state) == active_state

        # Explicitly finish the old control-plane workflow under its own rules:
        # a failed command has a matching Receipt; successors are causally blocked.
        # No trigger disabling, profile rewriting or implicit v6 activation.
        with connection.transaction():
            _force_terminal_command(cursor, str(command_ids[0]), "failed")
            cursor.execute(
                """UPDATE runtime.pipeline_commands SET state = 'blocked', version = version + 1,
                    blocking_command_id = %s, completed_at = transaction_timestamp(),
                    updated_at = transaction_timestamp() WHERE run_id = %s AND ordinal > 0""",
                (command_ids[0], run_id),
            )
            cursor.execute(
                """UPDATE runtime.pipeline_runs SET state = 'failed', version = version + 1,
                    updated_at = transaction_timestamp() WHERE run_id = %s""",
                (run_id,),
            )
        terminal_history = read_frozen_history()
        assert terminal_history[:2] == active_history[:2]
        assert json.loads(terminal_history[0]) == profile.to_mapping()
        assert tuple(item["stage"] for item in json.loads(terminal_history[2])) == stages

        cursor.execute(migration_sql)
        assert read_frozen_history() == terminal_history
        cursor.execute(
            """SELECT state, runtime.execution_profile_semantic_v6_is_valid(execution_profile, state)
                FROM runtime.pipeline_runs WHERE run_id = %s""",
            (run_id,),
        )
        state, valid_history = cursor.fetchone()
        assert (state.decode() if isinstance(state, bytes) else state) == "failed"
        assert valid_history is True
        with pytest.raises(psycopg.errors.RaiseException, match="historical pre-v6 execution profile rows are read-only"):
            cursor.execute(
                "UPDATE runtime.pipeline_runs SET execution_profile_hash = execution_profile_hash WHERE run_id = %s",
                (run_id,),
            )
        assert read_frozen_history() == terminal_history


def test_0020_migration_rejects_active_v6_and_keeps_terminal_history_read_only() -> None:
    """Remote-only upgrade coverage for the v7 boundary; never run without DSN."""
    assert DSN is not None
    migration_root = Path("packages/autocut-kernel/migrations")
    migration_sql = (migration_root / "0020_stage2_pipeline_profile.sql").read_text(encoding="utf-8")
    profile = _v6_execution_profile()

    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        for migration in sorted(migration_root.glob("*.sql")):
            if migration.name >= "0020_stage2_pipeline_profile.sql":
                break
            cursor.execute(migration.read_text(encoding="utf-8"))
        with connection.transaction():
            run_id = _insert_profile_for_guard(cursor, profile.to_mapping())

        cursor.execute(
            "SELECT execution_profile::text, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        active_history = cursor.fetchone()
        with pytest.raises(psycopg.errors.RaiseException, match="0020 refuses accepted/running pre-v7 runs"):
            cursor.execute(migration_sql)
        connection.rollback()
        cursor.execute("SELECT to_regprocedure('runtime.stage2_command_policy_shape_is_valid(jsonb)')")
        assert cursor.fetchone() == (None,)
        cursor.execute(
            "SELECT execution_profile::text, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        assert cursor.fetchone() == active_history

        with connection.transaction():
            cursor.execute(
                """UPDATE runtime.pipeline_runs SET state = 'failed', version = version + 1,
                    updated_at = transaction_timestamp() WHERE run_id = %s""",
                (run_id,),
            )
        cursor.execute(
            "SELECT execution_profile::text, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        terminal_history = cursor.fetchone()
        assert json.loads(terminal_history[0]) == profile.to_mapping()

        cursor.execute(migration_sql)
        cursor.execute(
            """SELECT runtime.execution_profile_semantic_v7_is_valid(execution_profile, state)
                 FROM runtime.pipeline_runs WHERE run_id = %s""",
            (run_id,),
        )
        assert cursor.fetchone() == (True,)
        with pytest.raises(psycopg.errors.RaiseException, match="historical pre-v7 execution profile rows are read-only"):
            cursor.execute(
                "UPDATE runtime.pipeline_runs SET execution_profile_hash = execution_profile_hash WHERE run_id = %s",
                (run_id,),
            )
        cursor.execute(
            "SELECT execution_profile::text, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        assert cursor.fetchone() == terminal_history

        valid = _execution_profile().to_mapping()
        invalid_candidate = json.loads(json.dumps(valid))
        invalid_candidate["stage2_command_policy"]["candidate_policy"]["minimum_confidence"] = None
        invalid_range = json.loads(json.dumps(valid))
        invalid_range["stage2_command_policy"]["job_policy"]["proposal_count"]["min"] = None
        invalid_unknown = json.loads(json.dumps(valid))
        invalid_unknown["stage2_command_policy"]["story_policy"]["unexpected"] = True
        for mapping, expected in (
            (valid, True),
            (invalid_candidate, False),
            (invalid_range, False),
            (invalid_unknown, False),
        ):
            cursor.execute(
                "SELECT runtime.stage2_command_policy_shape_is_valid(%s::jsonb)",
                (json.dumps(mapping["stage2_command_policy"]),),
            )
            assert cursor.fetchone() == (expected,)


def test_0021_migration_rejects_active_v7_and_keeps_terminal_history_read_only() -> None:
    """Remote-only upgrade coverage for the v8 boundary; never run without DSN."""
    assert DSN is not None
    migration_root = Path("packages/autocut-kernel/migrations")
    migration_sql = (migration_root / "0021_stage3_pipeline_profile.sql").read_text(encoding="utf-8")
    profile = _v7_execution_profile()

    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        for migration in sorted(migration_root.glob("*.sql")):
            if migration.name >= "0021_stage3_pipeline_profile.sql":
                break
            cursor.execute(migration.read_text(encoding="utf-8"))
        with connection.transaction():
            run_id = _insert_profile_for_guard(cursor, profile.to_mapping())

        cursor.execute(
            "SELECT execution_profile::text, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        active_history = cursor.fetchone()
        with pytest.raises(psycopg.errors.RaiseException, match="0021 refuses accepted/running pre-v8 runs"):
            cursor.execute(migration_sql)
        connection.rollback()
        cursor.execute("SELECT to_regprocedure('runtime.stage3_command_policy_shape_is_valid(jsonb)')")
        assert cursor.fetchone() == (None,)
        cursor.execute(
            "SELECT execution_profile::text, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        assert cursor.fetchone() == active_history

        with connection.transaction():
            cursor.execute(
                """UPDATE runtime.pipeline_runs SET state = 'failed', version = version + 1,
                    updated_at = transaction_timestamp() WHERE run_id = %s""",
                (run_id,),
            )
        cursor.execute(
            "SELECT execution_profile::text, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        terminal_history = cursor.fetchone()
        assert json.loads(terminal_history[0]) == profile.to_mapping()

        cursor.execute(migration_sql)
        cursor.execute(
            """SELECT runtime.execution_profile_semantic_v8_is_valid(execution_profile, state)
                 FROM runtime.pipeline_runs WHERE run_id = %s""",
            (run_id,),
        )
        assert cursor.fetchone() == (True,)
        with pytest.raises(psycopg.errors.RaiseException, match="historical pre-v8 execution profile rows are read-only"):
            cursor.execute(
                "UPDATE runtime.pipeline_runs SET execution_profile_hash = execution_profile_hash WHERE run_id = %s",
                (run_id,),
            )
        cursor.execute(
            "SELECT execution_profile::text, execution_profile_hash FROM runtime.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        assert cursor.fetchone() == terminal_history


@pytest.mark.parametrize("active_state", ["accepted", "running"])
def test_0022_refuses_active_v8_then_preserves_exact_terminal_history(active_state) -> None:
    """Remote-only upgrade; finish old work honestly before installing the new guard."""
    assert DSN is not None
    migration_root = Path("packages/autocut-kernel/migrations")
    migration_sql = (migration_root / "0022_evidence_read_limits_profile.sql").read_text(encoding="utf-8")
    profile = _v8_execution_profile()
    stages = ("source_prep", "vlm", "stage1_narrative", "stage2_portfolio", "stage3_blueprint", "media_preflight")
    command_ids = tuple(uuid4() for _ in stages)

    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        for migration in sorted(migration_root.glob("*.sql")):
            if migration.name >= "0022_evidence_read_limits_profile.sql":
                break
            cursor.execute(migration.read_text(encoding="utf-8"))
        with connection.transaction():
            run_id = _insert_profile_for_guard(cursor, profile.to_mapping())
            for ordinal, (command_id, stage) in enumerate(zip(command_ids, stages, strict=True)):
                cursor.execute(
                    """INSERT INTO runtime.pipeline_commands
                        (command_id, run_id, ordinal, stage, state, version)
                    VALUES (%s, %s, %s, %s, 'pending', 0)""",
                    (command_id, run_id, ordinal, stage),
                )
            if active_state == "running":
                cursor.execute(
                    """UPDATE runtime.pipeline_runs SET state = 'running', version = version + 1,
                        updated_at = transaction_timestamp() WHERE run_id = %s""",
                    (run_id,),
                )

        def read_history():
            cursor.execute(
                """SELECT execution_profile::text, execution_profile_hash, state, version,
                    (SELECT jsonb_agg(to_jsonb(command) ORDER BY ordinal)::text
                       FROM runtime.pipeline_commands command WHERE command.run_id = run.run_id)
                    FROM runtime.pipeline_runs run WHERE run_id = %s""",
                (run_id,),
            )
            return cursor.fetchone()

        def read_guards():
            cursor.execute(
                """SELECT pg_get_constraintdef(oid),
                    pg_get_functiondef('runtime.guard_historical_execution_profile_write()'::regprocedure)
                    FROM pg_constraint WHERE conrelid = 'runtime.pipeline_runs'::regclass
                      AND conname = 'pipeline_runs_execution_profile_closed_check'"""
            )
            return cursor.fetchone()

        active_history, old_guards = read_history(), read_guards()
        with pytest.raises(psycopg.errors.RaiseException, match="0022 refuses accepted/running pre-v9 runs"):
            cursor.execute(migration_sql)
        connection.rollback()
        assert read_history() == active_history
        assert read_guards() == old_guards
        cursor.execute(
            """SELECT to_regprocedure('runtime.evidence_read_limits_shape_is_valid(jsonb)'),
                to_regprocedure('runtime.execution_profile_semantic_v9_is_valid(jsonb,text)')"""
        )
        assert cursor.fetchone() == (None, None)

        # Legitimate old-plan closure with a failed Receipt and causally blocked
        # successors. No trigger disabling, new defaults, or successful full-run claim.
        with connection.transaction():
            _force_terminal_command(cursor, str(command_ids[0]), "failed")
            cursor.execute(
                """UPDATE runtime.pipeline_commands SET state = 'blocked', version = version + 1,
                    blocking_command_id = %s, completed_at = transaction_timestamp(),
                    updated_at = transaction_timestamp() WHERE run_id = %s AND ordinal > 0""",
                (command_ids[0], run_id),
            )
            cursor.execute(
                """UPDATE runtime.pipeline_runs SET state = 'failed', version = version + 1,
                    updated_at = transaction_timestamp() WHERE run_id = %s""",
                (run_id,),
            )
        terminal_history = read_history()
        assert terminal_history[:2] == active_history[:2]
        assert json.loads(terminal_history[0]) == profile.to_mapping()
        assert "evidence_read_limits" not in json.loads(terminal_history[0])
        assert tuple(item["stage"] for item in json.loads(terminal_history[4])) == stages

        cursor.execute(migration_sql)
        assert read_history() == terminal_history
        cursor.execute(
            """SELECT runtime.execution_profile_semantic_v9_is_valid(execution_profile, state)
                FROM runtime.pipeline_runs WHERE run_id = %s""",
            (run_id,),
        )
        assert cursor.fetchone() == (True,)
        with pytest.raises(psycopg.errors.RaiseException, match="historical pre-v9 execution profile rows are read-only"):
            cursor.execute(
                "UPDATE runtime.pipeline_runs SET execution_profile_hash = execution_profile_hash WHERE run_id = %s",
                (run_id,),
            )
        assert read_history() == terminal_history

    store = PostgresPipelineRunStore(lambda: psycopg.connect(DSN))
    history = store._read_run_sync(run_id)
    assert history is not None and history.status == "failed"
    assert history.execution_profile == profile
    assert history.execution_profile.canonical_hash == profile.canonical_hash
    assert tuple(command.stage for command in history.commands) == stages
    assert store._list_reconstructible_runs_sync() == ()
    assert store._claim_next_pending_sync(run_id, 0, "forbidden-history-worker") is None
    with pytest.raises(ResumeNotAllowedError):
        store._claim_resume_sync(run_id, history.version)
    for command in history.commands[1:]:
        with pytest.raises(PipelineRunValidationError, match="profile v9"):
            PipelineStageContext(run_id, history.request, command, profile)
    with pytest.raises(PipelineRunValidationError, match="profile v9"):
        store._claim_run_sync(
            f"pipeline_run_{uuid4().hex}", f"forbidden-v8-{uuid4().hex}",
            history.request, history.request_hash, profile,
        )


def test_0013_postgres_allows_v4_writes_and_rejects_new_historical_rows() -> None:
    assert DSN is not None
    request = PipelineRunRequest("test", source_root="/migration-0013/source")
    v4 = _v4_execution_profile()
    v3 = _historical_execution_profile("pipeline-execution-profile-v3")

    def insert_profile(
        cursor,
        *,
        label: str,
        profile: dict[str, object],
        profile_hash: str,
        state: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO runtime.pipeline_runs
                (run_id, idempotency_key, request_hash, profile,
                 source_kind, source_value, execution_profile,
                 execution_profile_hash, state, version)
            VALUES (%s, %s, %s, 'test', 'root', %s, %s::jsonb, %s, %s, 0)
            """,
            (
                f"pipeline_run_{uuid4().hex}",
                f"migration-0013-{label}-{uuid4().hex}",
                request.request_hash,
                request.source_root,
                json.dumps(profile),
                profile_hash,
                state,
            ),
        )

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            # Preserve the actual 0013 contract, not the latest write guard.
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for migration in sorted(Path("packages/autocut-kernel/migrations").glob("*.sql")):
                if migration.name > "0013_vlm_semantic_pack_profile.sql":
                    break
                cursor.execute(migration.read_text(encoding="utf-8"))
            insert_profile(
                cursor,
                label="v4-current",
                profile=v4.to_mapping(),
                profile_hash=v4.canonical_hash,
                state="accepted",
            )
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="new v1/v2/v3 execution profile rows are forbidden",
            ):
                insert_profile(
                    cursor,
                    label="v3-new-history",
                    profile=v3.to_mapping(),
                    profile_hash=v3.canonical_hash,
                    state="failed",
                )

            v4_with_legacy_policy = v4.to_mapping()
            v4_with_legacy_policy["parse_policy"] = v3.to_mapping()["parse_policy"]
            with pytest.raises(psycopg.errors.CheckViolation):
                insert_profile(
                    cursor,
                    label="v4-legacy-policy",
                    profile=v4_with_legacy_policy,
                    profile_hash=v4.canonical_hash,
                    state="accepted",
                )

            v3_with_current_policy = v3.to_mapping()
            v3_with_current_policy["parse_policy"] = v4.to_mapping()["parse_policy"]
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="new v1/v2/v3 execution profile rows are forbidden",
            ):
                insert_profile(
                    cursor,
                    label="v3-current-policy",
                    profile=v3_with_current_policy,
                    profile_hash=v3.canonical_hash,
                    state="failed",
                )

            null_schema = v4.to_mapping()
            null_schema["schema_version"] = None
            with pytest.raises(psycopg.errors.CheckViolation):
                insert_profile(
                    cursor,
                    label="null-schema-version",
                    profile=null_schema,
                    profile_hash=v4.canonical_hash,
                    state="accepted",
                )


def _force_terminal_command(cursor, command_id: str, outcome: str) -> str:
    lease_id = f"fixture-{command_id}"
    cursor.execute(
        """
        UPDATE runtime.pipeline_commands
           SET state = 'running', version = version + 1, lease_id = %s,
               lease_expires_at = transaction_timestamp() + interval '1 hour',
               updated_at = transaction_timestamp()
         WHERE command_id = %s AND state = 'pending'
        """,
        (lease_id, command_id),
    )
    receipt_id = str(uuid4())
    cursor.execute(
        """
        INSERT INTO runtime.pipeline_run_receipts (receipt_id, command_id, outcome)
        VALUES (%s, %s, %s)
        """,
        (receipt_id, command_id, outcome),
    )
    cursor.execute(
        """
        UPDATE runtime.pipeline_commands
           SET state = %s, version = version + 1,
               lease_id = NULL, lease_expires_at = NULL,
               completed_at = transaction_timestamp(),
               updated_at = transaction_timestamp()
         WHERE command_id = %s AND state = 'running' AND lease_id = %s
        """,
        (outcome, command_id, lease_id),
    )
    return receipt_id


@pytest.mark.asyncio
async def test_claim_and_outbox_are_atomic_and_reconstruct_after_restart(tmp_path: Path) -> None:
    service, store, scheduler = _composition(tmp_path)
    request = PipelineRunRequest.from_mapping(
        {"profile": "test", "source_root": str(tmp_path / "input")}
    )

    first = await service.submit(request, "postgres-request-1")
    replay = await service.submit(request, "postgres-request-1")

    assert replay.replayed is True
    assert replay.snapshot.run_id == first.snapshot.run_id
    assert replay.snapshot.commands[0].stage == "source_prep"
    assert replay.snapshot.commands[0].status == "pending"
    assert replay.snapshot.commands[0].receipt_id is None
    assert tuple(command.stage for command in replay.snapshot.commands) == (
        "source_prep",
        "vlm",
        "stage1_narrative",
        "stage2_portfolio",
        "stage3_blueprint",
        "media_preflight",
    )
    assert await scheduler.pending_run_ids() == (first.snapshot.run_id,)

    restarted = DurablePipelineRunService(
        PostgresPipelineRunStore(store.connection_factory),
        PostgresPipelineScheduler(store.connection_factory),
        _source_catalog(tmp_path / "input"),
    )
    assert await restarted.status(first.snapshot.run_id) == first.snapshot
    assert await restarted.reconstruct() == (first.snapshot.run_id,)


@pytest.mark.asyncio
async def test_postgres_replay_uses_frozen_profile_and_new_key_uses_changed_profile(
    tmp_path: Path,
) -> None:
    assert DSN is not None

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory)
    scheduler = PostgresPipelineScheduler(factory)
    authority = _source_catalog(tmp_path / "input")
    first_profile = _execution_profile(
        model_id="doubao-model-v1",
        asr_model_revision="sensevoice-frozen-v1",
    )
    changed_profile = _execution_profile(
        model_id="doubao-model-v2",
        asr_model_revision="sensevoice-new-v2",
        stage1_revision=2,
    )
    first_service = DurablePipelineRunService(
        store,
        scheduler,
        authority,
        execution_profile=first_profile,
    )
    changed_service = DurablePipelineRunService(
        PostgresPipelineRunStore(factory),
        PostgresPipelineScheduler(factory),
        authority,
        execution_profile=changed_profile,
    )
    request = PipelineRunRequest("test", source_root=str(tmp_path / "input"))

    first = await first_service.submit(request, "postgres-profile-frozen")
    replay = await changed_service.submit(request, "postgres-profile-frozen")
    changed = await changed_service.submit(request, "postgres-profile-new")

    assert replay.replayed is True
    assert replay.snapshot.execution_profile == first_profile
    assert replay.snapshot.execution_profile_hash == first_profile.canonical_hash
    assert replay.snapshot.execution_profile.build_stage1_command_policy().artifact_revision == 1
    assert (
        replay.snapshot.execution_profile.to_media_preflight_policy().asr_model_revision
        == "sensevoice-frozen-v1"
    )
    assert (
        replay.snapshot.execution_profile.to_media_preflight_policy().word_timing_capability
        == "required"
    )
    assert changed.snapshot.execution_profile == changed_profile
    assert changed.snapshot.execution_profile_hash == changed_profile.canonical_hash
    assert changed.snapshot.execution_profile.build_stage1_command_policy().artifact_revision == 2
    assert first.snapshot.request_hash == changed.snapshot.request_hash
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT execution_profile, execution_profile_hash
                  FROM runtime.pipeline_runs WHERE run_id = %s
                """,
                (first.snapshot.run_id,),
            )
            persisted = cursor.fetchone()
            assert persisted is not None
            assert persisted[0] == first_profile.to_mapping()
            persisted_hash = (
                persisted[1].decode() if isinstance(persisted[1], bytes) else persisted[1]
            )
            assert persisted_hash == first_profile.canonical_hash


@pytest.mark.asyncio
async def test_postgres_read_recomputes_execution_profile_hash(tmp_path: Path) -> None:
    assert DSN is not None
    service, store, _scheduler = _composition(tmp_path)
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "input")),
        "postgres-profile-hash-tamper",
    )

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE runtime.pipeline_runs DISABLE TRIGGER runtime_pipeline_run_transition_guard"
            )
            try:
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_runs
                       SET execution_profile_hash = %s
                     WHERE run_id = %s
                    """,
                    ("sha256:" + "0" * 64, submitted.snapshot.run_id),
                )
            finally:
                cursor.execute(
                    "ALTER TABLE runtime.pipeline_runs ENABLE TRIGGER runtime_pipeline_run_transition_guard"
                )

    with pytest.raises(PipelineRunValidationError, match="hash does not bind"):
        await store.read_run(submitted.snapshot.run_id)


@pytest.mark.asyncio
async def test_postgres_check_rejects_active_profile_downgrade_to_v2(tmp_path: Path) -> None:
    assert DSN is not None
    service, _store, _scheduler = _composition(tmp_path)
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "input")),
        "postgres-profile-v2-downgrade",
    )
    mapping = submitted.snapshot.execution_profile.to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v2"
    mapping["parse_policy"] = {
        "max_observations": 64,
        "max_response_bytes": 64_000,
        "max_summary_characters": 512,
        "max_total_summary_characters": 8_192,
        "minimum_confidence": "0.80",
    }
    del mapping["media_preflight_policy"]
    del mapping["media_preflight_policy_hash"]
    del mapping["materialization_limits"]
    del mapping["stage1_command_policy"]
    del mapping["stage2_command_policy"]
    del mapping["stage3_command_policy"]
    del mapping["evidence_read_limits"]
    v2 = PipelineExecutionProfile.from_mapping(mapping)

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE runtime.pipeline_runs DISABLE TRIGGER runtime_pipeline_run_transition_guard"
            )
            try:
                with pytest.raises(
                    psycopg.errors.RaiseException,
                    match="new pre-v9 execution profile rows are forbidden",
                ):
                    cursor.execute(
                        """
                        UPDATE runtime.pipeline_runs
                           SET execution_profile = %s::jsonb,
                               execution_profile_hash = %s
                         WHERE run_id = %s
                        """,
                        (v2.canonical_json, v2.canonical_hash, submitted.snapshot.run_id),
                    )
            finally:
                cursor.execute(
                    "ALTER TABLE runtime.pipeline_runs ENABLE TRIGGER runtime_pipeline_run_transition_guard"
                )


@pytest.mark.asyncio
async def test_postgres_idempotency_conflict_and_resume_cas(tmp_path: Path) -> None:
    service, _store, scheduler = _composition(tmp_path, tmp_path / "different")
    first_request = PipelineRunRequest.from_mapping(
        {"profile": "shadow", "source_reference": "source:fixture-1"}
    )
    first = await service.submit(first_request, "postgres-request-1")

    with pytest.raises(IdempotencyConflictError):
        await service.submit(
            PipelineRunRequest.from_mapping(
                {"profile": "test", "source_root": str(tmp_path / "different")}
            ),
            "postgres-request-1",
        )

    resumed = await service.resume(first.snapshot.run_id, expected_version=0)
    assert resumed.run_id == first.snapshot.run_id
    assert resumed.version == 1
    assert resumed.commands[0].status == "pending"
    with pytest.raises(StaleRunVersionError):
        await service.resume(first.snapshot.run_id, expected_version=0)
    assert await scheduler.pending_run_ids() == (first.snapshot.run_id,)


@pytest.mark.asyncio
async def test_source_authority_denies_before_postgres_claim(tmp_path: Path) -> None:
    service, store, _scheduler = _composition(tmp_path)

    with pytest.raises(SourceDeniedError):
        await service.submit(
            PipelineRunRequest.from_mapping(
                {"profile": "test", "source_root": str(tmp_path.parent / "outside")}
            ),
            "postgres-request-1",
        )

    assert await store.list_reconstructible_runs() == ()


@pytest.mark.asyncio
async def test_leased_result_and_receipt_are_one_cas_projection(tmp_path: Path) -> None:
    service, store, _scheduler = _composition(tmp_path)
    submitted = await service.submit(
        PipelineRunRequest.from_mapping(
            {"profile": "test", "source_root": str(tmp_path / "input")}
        ),
        "postgres-request-1",
    )
    run_id = submitted.snapshot.run_id
    command = await store.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="worker-lease-1",
    )
    assert command is not None
    receipt_id = uuid4()

    await store.record_result(
        run_id,
        result=PipelineStageResult(command.command_id, "succeeded", receipt_id),
        expected_version=1,
        lease_id="worker-lease-1",
    )

    restarted = PostgresPipelineRunStore(store.connection_factory)
    projected = await restarted.read_run(run_id)
    assert projected is not None
    assert projected.status == "running"
    assert projected.commands[0].status == "succeeded"
    assert projected.commands[0].receipt_id == receipt_id
    assert projected.commands[1].status == "pending"
    vlm = await restarted.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="worker-lease-2",
    )
    assert vlm is not None and vlm.stage == "vlm"
    vlm_receipt_id = uuid4()
    await restarted.record_result(
        run_id,
        result=PipelineStageResult(vlm.command_id, "succeeded", vlm_receipt_id),
        expected_version=vlm.version,
        lease_id="worker-lease-2",
    )
    narrative = await restarted.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="worker-lease-3",
    )
    assert narrative is not None and narrative.stage == "stage1_narrative"
    await restarted.record_result(
        run_id,
        result=PipelineStageResult(narrative.command_id, "succeeded", uuid4()),
        expected_version=narrative.version,
        lease_id="worker-lease-3",
    )
    portfolio = await restarted.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="worker-lease-4",
    )
    assert portfolio is not None and portfolio.stage == "stage2_portfolio"
    await restarted.record_result(
        run_id,
        result=PipelineStageResult(portfolio.command_id, "succeeded", uuid4()),
        expected_version=portfolio.version,
        lease_id="worker-lease-4",
    )
    blueprint = await restarted.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="worker-lease-5",
    )
    assert blueprint is not None and blueprint.stage == "stage3_blueprint"
    await restarted.record_result(
        run_id,
        result=PipelineStageResult(blueprint.command_id, "succeeded", uuid4()),
        expected_version=blueprint.version,
        lease_id="worker-lease-5",
    )
    media = await restarted.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="worker-lease-6",
    )
    assert media is not None and media.stage == "media_preflight"
    await restarted.record_result(
        run_id,
        result=PipelineStageResult(media.command_id, "succeeded", uuid4()),
        expected_version=media.version,
        lease_id="worker-lease-6",
    )
    completed = await restarted.read_run(run_id)
    assert completed is not None and completed.status == "failed"
    assert all(command.status == "succeeded" for command in completed.commands)
    with pytest.raises(StaleRunVersionError):
        await restarted.record_result(
            run_id,
            result=PipelineStageResult(command.command_id, "succeeded", receipt_id),
            expected_version=1,
            lease_id="worker-lease-1",
        )


@pytest.mark.asyncio
async def test_predecessor_denial_atomically_blocks_vlm_and_terminates_run(
    tmp_path: Path,
) -> None:
    service, store, _scheduler = _composition(tmp_path)
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "input")),
        "postgres-denied-1",
    )
    source = await store.claim_next_pending(
        submitted.snapshot.run_id,
        expected_version=0,
        lease_id="source-lease",
    )
    assert source is not None
    receipt_id = uuid4()
    await store.record_result(
        submitted.snapshot.run_id,
        result=PipelineStageResult(source.command_id, "denied", receipt_id),
        expected_version=source.version,
        lease_id="source-lease",
    )

    snapshot = await store.read_run(submitted.snapshot.run_id)
    assert snapshot is not None and snapshot.status == "denied"
    assert snapshot.commands[0].status == "denied"
    assert snapshot.commands[1].status == "blocked"
    assert snapshot.commands[1].receipt_id is None
    assert snapshot.commands[1].blocking_command_id == source.command_id
    assert tuple(command.stage for command in snapshot.commands[1:]) == (
        "vlm", "stage1_narrative", "stage2_portfolio", "stage3_blueprint", "media_preflight",
    )
    assert all(
        command.status == "blocked"
        and command.receipt_id is None
        and command.blocking_command_id == source.command_id
        for command in snapshot.commands[1:]
    )
    assert (
        await store.claim_next_pending(
            snapshot.run_id,
            expected_version=snapshot.commands[1].version,
            lease_id="forbidden-vlm-lease",
        )
        is None
    )


@pytest.mark.asyncio
async def test_blocked_command_rejects_cross_run_blocker(tmp_path: Path) -> None:
    assert DSN is not None
    service, store, _scheduler = _composition(
        tmp_path,
        tmp_path / "denied",
        tmp_path / "target",
    )
    denied_run = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "denied")),
        "postgres-blocker-cross-a",
    )
    source = await store.claim_next_pending(
        denied_run.snapshot.run_id,
        expected_version=0,
        lease_id="cross-source",
    )
    assert source is not None
    await store.record_result(
        denied_run.snapshot.run_id,
        result=PipelineStageResult(source.command_id, "denied", uuid4()),
        expected_version=source.version,
        lease_id="cross-source",
    )
    target_run = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "target")),
        "postgres-blocker-cross-b",
    )
    target_vlm = target_run.snapshot.commands[1]

    with pytest.raises(psycopg.DatabaseError, match="earlier denied/failed"):
        with psycopg.connect(DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_commands
                       SET state = 'blocked', version = version + 1,
                           blocking_command_id = %s,
                           completed_at = transaction_timestamp(),
                           updated_at = transaction_timestamp()
                     WHERE command_id = %s
                    """,
                    (source.command_id, target_vlm.command_id),
                )


@pytest.mark.asyncio
async def test_blocked_command_rejects_later_failed_blocker(tmp_path: Path) -> None:
    assert DSN is not None
    service, _store, _scheduler = _composition(tmp_path, tmp_path / "later")
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "later")),
        "postgres-blocker-later",
    )
    source, vlm = submitted.snapshot.commands[:2]

    with pytest.raises(psycopg.DatabaseError, match="earlier denied/failed"):
        with psycopg.connect(DSN) as connection:
            with connection.cursor() as cursor:
                _force_terminal_command(cursor, vlm.command_id, "failed")
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_commands
                       SET state = 'blocked', version = version + 1,
                           blocking_command_id = %s,
                           completed_at = transaction_timestamp(),
                           updated_at = transaction_timestamp()
                     WHERE command_id = %s
                    """,
                    (vlm.command_id, source.command_id),
                )


@pytest.mark.asyncio
async def test_blocked_command_rejects_non_failure_predecessor(tmp_path: Path) -> None:
    assert DSN is not None
    service, _store, _scheduler = _composition(tmp_path, tmp_path / "success")
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "success")),
        "postgres-blocker-success",
    )
    source, vlm = submitted.snapshot.commands[:2]

    with pytest.raises(psycopg.DatabaseError, match="earlier denied/failed"):
        with psycopg.connect(DSN) as connection:
            with connection.cursor() as cursor:
                _force_terminal_command(cursor, source.command_id, "succeeded")
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_commands
                       SET state = 'blocked', version = version + 1,
                           blocking_command_id = %s,
                           completed_at = transaction_timestamp(),
                           updated_at = transaction_timestamp()
                     WHERE command_id = %s
                    """,
                    (source.command_id, vlm.command_id),
                )


@pytest.mark.asyncio
async def test_outbox_lease_ack_requeue_and_command_heartbeat(tmp_path: Path) -> None:
    service, store, scheduler = _composition(tmp_path)
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "input")),
        "postgres-worker-1",
    )
    outbox = await scheduler.claim_next(lease_id="outbox-1")
    assert outbox is not None and outbox.run_id == submitted.snapshot.run_id
    assert await scheduler.claim_next(lease_id="outbox-2") is None
    await scheduler.requeue(outbox)
    replay = await scheduler.claim_next(lease_id="outbox-3")
    assert replay is not None and replay.version > outbox.version
    renewed_outbox = await scheduler.renew(replay)
    assert renewed_outbox.version == replay.version + 1

    command = await store.claim_next_pending(
        submitted.snapshot.run_id,
        expected_version=0,
        lease_id="command-1",
    )
    assert command is not None
    renewed = await store.renew_running_lease(
        submitted.snapshot.run_id,
        command_id=command.command_id,
        expected_version=command.version,
        lease_id="command-1",
    )
    assert renewed.version == command.version + 1
    await scheduler.acknowledge(renewed_outbox)
    assert await scheduler.pending_run_ids() == ()


@pytest.mark.asyncio
async def test_expired_outbox_lease_is_reclaimed_with_a_new_cas_version(
    tmp_path: Path,
) -> None:
    assert DSN is not None

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory)
    scheduler = PostgresPipelineScheduler(factory, lease_seconds=1)
    service = DurablePipelineRunService(
        store,
        scheduler,
        _source_catalog(tmp_path / "input"),
        execution_profile=_execution_profile(),
    )
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "input")),
        "postgres-expired-outbox-1",
    )
    expired = await scheduler.claim_next(lease_id="expired-owner")
    assert expired is not None and expired.run_id == submitted.snapshot.run_id
    await asyncio.sleep(1.05)

    reclaimed = await scheduler.claim_next(lease_id="replacement-owner")

    assert reclaimed is not None
    assert reclaimed.outbox_id == expired.outbox_id
    assert reclaimed.version == expired.version + 2
    await scheduler.requeue(reclaimed)


class _RecoveredStage:
    def __init__(self) -> None:
        self.calls = 0
        self.receipt_id = uuid4()

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult:
        self.calls += 1
        return PipelineStageResult(context.command.command_id, "succeeded", self.receipt_id)


@pytest.mark.asyncio
async def test_expired_lease_resumes_only_through_reconciler(tmp_path: Path) -> None:
    assert DSN is not None

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory, lease_seconds=1)
    scheduler = PostgresPipelineScheduler(factory)
    service = DurablePipelineRunService(
        store,
        scheduler,
        _source_catalog(tmp_path / "input"),
        execution_profile=_execution_profile(),
    )
    submitted = await service.submit(
        PipelineRunRequest.from_mapping(
            {"profile": "test", "source_root": str(tmp_path / "input")}
        ),
        "postgres-request-1",
    )
    run_id = submitted.snapshot.run_id
    claimed = await store.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="crashed-worker-lease",
    )
    assert claimed is not None
    await asyncio.sleep(1.05)

    with pytest.raises(StaleRunVersionError):
        await store.record_result(
            run_id,
            result=PipelineStageResult(claimed.command_id, "succeeded", uuid4()),
            expected_version=1,
            lease_id="crashed-worker-lease",
        )
    expired = await store.expire_running_lease(
        run_id,
        expected_version=1,
        lease_id="crashed-worker-lease",
    )
    assert expired.status == "indeterminate"
    assert expired.version == 2
    with pytest.raises(StaleRunVersionError):
        await store.record_result(
            run_id,
            result=PipelineStageResult(claimed.command_id, "succeeded", uuid4()),
            expected_version=1,
            lease_id="crashed-worker-lease",
        )

    resumed = await service.resume(run_id, expected_version=2)
    assert resumed.commands[0].status == "indeterminate"
    assert resumed.version == 3
    assert await service.reconstruct() == (run_id,)

    recovered_stage = _RecoveredStage()
    reconciler = PipelineStageReconciler.from_ports(
        PostgresPipelineRunStore(factory),
        ("source_prep", recovered_stage),
    )
    snapshot = await store.read_run(run_id)
    assert snapshot is not None
    result = await reconciler.reconcile(snapshot)
    assert result is not None
    assert recovered_stage.calls == 1
    projected = await store.read_run(run_id)
    assert projected is not None
    assert projected.status == "running"
    assert projected.commands[0].receipt_id == recovered_stage.receipt_id
    assert projected.commands[1].status == "pending"


def test_0007_upgrades_active_0005_single_stage_runs_with_causal_vlm_state() -> None:
    assert DSN is not None
    migration_root = Path("packages/autocut-kernel/migrations")
    cases = (
        ("pending", "accepted", "pending", "accepted"),
        ("running", "running", "pending", "running"),
        ("succeeded", "running", "pending", "running"),
        ("denied", "running", "blocked", "denied"),
        ("failed", "accepted", "blocked", "failed"),
        ("succeeded", "succeeded", None, "succeeded"),
    )

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for migration in sorted(migration_root.glob("000[1-5]_*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))

            identities: list[tuple[str, str, str, str | None, str]] = []
            for index, (
                source_state,
                initial_run_state,
                expected_vlm_state,
                expected_run_state,
            ) in enumerate(cases, start=1):
                run_id = f"pipeline_run_{index:032x}"
                command_id = str(uuid4())
                with connection.transaction():
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_runs
                            (run_id, idempotency_key, request_hash, profile,
                             source_kind, source_value, state, version)
                        VALUES (%s, %s, %s, 'test', 'root', %s, 'accepted', 0)
                        """,
                        (
                            run_id,
                            f"upgrade-{index}",
                            "sha256:" + f"{index:x}" * 64,
                            f"/upgrade/{index}",
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_commands
                            (command_id, run_id, ordinal, stage, state, version)
                        VALUES (%s, %s, 0, 'source_prep', 'pending', 0)
                        """,
                        (command_id, run_id),
                    )
                    if source_state == "running":
                        cursor.execute(
                            """
                            UPDATE runtime.pipeline_commands
                               SET state = 'running', version = 1,
                                   lease_id = 'upgrade-running',
                                   lease_expires_at = transaction_timestamp()
                                       + interval '1 hour',
                                   updated_at = transaction_timestamp()
                             WHERE command_id = %s
                            """,
                            (command_id,),
                        )
                    elif source_state in ("succeeded", "denied", "failed"):
                        _force_terminal_command(cursor, command_id, source_state)
                    if initial_run_state != "accepted":
                        cursor.execute(
                            """
                            UPDATE runtime.pipeline_runs
                               SET state = %s, version = version + 1,
                                   updated_at = transaction_timestamp()
                             WHERE run_id = %s
                            """,
                            (initial_run_state, run_id),
                        )
                identities.append(
                    (
                        run_id,
                        command_id,
                        source_state,
                        expected_vlm_state,
                        expected_run_state,
                    )
                )

            cursor.execute(
                (migration_root / "0007_pipeline_stage_worker.sql").read_text(encoding="utf-8")
            )
            for (
                run_id,
                source_command_id,
                source_state,
                expected_vlm_state,
                expected_run_state,
            ) in identities:
                cursor.execute(
                    """
                    SELECT state FROM runtime.pipeline_runs WHERE run_id = %s
                    """,
                    (run_id,),
                )
                assert cursor.fetchone() == (expected_run_state,)
                cursor.execute(
                    """
                    SELECT state, blocking_command_id
                      FROM runtime.pipeline_commands
                     WHERE run_id = %s AND ordinal = 1
                    """,
                    (run_id,),
                )
                vlm = cursor.fetchone()
                if expected_vlm_state is None:
                    assert vlm is None
                else:
                    assert vlm is not None
                    assert vlm[0] == expected_vlm_state
                    assert (str(vlm[1]) if vlm[1] is not None else None) == (
                        source_command_id if source_state in ("denied", "failed") else None
                    )


@pytest.mark.asyncio
async def test_0008_aborts_on_old_active_runs_and_marks_only_terminal_history() -> None:
    assert DSN is not None
    migration_root = Path("packages/autocut-kernel/migrations")
    active_request = PipelineRunRequest("test", source_root="/legacy/active")
    terminal_request = PipelineRunRequest("test", source_root="/legacy/terminal")
    active_run_id = "pipeline_run_" + "a" * 32
    terminal_run_id = "pipeline_run_" + "b" * 32

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for migration in sorted(migration_root.glob("000[1-6]_*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))
            for run_id, idempotency_key, request in (
                (active_run_id, "legacy-active", active_request),
                (terminal_run_id, "legacy-terminal", terminal_request),
            ):
                command_id = str(uuid4())
                with connection.transaction():
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_runs
                            (run_id, idempotency_key, request_hash, profile,
                             source_kind, source_value, state, version)
                        VALUES (%s, %s, %s, 'test', 'root', %s, 'accepted', 0)
                        """,
                        (run_id, idempotency_key, request.request_hash, request.source_root),
                    )
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_commands
                            (command_id, run_id, ordinal, stage, state, version)
                        VALUES (%s, %s, 0, 'source_prep', 'pending', 0)
                        """,
                        (command_id, run_id),
                    )
                    _force_terminal_command(cursor, command_id, "succeeded")
                    cursor.execute(
                        """
                        UPDATE runtime.pipeline_runs
                           SET state = %s, version = version + 1,
                               updated_at = transaction_timestamp()
                         WHERE run_id = %s
                        """,
                        (
                            "running" if run_id == active_run_id else "succeeded",
                            run_id,
                        ),
                    )
            cursor.execute(
                (migration_root / "0007_pipeline_stage_worker.sql").read_text(encoding="utf-8")
            )
            profile_migration = (migration_root / "0008_pipeline_execution_profile.sql").read_text(
                encoding="utf-8"
            )
            with pytest.raises(
                psycopg.DatabaseError,
                match="refuses legacy accepted/running pipeline runs",
            ):
                cursor.execute(profile_migration)
            connection.rollback()
            cursor.execute(
                """
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'runtime'
                   AND table_name = 'pipeline_runs'
                   AND column_name = 'execution_profile'
                """
            )
            assert cursor.fetchone() is None

            cursor.execute(
                """
                SELECT command_id FROM runtime.pipeline_commands
                 WHERE run_id = %s AND stage = 'vlm' AND state = 'pending'
                """,
                (active_run_id,),
            )
            active_vlm = cursor.fetchone()
            assert active_vlm is not None
            with connection.transaction():
                _force_terminal_command(cursor, str(active_vlm[0]), "failed")
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_runs
                       SET state = 'failed', version = version + 1,
                           updated_at = transaction_timestamp()
                     WHERE run_id = %s AND state = 'running'
                    """,
                    (active_run_id,),
                )
            cursor.execute(profile_migration)

            for index, malformed_profile in enumerate(
                (
                    "{}",
                    '{"kind":"legacy_unresolved"}',
                    '{"kind":null,"schema_version":null}',
                )
            ):
                with pytest.raises(
                    psycopg.errors.CheckViolation,
                    match="pipeline_runs_execution_profile_closed_check",
                ):
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_runs
                            (run_id, idempotency_key, request_hash, profile,
                             source_kind, source_value, state, version,
                             execution_profile, execution_profile_hash)
                        VALUES (%s, %s, %s, 'test', 'root', %s, 'accepted', 0,
                                %s::jsonb, %s)
                        """,
                        (
                            "pipeline_run_" + str(index + 1) * 32,
                            f"malformed-profile-{index}",
                            active_request.request_hash,
                            f"/malformed/{index}",
                            malformed_profile,
                            "sha256:" + "0" * 64,
                        ),
                    )

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory)
    failed_history = await store.read_run(active_run_id)
    succeeded_history = await store.read_run(terminal_run_id)

    assert failed_history is not None and failed_history.status == "failed"
    assert failed_history.execution_profile.is_legacy_unresolved
    assert tuple(command.stage for command in failed_history.commands) == (
        "source_prep",
        "vlm",
    )
    assert failed_history.commands[1].status == "failed"
    assert succeeded_history is not None and succeeded_history.status == "succeeded"
    assert succeeded_history.execution_profile.is_legacy_unresolved
    assert await store.list_reconstructible_runs() == ()


@pytest.mark.asyncio
async def test_0012_atomically_rejects_active_historical_profiles_then_replays() -> None:
    assert DSN is not None
    migration_root = Path("packages/autocut-kernel/migrations")

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for migration in sorted(migration_root.glob("*.sql")):
                if migration.name >= "0012_pipeline_media_preflight_profile.sql":
                    break
                cursor.execute(migration.read_text(encoding="utf-8"))

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory)
    v1 = _historical_execution_profile("pipeline-execution-profile-v1")
    v2 = _historical_execution_profile("pipeline-execution-profile-v2")
    request = PipelineRunRequest("test", source_root="/migration-0012/source")

    async def create_run(
        label: str,
        profile: PipelineExecutionProfile,
    ) -> str:
        # These rows predate v6. Seed their historical schema and stage plan
        # directly; today's public claim_run must not reactivate old profiles.
        run_id = f"pipeline_run_{uuid4().hex}"
        stages = ("source_prep", "vlm")
        if profile.schema_version in ("pipeline-execution-profile-v3", "pipeline-execution-profile-v4"):
            stages += ("media_preflight",)
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO runtime.pipeline_runs
                    (run_id, idempotency_key, request_hash, profile, source_kind,
                     source_value, execution_profile, execution_profile_hash, state, version)
                VALUES (%s, %s, %s, 'test', 'root', %s, %s::jsonb, %s, 'accepted', 0)""",
                (run_id, f"migration-0012-{label}", request.request_hash,
                 request.source_root, profile.canonical_json, profile.canonical_hash),
            )
            for ordinal, stage in enumerate(stages):
                cursor.execute(
                    """INSERT INTO runtime.pipeline_commands
                        (command_id, run_id, ordinal, stage, state, version)
                    VALUES (%s, %s, %s, %s, 'pending', 0)""",
                    (uuid4(), run_id, ordinal, stage),
                )
        return run_id

    terminal_v1_id = await create_run("terminal-v1", v1)
    terminal_v2_id = await create_run("terminal-v2", v2)
    active_v1_id = await create_run("active-v1", v1)
    active_v2_id = await create_run("active-v2", v2)

    async def fail_next(run_id: str, lease_id: str) -> None:
        command = await store.claim_next_pending(
            run_id,
            expected_version=0,
            lease_id=lease_id,
        )
        assert command is not None
        await store.record_result(
            run_id,
            result=PipelineStageResult(command.command_id, "failed", uuid4()),
            expected_version=command.version,
            lease_id=lease_id,
        )

    await fail_next(terminal_v1_id, "terminal-v1")
    await fail_next(terminal_v2_id, "terminal-v2")
    active_v2_command = await store.claim_next_pending(
        active_v2_id,
        expected_version=0,
        lease_id="active-v2",
    )
    assert active_v2_command is not None

    profile_migration = (migration_root / "0012_pipeline_media_preflight_profile.sql").read_text(
        encoding="utf-8"
    )
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(
                psycopg.DatabaseError,
                match="refuses accepted/running pipeline runs",
            ):
                cursor.execute(profile_migration)
            connection.rollback()
            cursor.execute(
                """
                SELECT state, execution_profile ->> 'schema_version'
                  FROM runtime.pipeline_runs
                 WHERE run_id = ANY(%s)
                 ORDER BY run_id
                """,
                ([active_v1_id, active_v2_id],),
            )
            active_rows = cursor.fetchall()
            assert {str(row[0]) for row in active_rows} == {"accepted", "running"}
            assert {str(row[1]) for row in active_rows} == {
                "pipeline-execution-profile-v1",
                "pipeline-execution-profile-v2",
            }
            cursor.execute(
                """
                SELECT pg_get_constraintdef(oid)
                  FROM pg_constraint
                 WHERE conrelid = 'runtime.pipeline_runs'::regclass
                   AND conname = 'pipeline_runs_execution_profile_closed_check'
                """
            )
            old_constraint = cursor.fetchone()
            assert old_constraint is not None
            assert "word_timing_capability" not in str(old_constraint[0])

    await fail_next(active_v1_id, "active-v1")
    await store.record_result(
        active_v2_id,
        result=PipelineStageResult(
            active_v2_command.command_id,
            "failed",
            uuid4(),
        ),
        expected_version=active_v2_command.version,
        lease_id="active-v2",
    )

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(profile_migration)
            cursor.execute(profile_migration)

            malformed_v3 = _historical_execution_profile("pipeline-execution-profile-v3").to_mapping()
            media_policy = malformed_v3["media_preflight_policy"]
            assert isinstance(media_policy, dict)
            media_policy["word_timing_capability"] = "sentence_only"
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="pipeline_runs_execution_profile_closed_check",
            ):
                cursor.execute(
                    """
                    INSERT INTO runtime.pipeline_runs
                        (run_id, idempotency_key, request_hash, profile,
                         source_kind, source_value, execution_profile,
                         execution_profile_hash, state, version)
                    VALUES (%s, %s, %s, 'test', 'root', %s, %s::jsonb, %s,
                            'accepted', 0)
                    """,
                    (
                        f"pipeline_run_{uuid4().hex}",
                        "migration-0012-malformed-v3",
                        request.request_hash,
                        request.source_root,
                        json.dumps(malformed_v3),
                        _historical_execution_profile("pipeline-execution-profile-v3").canonical_hash,
                    ),
                )

    for run_id, expected_profile in (
        (terminal_v1_id, v1),
        (terminal_v2_id, v2),
        (active_v1_id, v1),
        (active_v2_id, v2),
    ):
        history = await store.read_run(run_id)
        assert history is not None
        assert history.status == "failed"
        assert history.execution_profile == expected_profile

    v3 = _historical_execution_profile("pipeline-execution-profile-v3")
    terminal_v3_id = await create_run("terminal-v3", v3)
    active_v3_id = await create_run("active-v3-history", v3)
    await fail_next(terminal_v3_id, "terminal-v3")
    active_v3_command = await store.claim_next_pending(
        active_v3_id,
        expected_version=0,
        lease_id="active-v3-history",
    )
    assert active_v3_command is not None

    semantic_migration = (migration_root / "0013_vlm_semantic_pack_profile.sql").read_text(
        encoding="utf-8"
    )
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(
                psycopg.DatabaseError,
                match="0013 refuses accepted/running pipeline runs",
            ):
                cursor.execute(semantic_migration)
            connection.rollback()

    await store.record_result(
        active_v3_id,
        result=PipelineStageResult(active_v3_command.command_id, "failed", uuid4()),
        expected_version=active_v3_command.version,
        lease_id="active-v3-history",
    )

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(semantic_migration)
            cursor.execute(semantic_migration)
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="historical v1/v2/v3 execution profile rows are read-only",
            ):
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_runs
                       SET execution_profile_hash = execution_profile_hash
                     WHERE run_id = %s
                    """,
                    (terminal_v2_id,),
                )

    for run_id, expected_profile in (
        (terminal_v1_id, v1),
        (terminal_v2_id, v2),
        (active_v1_id, v1),
        (active_v2_id, v2),
        (terminal_v3_id, v3),
        (active_v3_id, v3),
    ):
        history = await store.read_run(run_id)
        assert history is not None
        assert history.status == "failed"
        assert history.execution_profile == expected_profile

    v4_run_id = await create_run("active-v4", _v4_execution_profile())
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                ALTER TABLE runtime.pipeline_runs
                DISABLE TRIGGER runtime_pipeline_run_transition_guard
                """
            )
            try:
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_runs
                       SET execution_profile_hash = %s
                     WHERE run_id = %s
                    """,
                    ("sha256:" + "0" * 64, v4_run_id),
                )
            finally:
                cursor.execute(
                    """
                    ALTER TABLE runtime.pipeline_runs
                    ENABLE TRIGGER runtime_pipeline_run_transition_guard
                    """
                )
    with pytest.raises(PipelineRunValidationError, match="hash does not bind"):
        await store.read_run(v4_run_id)


def _agent() -> MagicMock:
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="unused")
    return agent


@pytest.mark.asyncio
async def test_real_http_run_status_resume_survive_app_restart(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    first_service, _, _ = _composition(tmp_path)
    headers = {"Idempotency-Key": "http-postgres-request-1"}
    payload = {"profile": "test", "source_root": str(tmp_path / "input")}

    first_client = TestClient(TestServer(create_app(_agent(), pipeline_run_service=first_service)))
    await first_client.start_server()
    created = await first_client.post("/v1/pipeline/run", headers=headers, json=payload)
    assert created.status == 202
    created_body = await created.json()
    run_id = created_body["run_id"]
    await first_client.close()

    restarted_service, _, _ = _composition(tmp_path)
    restarted_client = TestClient(
        TestServer(create_app(_agent(), pipeline_run_service=restarted_service))
    )
    await restarted_client.start_server()
    replay = await restarted_client.post("/v1/pipeline/run", headers=headers, json=payload)
    status = await restarted_client.get("/v1/pipeline/status", params={"run_id": run_id})
    resumed = await restarted_client.post(
        "/v1/pipeline/resume",
        json={"run_id": run_id, "expected_version": 0},
    )
    try:
        assert replay.status == 202
        assert (await replay.json())["run_id"] == run_id
        assert (await replay.json())["replayed"] is True
        assert status.status == 200
        status_body = await status.json()
        assert status_body["commands"] == [
            {
                "command_id": status_body["commands"][0]["command_id"],
                "stage": "source_prep",
                "status": "pending",
                "receipt_id": None,
                "version": 0,
                "lease_id": None,
                "blocking_command_id": None,
            },
            {
                "command_id": status_body["commands"][1]["command_id"],
                "stage": "vlm",
                "status": "pending",
                "receipt_id": None,
                "version": 0,
                "lease_id": None,
                "blocking_command_id": None,
            },
            {
                "command_id": status_body["commands"][2]["command_id"],
                "stage": "stage1_narrative",
                "status": "pending",
                "receipt_id": None,
                "version": 0,
                "lease_id": None,
                "blocking_command_id": None,
            },
            {
                "command_id": status_body["commands"][3]["command_id"],
                "stage": "stage2_portfolio",
                "status": "pending",
                "receipt_id": None,
                "version": 0,
                "lease_id": None,
                "blocking_command_id": None,
            },
            {
                "command_id": status_body["commands"][4]["command_id"],
                "stage": "stage3_blueprint",
                "status": "pending",
                "receipt_id": None,
                "version": 0,
                "lease_id": None,
                "blocking_command_id": None,
            },
            {
                "command_id": status_body["commands"][5]["command_id"],
                "stage": "media_preflight",
                "status": "pending",
                "receipt_id": None,
                "version": 0,
                "lease_id": None,
                "blocking_command_id": None,
            },
        ]
        assert resumed.status == 202
        assert (await resumed.json())["version"] == 1
    finally:
        await restarted_client.close()
