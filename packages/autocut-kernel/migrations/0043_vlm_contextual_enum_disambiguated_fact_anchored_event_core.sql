-- Register V18's fact-kind disambiguation prompt profile.
-- V18 preserves V17's wire schema and parser.  It only removes an observed
-- model ambiguity between visible_state and visible_change; historical
-- execution-profile bytes remain immutable.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION runtime.execution_profile_contextual_enum_disambiguated_fact_anchored_event_core_prompt_is_valid(
    profile_value jsonb,
    run_state text
)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'prompt_version'
                = 'vlm-semantic-pack-v18-context-assisted-enum-disambiguated-fact-anchored-event-core' THEN
        CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v10'
                  AND profile_value ->> 'provider_id' = 'doubao-ark-responses-stream'
                  AND profile_value ->> 'kernel_parser_strategy_version' = 'strict-semantic-pack-v4'
                  AND profile_value ->> 'vlm_stage_strategy_version'
                      = 'doubao-generate-vlm-semantic-pack-v4-probe-then-parallel-3-v7' THEN
            runtime.execution_profile_contextual_fact_anchored_event_core_prompt_is_valid(
                jsonb_set(
                    jsonb_set(
                        profile_value, '{prompt_version}',
                        '"vlm-semantic-pack-v17-context-assisted-fact-anchored-event-core"'::jsonb, false
                    ),
                    '{vlm_stage_strategy_version}',
                    '"doubao-generate-vlm-semantic-pack-v4-probe-then-parallel-3-v6"'::jsonb, false
                ),
                run_state
            )
        ELSE FALSE END
    ELSE runtime.execution_profile_contextual_fact_anchored_event_core_prompt_is_valid(profile_value, run_state)
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_contextual_enum_disambiguated_fact_anchored_event_core_prompt_is_valid(execution_profile, state)
    ) IS TRUE);

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'V18 explicitly disambiguates registered fact kinds; historical execution profiles remain immutable.';

COMMIT;
