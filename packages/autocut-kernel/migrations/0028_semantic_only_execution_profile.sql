-- Permit a deliberately narrow SourcePrep -> VLM plan without weakening v9.
-- A v10 run has no media, story, render, or publication configuration.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION runtime.execution_profile_semantic_v10_is_valid(profile_value jsonb, run_state text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v10' THEN
        jsonb_typeof(profile_value) = 'object'
        AND profile_value ->> 'kind' = 'doubao_vlm'
        AND profile_value ?& ARRAY[
            'adapter_strategy_version', 'generation_retry_policy', 'kind',
            'kernel_parser_strategy_version', 'model_id', 'parse_policy',
            'prompt_version', 'provider_id', 'request_parameters',
            'response_schema', 'schema_version', 'vlm_stage_strategy_version'
        ]::text[]
        AND profile_value - ARRAY[
            'adapter_strategy_version', 'generation_retry_policy', 'kind',
            'kernel_parser_strategy_version', 'model_id', 'parse_policy',
            'prompt_version', 'provider_id', 'request_parameters',
            'response_schema', 'schema_version', 'vlm_stage_strategy_version'
        ]::text[] = '{}'::jsonb
        AND jsonb_typeof(profile_value -> 'response_schema') = 'object'
        AND jsonb_typeof(profile_value -> 'request_parameters') = 'object'
        AND profile_value -> 'request_parameters' ?& ARRAY[
            'adapter_strategy_version', 'max_output_tokens', 'temperature', 'video_fps'
        ]::text[]
        AND (profile_value -> 'request_parameters') - ARRAY[
            'adapter_strategy_version', 'max_output_tokens', 'temperature', 'video_fps'
        ]::text[] = '{}'::jsonb
        AND profile_value -> 'request_parameters' ->> 'adapter_strategy_version'
            = profile_value ->> 'adapter_strategy_version'
        AND jsonb_typeof(profile_value -> 'request_parameters' -> 'max_output_tokens') = 'number'
        AND (profile_value -> 'request_parameters' ->> 'max_output_tokens') ~ '^[1-9][0-9]{0,4}$'
        AND (profile_value -> 'request_parameters' ->> 'max_output_tokens')::numeric <= 32768
        AND jsonb_typeof(profile_value -> 'request_parameters' -> 'temperature') = 'number'
        AND (profile_value -> 'request_parameters' ->> 'temperature')::numeric BETWEEN 0 AND 2
        AND jsonb_typeof(profile_value -> 'request_parameters' -> 'video_fps') = 'number'
        AND (profile_value -> 'request_parameters' ->> 'video_fps')::numeric BETWEEN 0.1 AND 10
        AND jsonb_typeof(profile_value -> 'parse_policy') = 'object'
        AND profile_value -> 'parse_policy' ?& ARRAY[
            'max_candidate_hypotheses', 'max_entities', 'max_events', 'max_facts',
            'max_measurements', 'max_response_bytes', 'max_temporal_segments',
            'max_text_characters', 'max_total_text_characters'
        ]::text[]
        AND (profile_value -> 'parse_policy') - ARRAY[
            'max_candidate_hypotheses', 'max_entities', 'max_events', 'max_facts',
            'max_measurements', 'max_response_bytes', 'max_temporal_segments',
            'max_text_characters', 'max_total_text_characters'
        ]::text[] = '{}'::jsonb
        AND NOT EXISTS (
            SELECT 1 FROM jsonb_each(profile_value -> 'parse_policy') AS entry(key, value)
             WHERE jsonb_typeof(value) <> 'number'
                OR value #>> '{}' !~ '^[1-9][0-9]{0,15}$'
                OR (value #>> '{}')::numeric > 9007199254740991
        )
        AND (profile_value -> 'parse_policy' ->> 'max_text_characters')::numeric
            <= (profile_value -> 'parse_policy' ->> 'max_total_text_characters')::numeric
        AND jsonb_typeof(profile_value -> 'generation_retry_policy') = 'object'
        AND profile_value -> 'generation_retry_policy' ?& ARRAY[
            'backoff_seconds', 'max_attempts', 'strategy_version'
        ]::text[]
        AND (profile_value -> 'generation_retry_policy') - ARRAY[
            'backoff_seconds', 'max_attempts', 'strategy_version'
        ]::text[] = '{}'::jsonb
        AND profile_value -> 'generation_retry_policy' ->> 'strategy_version' = 'generation-retry-v1'
        AND jsonb_typeof(profile_value -> 'generation_retry_policy' -> 'max_attempts') = 'number'
        AND (profile_value -> 'generation_retry_policy' ->> 'max_attempts') ~ '^[1-3]$'
        AND jsonb_typeof(profile_value -> 'generation_retry_policy' -> 'backoff_seconds') = 'array'
        AND jsonb_array_length(profile_value -> 'generation_retry_policy' -> 'backoff_seconds')
            = (profile_value -> 'generation_retry_policy' ->> 'max_attempts')::integer - 1
        AND NOT EXISTS (
            SELECT 1
              FROM jsonb_array_elements(profile_value -> 'generation_retry_policy' -> 'backoff_seconds') AS entry(value)
             WHERE jsonb_typeof(value) <> 'number'
                OR value #>> '{}' !~ '^(0|[1-9][0-9]{0,15})$'
                OR (value #>> '{}')::numeric > 9007199254740991
        )
        AND jsonb_typeof(profile_value -> 'provider_id') = 'string'
        AND jsonb_typeof(profile_value -> 'model_id') = 'string'
        AND jsonb_typeof(profile_value -> 'adapter_strategy_version') = 'string'
        AND jsonb_typeof(profile_value -> 'prompt_version') = 'string'
        AND jsonb_typeof(profile_value -> 'kernel_parser_strategy_version') = 'string'
        AND jsonb_typeof(profile_value -> 'vlm_stage_strategy_version') = 'string'
        AND length(btrim(profile_value ->> 'provider_id')) > 0
        AND length(btrim(profile_value ->> 'model_id')) > 0
        AND length(btrim(profile_value ->> 'adapter_strategy_version')) > 0
        AND length(btrim(profile_value ->> 'prompt_version')) > 0
        AND length(btrim(profile_value ->> 'kernel_parser_strategy_version')) > 0
        AND length(btrim(profile_value ->> 'vlm_stage_strategy_version')) > 0
    ELSE runtime.execution_profile_semantic_v9_is_valid(profile_value, run_state)
         AND run_state IN ('succeeded', 'denied', 'failed', 'accepted', 'running',
                           'awaiting_calibration', 'recompute_needed')
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_semantic_v10_is_valid(execution_profile, state)
    ) IS TRUE);

CREATE OR REPLACE FUNCTION runtime.guard_historical_execution_profile_write()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND (OLD.execution_profile ->> 'schema_version') NOT IN (
           'pipeline-execution-profile-v9', 'pipeline-execution-profile-v10'
       ) THEN
        RAISE EXCEPTION 'historical pre-v9 execution profile rows are read-only';
    END IF;
    IF (NEW.execution_profile ->> 'schema_version') NOT IN (
        'pipeline-execution-profile-v9', 'pipeline-execution-profile-v10'
    ) THEN
        RAISE EXCEPTION 'new execution profile rows must be v9 or v10';
    END IF;
    RETURN NEW;
END $$;

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'v10 is a closed SourcePrep/VLM-only plan; it cannot carry media, story, render, publication or authority fields.';

COMMIT;
