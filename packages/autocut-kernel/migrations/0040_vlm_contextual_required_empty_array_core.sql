-- Register V15's required-empty-array VLM profile.
-- V15 keeps V13's required-field schema and asks the model to render the
-- optional graph arrays explicitly empty; V14's zero-item schema can cause
-- Ark to omit required keys. Historical profile bytes remain immutable.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION runtime.execution_profile_contextual_required_empty_array_core_prompt_is_valid(
    profile_value jsonb,
    run_state text
)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'prompt_version'
                = 'vlm-semantic-pack-v15-context-assisted-required-empty-array-core' THEN
        CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v10'
                  AND profile_value ->> 'provider_id' = 'doubao-ark-responses-stream'
                  AND profile_value ->> 'kernel_parser_strategy_version' = 'strict-semantic-pack-v4'
                  AND profile_value ->> 'vlm_stage_strategy_version'
                      = 'doubao-generate-vlm-semantic-pack-v4-probe-then-parallel-3-v4' THEN
            runtime.execution_profile_contextual_stable_core_prompt_is_valid(
                jsonb_set(
                    jsonb_set(
                        profile_value, '{prompt_version}',
                        '"vlm-semantic-pack-v14-context-assisted-stable-core-observations"'::jsonb, false
                    ),
                    '{vlm_stage_strategy_version}',
                    '"doubao-generate-vlm-semantic-pack-v4-probe-then-parallel-3-v3"'::jsonb, false
                ),
                run_state
            )
        ELSE FALSE END
    ELSE runtime.execution_profile_contextual_stable_core_prompt_is_valid(profile_value, run_state)
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_contextual_required_empty_array_core_prompt_is_valid(execution_profile, state)
    ) IS TRUE);

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'V15 preserves required empty graph fields in the Ark wire schema; historical profile bytes stay immutable.';

COMMIT;
