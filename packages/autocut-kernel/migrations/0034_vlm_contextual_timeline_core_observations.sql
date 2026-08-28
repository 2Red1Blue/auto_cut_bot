-- Register V9 as the timeline-closed successor to V8.
-- Historical execution-profile bytes and all prior artifacts/Receipts remain immutable.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION runtime.execution_profile_contextual_timeline_core_prompt_is_valid(
    profile_value jsonb,
    run_state text
)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'prompt_version'
                = 'vlm-semantic-pack-v9-context-assisted-timeline-core-observations' THEN
        CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v10'
                  AND profile_value ->> 'provider_id' = 'doubao-ark-responses-stream' THEN
            -- V9 changes only the separately bound model instruction text.  It
            -- retains V8's ContextPack input and closed core-observation schema.
            runtime.execution_profile_contextual_core_prompt_is_valid(
                jsonb_set(
                    profile_value, '{prompt_version}',
                    '"vlm-semantic-pack-v8-context-assisted-core-observations"'::jsonb, false
                ),
                run_state
            )
        ELSE FALSE END
    ELSE runtime.execution_profile_contextual_core_prompt_is_valid(profile_value, run_state)
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_contextual_timeline_core_prompt_is_valid(execution_profile, state)
    ) IS TRUE);

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'Timeline-core V9 is v10-only; discriminator projection preserves all historical profile bytes.';

COMMIT;
