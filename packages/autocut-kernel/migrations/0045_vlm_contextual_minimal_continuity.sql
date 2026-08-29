-- Register V20's self-contained minimal observation prompt.
-- V20 removes the accidental V7 template suffix and fixes continuity to the
-- conservative empty/default projection. Historical V19 profile bytes remain
-- valid and immutable.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION runtime.execution_profile_contextual_minimal_continuity_prompt_is_valid(
    profile_value jsonb,
    run_state text
)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'prompt_version'
                = 'vlm-semantic-pack-v20-context-assisted-minimal-core-observations' THEN
        CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v10'
                  AND profile_value ->> 'provider_id' = 'doubao-ark-responses-stream'
                  AND profile_value ->> 'kernel_parser_strategy_version' = 'strict-semantic-pack-v4'
                  AND profile_value ->> 'vlm_stage_strategy_version'
                      = 'doubao-generate-vlm-semantic-pack-v4-probe-then-parallel-3-v8' THEN
            runtime.execution_profile_contextual_minimal_core_prompt_is_valid(
                jsonb_set(
                    profile_value, '{prompt_version}',
                    '"vlm-semantic-pack-v19-context-assisted-minimal-core-observations"'::jsonb, false
                ),
                run_state
            )
        ELSE FALSE END
    ELSE runtime.execution_profile_contextual_minimal_core_prompt_is_valid(profile_value, run_state)
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_contextual_minimal_continuity_prompt_is_valid(execution_profile, state)
    ) IS TRUE);

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'V20 uses a self-contained minimal observation prompt with conservative empty continuity; historical profiles remain immutable.';

COMMIT;
