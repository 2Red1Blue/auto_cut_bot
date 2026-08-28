-- Add one bounded-reference prompt variant without changing the v4 parser,
-- the prior validator, or any persisted profile/request/response bytes.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION runtime.execution_profile_bounded_video_prompt_is_valid(profile_value jsonb, run_state text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'prompt_version' = 'vlm-semantic-pack-v6-bounded-references' THEN
        CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v10'
                  AND profile_value ->> 'provider_id' = 'doubao-ark-responses-stream' THEN
            -- Only the registered prompt discriminator is projected internally.
            -- The original complete object still supplies every other field,
            -- including the mandatory frozen parser implementation identity.
            runtime.execution_profile_semantic_v10_is_valid(
                jsonb_set(
                    profile_value, '{prompt_version}',
                    '"vlm-semantic-pack-v5-video-observation"'::jsonb, false
                ),
                run_state
            )
        ELSE FALSE END
    ELSE runtime.execution_profile_semantic_v10_is_valid(profile_value, run_state)
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_bounded_video_prompt_is_valid(execution_profile, state)
    ) IS TRUE);

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'Bounded-reference video prompt is v10-only; internal validation projection never rewrites frozen profile bytes.';

COMMIT;
