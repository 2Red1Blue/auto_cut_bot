from pathlib import Path

MIGRATION = Path("packages/autocut-kernel/migrations/0005_pipeline_http_run_control.sql")
WORKER_MIGRATION = Path("packages/autocut-kernel/migrations/0007_pipeline_stage_worker.sql")
EXECUTION_PROFILE_MIGRATION = Path(
    "packages/autocut-kernel/migrations/0008_pipeline_execution_profile.sql"
)
MEDIA_PROFILE_MIGRATION = Path(
    "packages/autocut-kernel/migrations/0012_pipeline_media_preflight_profile.sql"
)
SEMANTIC_PROFILE_MIGRATION = Path(
    "packages/autocut-kernel/migrations/0013_vlm_semantic_pack_profile.sql"
)
MATERIALIZATION_PROFILE_MIGRATION = Path(
    "packages/autocut-kernel/migrations/0014_media_preflight_materialization_profile.sql"
)
STAGE1_PROFILE_MIGRATION = Path(
    "packages/autocut-kernel/migrations/0019_stage1_pipeline_profile.sql"
)
STAGE2_PROFILE_MIGRATION = Path(
    "packages/autocut-kernel/migrations/0020_stage2_pipeline_profile.sql"
)
STAGE3_PROFILE_MIGRATION = Path(
    "packages/autocut-kernel/migrations/0021_stage3_pipeline_profile.sql"
)


def test_pipeline_http_run_migration_owns_durable_control_plane() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for relation in (
        "runtime.pipeline_runs",
        "runtime.pipeline_commands",
        "runtime.pipeline_run_receipts",
        "runtime.pipeline_run_outbox",
    ):
        assert f"CREATE TABLE {relation}" in sql
    assert "UNIQUE (idempotency_key)" in sql
    assert "pipeline command must begin pending at version zero" in sql
    assert "terminal pipeline command requires one matching Receipt" in sql
    assert "pipeline outbox transition requires exact version increment" in sql
    assert "pipeline command transition requires exact version increment" in sql
    assert "REVOKE ALL ON runtime.pipeline_run_outbox FROM PUBLIC" in sql


def test_pipeline_worker_migration_closes_ordered_blocking_and_heartbeat_states() -> None:
    sql = WORKER_MIGRATION.read_text(encoding="utf-8")

    assert "blocking_command_id uuid" in sql
    assert "'indeterminate', 'blocked'" in sql
    assert "OLD.state = 'pending' AND NEW.state IN ('running', 'blocked')" in sql
    assert "OLD.state = 'running' AND NEW.state IN" in sql
    assert "'running', 'succeeded', 'denied', 'failed', 'indeterminate'" in sql
    assert "non-Receipt pipeline command cannot have a Receipt" in sql
    assert "OLD.state = 'leased' AND NEW.state IN ('leased', 'pending', 'consumed')" in sql
    assert "md5(run.run_id || ':vlm')::uuid" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "runtime.assert_pipeline_blocker_relation" in sql
    assert "blocker.run_id = NEW.run_id" in sql
    assert "blocker.ordinal < NEW.ordinal" in sql
    assert "blocker.state IN ('denied', 'failed')" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql


def test_execution_profile_migration_is_immutable_closed_and_legacy_fail_closed() -> None:
    sql = EXECUTION_PROFILE_MIGRATION.read_text(encoding="utf-8")

    assert "ADD COLUMN execution_profile jsonb NOT NULL DEFAULT" in sql
    assert "ADD COLUMN execution_profile_hash text NOT NULL DEFAULT" in sql
    assert '"kind":"legacy_unresolved"' in sql
    assert "LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE" in sql
    assert "WHERE state IN ('accepted', 'running')" in sql
    assert "0008 refuses legacy accepted/running pipeline runs" in sql
    assert "pipeline_runs_execution_profile_closed_check" in sql
    assert ") IS TRUE);" in sql
    assert "kernel_parser_strategy_version" in sql
    assert "- ARRAY['kind', 'schema_version']::text[] = '{}'::jsonb" in sql
    assert "execution_profile ?& ARRAY[" in sql
    assert "(execution_profile -> 'request_parameters') - ARRAY[" in sql
    assert "(execution_profile -> 'parse_policy') - ARRAY[" in sql
    assert "NEW.execution_profile_hash" in sql
    assert "OLD.execution_profile_hash" in sql
    assert "distinct from request_hash" in sql


def test_media_profile_migration_requires_v3_for_reconstructible_runs() -> None:
    sql = MEDIA_PROFILE_MIGRATION.read_text(encoding="utf-8")

    assert "LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE" in sql
    assert "WHERE state IN ('accepted', 'running')" in sql
    assert "pipeline-execution-profile-v3" in sql
    assert "0012 refuses accepted/running pipeline runs" in sql
    assert "media_preflight_policy_hash" in sql
    assert "timed_speech_endpoint_url" in sql
    assert "asr_model_revision" in sql
    assert "vad_model_sha256" in sql
    assert "funasr_version" in sql
    assert "torch_version" in sql
    assert "word_timing_capability' = 'required'" in sql
    assert "calibrations" in sql
    assert "state IN ('succeeded', 'denied', 'failed')" in sql
    assert ") IS TRUE);" in sql


def test_semantic_profile_migration_closes_v4_parse_policy_major() -> None:
    sql = SEMANTIC_PROFILE_MIGRATION.read_text(encoding="utf-8")

    assert "LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE" in sql
    assert "0013 refuses accepted/running pipeline runs" in sql
    assert "execution_profile_semantic_v4_is_valid" in sql
    assert "pipeline-execution-profile-v4" in sql
    assert "pipeline-execution-profile-v3" in sql
    assert "max_candidate_hypotheses" in sql
    assert "max_total_text_characters" in sql
    assert "max_observations" in sql
    assert "v1/v2/v3 are terminal read-only history" in sql
    assert "guard_historical_execution_profile_write" in sql
    assert "new v1/v2/v3 execution profile rows are forbidden" in sql
    assert "historical v1/v2/v3 execution profile rows are read-only" in sql
    assert "BEFORE INSERT OR UPDATE ON runtime.pipeline_runs" in sql
    assert ") IS NOT TRUE" in sql
    assert ") IS TRUE);" in sql


def test_materialization_profile_migration_closes_v5_limits_and_v4_history() -> None:
    sql = MATERIALIZATION_PROFILE_MIGRATION.read_text(encoding="utf-8")

    assert "0014 refuses accepted/running pipeline runs" in sql
    assert "execution_profile_semantic_v5_is_valid" in sql
    assert "pipeline-execution-profile-v5" in sql
    assert "pipeline-execution-profile-v4" in sql
    assert "materialization_limits" in sql
    assert "copy_chunk_bytes" in sql
    assert "timed_speech_max_request_bytes" in sql
    assert "historical v1/v2/v3/v4 execution profile rows are read-only" in sql


def test_stage1_profile_migration_closes_v6_and_preserves_terminal_history() -> None:
    """Static SQL contract only: this does not claim PostgreSQL execution."""
    sql = STAGE1_PROFILE_MIGRATION.read_text(encoding="utf-8")
    assert "LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE" in sql
    assert "0019 refuses accepted/running pre-v6 runs" in sql
    assert "WHERE state IN ('accepted', 'running')" in sql
    assert "runtime.execution_profile_semantic_v6_is_valid" in sql
    assert "runtime.stage1_command_policy_shape_is_valid" in sql
    assert "profile_value - 'stage1_command_policy'" in sql
    assert "runtime.execution_profile_semantic_v5_is_valid(profile_value, run_state)" in sql
    assert "run_state IN ('succeeded', 'denied', 'failed')" in sql
    assert "historical pre-v6 execution profile rows are read-only" in sql
    assert "new pre-v6 execution profile rows are forbidden" in sql
    assert "runtime.stage1_policy_closed_object" in sql
    assert ") IS TRUE);" in sql
    for field in (
        "artifact_revision", "generation", "draft_policy", "coverage_policy",
        "dependency_policy", "retry_policy", "prompt_template", "temperature",
        "max_prompt_bytes", "max_input_windows", "max_input_objects", "max_beats",
        "max_obligations", "max_story_threads", "max_merge_proposals",
        "max_references_per_item", "max_total_text_characters", "minimum_confidence",
        "canonical_owner_by_object_type", "edge_projections", "attribute_projections",
        "external_root_projections", "max_attempts", "backoff_seconds",
    ):
        assert f"'{field}'" in sql
    assert "UPDATE runtime.pipeline_runs" not in sql
    assert "INSERT INTO runtime.pipeline_commands" not in sql


def test_stage2_profile_migration_closes_v7_and_preserves_terminal_history() -> None:
    """Static SQL contract only: this does not claim PostgreSQL execution."""
    sql = STAGE2_PROFILE_MIGRATION.read_text(encoding="utf-8")
    assert "LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE" in sql
    assert "0020 refuses accepted/running pre-v7 runs" in sql
    assert "WHERE state IN ('accepted', 'running')" in sql
    assert "runtime.execution_profile_semantic_v7_is_valid" in sql
    assert "runtime.stage2_command_policy_shape_is_valid" in sql
    assert "profile_value - 'stage2_command_policy'" in sql
    assert "runtime.execution_profile_semantic_v6_is_valid(profile_value, run_state)" in sql
    assert "run_state IN ('succeeded', 'denied', 'failed')" in sql
    assert "historical pre-v7 execution profile rows are read-only" in sql
    assert "new pre-v7 execution profile rows are forbidden" in sql
    assert "runtime.stage1_policy_closed_object" in sql
    for field in (
        "artifact_revision", "generation", "max_prompt_bytes", "draft_policy",
        "candidate_policy", "job_policy", "story_policy", "retry_policy",
        "max_json_depth", "max_material_requirements_per_proposal",
        "max_total_material_requirements", "required_measurement_kinds",
        "story_design_policy_sha256", "selection_strategy", "proposal_count",
        "target_duration_seconds", "source_constraints", "authorization_purpose",
        "allowed_source_refs", "forbidden_source_refs", "minimum_confidence",
    ):
        assert f"'{field}'" in sql
    assert "UPDATE runtime.pipeline_runs" not in sql
    assert "INSERT INTO runtime.pipeline_commands" not in sql


def test_stage3_profile_migration_closes_v8_and_preserves_terminal_history() -> None:
    sql = STAGE3_PROFILE_MIGRATION.read_text(encoding="utf-8")
    for text in (
        "0021 refuses accepted/running pre-v8 runs",
        "runtime.execution_profile_semantic_v8_is_valid",
        "runtime.stage3_command_policy_shape_is_valid",
        "profile_value - 'stage3_command_policy'",
        "runtime.execution_profile_semantic_v7_is_valid(profile_value, run_state)",
        "historical pre-v8 execution profile rows are read-only",
        "new pre-v8 execution profile rows are forbidden",
        "stage3_command_policy", "draft_policy", "context_policy", "feasibility_policy", "retry_policy",
        "LANGUAGE plpgsql", "jsonb_typeof", "jsonb_array_elements",
        "^[1-9][0-9]{0,15}$", "^(0|[1-9][0-9]{0,15})$",
        "max_response_bytes", "max_search_states", "max_source_members",
    ):
        assert text in sql


def test_new_run_sql_has_ordered_stage3_and_current_profile_claim_predicates() -> None:
    """Inspect the actual emitted-query source; never open a DB connection."""
    from inspect import getsource

    from auto_cut_bot.pipeline.runtime.postgres import PostgresPipelineRunStore

    source = getsource(PostgresPipelineRunStore)
    start = source.index("INSERT INTO runtime.pipeline_commands")
    insert = source[start:source.index("self._insert_outbox", start)]
    for ordinal, stage in enumerate(
        ("source_prep", "vlm", "stage1_narrative", "stage2_portfolio", "stage3_blueprint", "media_preflight")
    ):
        assert f"%s, %s, {ordinal}, '{stage}', 'pending', 0" in insert
    assert insert.count("uuid4(), run_id") == 6
    assert "candidate.stage NOT IN ('vlm', 'stage1_narrative', 'stage2_portfolio', 'stage3_blueprint')" in source
    assert "->> 'schema_version' = 'pipeline-execution-profile-v8'" in source
    assert "profile_run.execution_profile ? 'stage1_command_policy'" in source
    assert "profile_run.execution_profile ? 'stage2_command_policy'" in source
    assert "profile_run.execution_profile ? 'stage3_command_policy'" in source
    assert "predecessor.ordinal < candidate.ordinal" in source
    assert "predecessor.state <> 'succeeded'" in source
