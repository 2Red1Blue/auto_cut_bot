-- Register V13's stricter model-output contract and bounded parallel policy.
-- Historical V12 profiles remain immutable and retain their original policy.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION runtime.execution_profile_contextual_validated_reciprocal_prompt_is_valid(
    profile_value jsonb,
    run_state text
)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'prompt_version'
                = 'vlm-semantic-pack-v13-context-assisted-validated-reciprocal-causal-core' THEN
        CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v10'
                  AND profile_value ->> 'provider_id' = 'doubao-ark-responses-stream'
                  AND profile_value ->> 'kernel_parser_strategy_version' = 'strict-semantic-pack-v4'
                  AND profile_value ->> 'vlm_stage_strategy_version'
                      = 'doubao-generate-vlm-semantic-pack-v4-probe-then-parallel-3-v2' THEN
            runtime.execution_profile_contextual_reciprocal_causal_prompt_is_valid(
                jsonb_set(
                    jsonb_set(
                        profile_value, '{prompt_version}',
                        '"vlm-semantic-pack-v12-context-assisted-reciprocal-causal-core"'::jsonb, false
                    ),
                    '{vlm_stage_strategy_version}',
                    '"doubao-generate-vlm-semantic-pack-v4-probe-then-parallel-10-v1"'::jsonb, false
                ),
                run_state
            )
        ELSE FALSE END
    ELSE runtime.execution_profile_contextual_reciprocal_causal_prompt_is_valid(profile_value, run_state)
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_contextual_validated_reciprocal_prompt_is_valid(execution_profile, state)
    ) IS TRUE);

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'V13 tightens V12 output generation and lowers batch concurrency; historical profile bytes stay immutable.';

COMMIT;
