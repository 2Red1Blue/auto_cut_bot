-- Register V11's compact/canonical model instruction without relabelling V10.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION runtime.execution_profile_contextual_compact_canonical_prompt_is_valid(
    profile_value jsonb,
    run_state text
)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'prompt_version'
                = 'vlm-semantic-pack-v11-context-assisted-compact-canonical-core' THEN
        CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v10'
                  AND profile_value ->> 'provider_id' = 'doubao-ark-responses-stream' THEN
            runtime.execution_profile_contextual_closed_vocabulary_prompt_is_valid(
                jsonb_set(
                    profile_value, '{prompt_version}',
                    '"vlm-semantic-pack-v10-context-assisted-timeline-closed-vocabulary"'::jsonb, false
                ),
                run_state
            )
        ELSE FALSE END
    ELSE runtime.execution_profile_contextual_closed_vocabulary_prompt_is_valid(profile_value, run_state)
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_contextual_compact_canonical_prompt_is_valid(execution_profile, state)
    ) IS TRUE);

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'Compact-canonical V11 is v10-only; discriminator projection preserves all historical profile bytes.';

COMMIT;
