-- Make execution-profile v5 bind every Media Preflight materialization limit.

BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.pipeline_runs
         WHERE state IN ('accepted', 'running')
           AND (execution_profile ->> 'schema_version'
               = 'pipeline-execution-profile-v5') IS NOT TRUE
    ) THEN
        RAISE EXCEPTION
            '0014 refuses accepted/running pipeline runs without v5 materialization policy';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION runtime.execution_profile_semantic_v5_is_valid(
    profile_value jsonb,
    run_state text
) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE
        WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v5' THEN
            runtime.execution_profile_semantic_v4_is_valid(
                jsonb_set(
                    profile_value - 'materialization_limits',
                    '{schema_version}',
                    '"pipeline-execution-profile-v4"'::jsonb
                ),
                run_state
            )
            AND jsonb_typeof(profile_value -> 'materialization_limits') = 'object'
            AND profile_value -> 'materialization_limits' ?& ARRAY[
                'copy_chunk_bytes', 'max_source_bytes', 'staging_quota_bytes',
                'timed_speech_max_request_bytes'
            ]::text[]
            AND (profile_value -> 'materialization_limits') - ARRAY[
                'copy_chunk_bytes', 'max_source_bytes', 'staging_quota_bytes',
                'timed_speech_max_request_bytes'
            ]::text[] = '{}'::jsonb
            AND jsonb_typeof(profile_value -> 'materialization_limits' -> 'copy_chunk_bytes') = 'number'
            AND jsonb_typeof(profile_value -> 'materialization_limits' -> 'max_source_bytes') = 'number'
            AND jsonb_typeof(profile_value -> 'materialization_limits' -> 'staging_quota_bytes') = 'number'
            AND jsonb_typeof(profile_value -> 'materialization_limits' -> 'timed_speech_max_request_bytes') = 'number'
            AND (profile_value -> 'materialization_limits' ->> 'copy_chunk_bytes') ~ '^[1-9][0-9]*$'
            AND (profile_value -> 'materialization_limits' ->> 'max_source_bytes') ~ '^[1-9][0-9]*$'
            AND (profile_value -> 'materialization_limits' ->> 'staging_quota_bytes') ~ '^[1-9][0-9]*$'
            AND (profile_value -> 'materialization_limits' ->> 'timed_speech_max_request_bytes') ~ '^[1-9][0-9]*$'
            AND CASE
                WHEN (profile_value -> 'materialization_limits' ->> 'copy_chunk_bytes') ~ '^[1-9][0-9]*$'
                 AND (profile_value -> 'materialization_limits' ->> 'max_source_bytes') ~ '^[1-9][0-9]*$'
                THEN (profile_value -> 'materialization_limits' ->> 'copy_chunk_bytes')::numeric
                     <= (profile_value -> 'materialization_limits' ->> 'max_source_bytes')::numeric
                ELSE false
            END
        WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v4' THEN
            runtime.execution_profile_semantic_v4_is_valid(profile_value, run_state)
            AND run_state IN ('succeeded', 'denied', 'failed')
        ELSE runtime.execution_profile_semantic_v4_is_valid(profile_value, run_state)
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_semantic_v5_is_valid(execution_profile, state)
    ) IS TRUE);

CREATE OR REPLACE FUNCTION runtime.guard_historical_execution_profile_write()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_schema_version text;
    new_schema_version text;
BEGIN
    old_schema_version := CASE
        WHEN TG_OP = 'UPDATE' THEN OLD.execution_profile ->> 'schema_version'
        ELSE NULL
    END;
    new_schema_version := NEW.execution_profile ->> 'schema_version';

    IF TG_OP = 'UPDATE' AND old_schema_version IN (
        'pipeline-execution-profile-v1', 'pipeline-execution-profile-v2',
        'pipeline-execution-profile-v3', 'pipeline-execution-profile-v4'
    ) THEN
        RAISE EXCEPTION
            'historical v1/v2/v3/v4 execution profile rows are read-only';
    END IF;

    IF new_schema_version IN (
        'pipeline-execution-profile-v1', 'pipeline-execution-profile-v2',
        'pipeline-execution-profile-v3', 'pipeline-execution-profile-v4'
    ) THEN
        RAISE EXCEPTION
            'new v1/v2/v3/v4 execution profile rows are forbidden';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check
    ON runtime.pipeline_runs IS
    'v1/v2/v3/v4 are terminal read-only history; v5 is the only executable Media Preflight profile.';

COMMIT;
