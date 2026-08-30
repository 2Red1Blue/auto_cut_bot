-- Combine the installed V23 semantic authority with Stage 1-3 policies.
-- V11 deliberately carries no media, render or publication fields.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

-- Repeat the planner boundaries for databases that had already installed
-- 0048 before this hardening was added.  ALTER FUNCTION is idempotent here and
-- changes neither accepted profile semantics nor persisted profile bytes.
ALTER FUNCTION runtime.execution_profile_semantic_v10_is_valid(jsonb, text)
    SET search_path TO pg_catalog, runtime;
ALTER FUNCTION runtime.execution_profile_contextual_stable_core_prompt_is_valid(jsonb, text)
    SET search_path TO pg_catalog, runtime;
ALTER FUNCTION runtime.execution_profile_contextual_minimal_object_shape_is_valid(jsonb, text)
    SET search_path TO pg_catalog, runtime;

CREATE FUNCTION runtime.execution_profile_semantic_story_v11_is_valid(
    profile_value jsonb,
    run_state text
)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
BEGIN
    IF profile_value ->> 'schema_version' = 'pipeline-execution-profile-v11' THEN
        IF NOT profile_value ?& ARRAY[
                'stage1_command_policy', 'stage2_command_policy', 'stage3_command_policy'
            ]::text[]
           OR profile_value ->> 'provider_id' <> 'doubao-ark-responses-stream'
           OR profile_value ->> 'model_id' <> 'doubao-seed-2-1-pro-260628'
           OR profile_value ->> 'adapter_strategy_version'
                <> 'doubao-ark-files-responses-stream-v5'
           OR profile_value ->> 'prompt_version'
                <> 'vlm-semantic-pack-v23-context-assisted-candidate-core'
           OR profile_value ->> 'kernel_parser_strategy_version'
                <> 'strict-semantic-pack-v4'
           OR profile_value ->> 'vlm_stage_strategy_version'
                <> 'doubao-generate-vlm-semantic-pack-v4-probe-then-parallel-3-v8' THEN
            RETURN FALSE;
        END IF;
        RETURN runtime.execution_profile_contextual_candidate_core_is_valid(
                   jsonb_set(
                       profile_value - ARRAY[
                           'stage1_command_policy', 'stage2_command_policy', 'stage3_command_policy'
                       ]::text[],
                       '{schema_version}',
                       '"pipeline-execution-profile-v10"'::jsonb,
                       false
                   ),
                   run_state
               )
               AND runtime.stage1_command_policy_shape_is_valid(
                   profile_value -> 'stage1_command_policy'
               )
               AND runtime.stage2_command_policy_shape_is_valid(
                   profile_value -> 'stage2_command_policy'
               )
               AND runtime.stage3_command_policy_shape_is_valid(
                   profile_value -> 'stage3_command_policy'
               );
    END IF;
    RETURN runtime.execution_profile_contextual_candidate_core_is_valid(
        profile_value,
        run_state
    );
END $$;

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_semantic_story_v11_is_valid(execution_profile, state)
    ) IS TRUE);

CREATE OR REPLACE FUNCTION runtime.guard_historical_execution_profile_write()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND (OLD.execution_profile ->> 'schema_version') NOT IN (
           'pipeline-execution-profile-v9',
           'pipeline-execution-profile-v10',
           'pipeline-execution-profile-v11'
       ) THEN
        RAISE EXCEPTION 'historical pre-v9 execution profile rows are read-only';
    END IF;
    IF (NEW.execution_profile ->> 'schema_version') NOT IN (
        'pipeline-execution-profile-v9',
        'pipeline-execution-profile-v10',
        'pipeline-execution-profile-v11'
    ) THEN
        RAISE EXCEPTION 'new execution profile rows must be v9, v10 or v11';
    END IF;
    RETURN NEW;
END $$;

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'v11 runs V23 semantic evidence through Stage 3 without media, render or publication authority.';

COMMIT;
