-- Make execution-profile v4 the closed Semantic Pack policy major.

BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.pipeline_runs
         WHERE state IN ('accepted', 'running')
           AND (execution_profile ->> 'schema_version'
               = 'pipeline-execution-profile-v4') IS NOT TRUE
    ) THEN
        RAISE EXCEPTION
            '0013 refuses accepted/running pipeline runs without v4 Semantic Pack policy';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION runtime.execution_profile_semantic_v4_is_valid(
    profile_value jsonb,
    run_state text
) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    jsonb_typeof(profile_value) = 'object'
    AND (
        (
            profile_value = '{"kind":"legacy_unresolved","schema_version":"pipeline-execution-profile-v1"}'::jsonb
            AND run_state IN ('succeeded', 'denied', 'failed')
        )
        OR (
            profile_value ->> 'kind' = 'doubao_vlm'
            AND profile_value ->> 'schema_version' IN (
                'pipeline-execution-profile-v1',
                'pipeline-execution-profile-v2',
                'pipeline-execution-profile-v3',
                'pipeline-execution-profile-v4'
            )
            AND profile_value ?& ARRAY[
                'adapter_strategy_version', 'kind', 'kernel_parser_strategy_version',
                'model_id', 'parse_policy', 'prompt_version', 'provider_id',
                'request_parameters', 'response_schema', 'schema_version',
                'vlm_stage_strategy_version'
            ]::text[]
            AND jsonb_typeof(profile_value -> 'provider_id') = 'string'
            AND length(btrim(profile_value ->> 'provider_id')) > 0
            AND jsonb_typeof(profile_value -> 'model_id') = 'string'
            AND length(btrim(profile_value ->> 'model_id')) > 0
            AND jsonb_typeof(profile_value -> 'adapter_strategy_version') = 'string'
            AND jsonb_typeof(profile_value -> 'prompt_version') = 'string'
            AND jsonb_typeof(profile_value -> 'kernel_parser_strategy_version') = 'string'
            AND jsonb_typeof(profile_value -> 'vlm_stage_strategy_version') = 'string'
            AND jsonb_typeof(profile_value -> 'response_schema') = 'object'
            AND jsonb_typeof(profile_value -> 'request_parameters') = 'object'
            AND profile_value -> 'request_parameters' ?& ARRAY[
                'adapter_strategy_version', 'max_output_tokens',
                'temperature', 'video_fps'
            ]::text[]
            AND (profile_value -> 'request_parameters') - ARRAY[
                'adapter_strategy_version', 'max_output_tokens',
                'temperature', 'video_fps'
            ]::text[] = '{}'::jsonb
            AND jsonb_typeof(profile_value -> 'parse_policy') = 'object'
            AND (
                (
                    profile_value ->> 'schema_version' IN (
                        'pipeline-execution-profile-v1',
                        'pipeline-execution-profile-v2',
                        'pipeline-execution-profile-v3'
                    )
                    AND run_state IN ('succeeded', 'denied', 'failed')
                    AND profile_value -> 'parse_policy' ?& ARRAY[
                        'max_observations', 'max_response_bytes',
                        'max_summary_characters', 'max_total_summary_characters',
                        'minimum_confidence'
                    ]::text[]
                    AND (profile_value -> 'parse_policy') - ARRAY[
                        'max_observations', 'max_response_bytes',
                        'max_summary_characters', 'max_total_summary_characters',
                        'minimum_confidence'
                    ]::text[] = '{}'::jsonb
                    AND (
                        profile_value ->> 'schema_version'
                            = 'pipeline-execution-profile-v3'
                        OR (
                            NOT profile_value ? 'media_preflight_policy'
                            AND NOT profile_value ? 'media_preflight_policy_hash'
                        )
                    )
                )
                OR (
                    profile_value ->> 'schema_version' = 'pipeline-execution-profile-v4'
                    AND profile_value -> 'parse_policy' ?& ARRAY[
                        'max_response_bytes', 'max_entities', 'max_facts',
                        'max_events', 'max_candidate_hypotheses',
                        'max_temporal_segments', 'max_measurements',
                        'max_text_characters', 'max_total_text_characters'
                    ]::text[]
                    AND (profile_value -> 'parse_policy') - ARRAY[
                        'max_response_bytes', 'max_entities', 'max_facts',
                        'max_events', 'max_candidate_hypotheses',
                        'max_temporal_segments', 'max_measurements',
                        'max_text_characters', 'max_total_text_characters'
                    ]::text[] = '{}'::jsonb
                    AND profile_value ? 'generation_retry_policy'
                    AND jsonb_typeof(profile_value -> 'generation_retry_policy') = 'object'
                    AND profile_value ? 'media_preflight_policy'
                    AND jsonb_typeof(profile_value -> 'media_preflight_policy') = 'object'
                    AND profile_value -> 'media_preflight_policy'
                        ->> 'word_timing_capability' = 'required'
                    AND profile_value ? 'media_preflight_policy_hash'
                    AND profile_value ->> 'media_preflight_policy_hash'
                        ~ '^sha256:[0-9a-f]{64}$'
                )
            )
            AND (
                (
                    profile_value ->> 'schema_version'
                        = 'pipeline-execution-profile-v1'
                    AND NOT profile_value ? 'generation_retry_policy'
                )
                OR (
                    profile_value ->> 'schema_version' IN (
                        'pipeline-execution-profile-v2',
                        'pipeline-execution-profile-v3',
                        'pipeline-execution-profile-v4'
                    )
                    AND jsonb_typeof(
                        profile_value -> 'generation_retry_policy'
                    ) = 'object'
                    AND profile_value -> 'generation_retry_policy' ?& ARRAY[
                        'backoff_seconds', 'max_attempts', 'strategy_version'
                    ]::text[]
                    AND (profile_value -> 'generation_retry_policy') - ARRAY[
                        'backoff_seconds', 'max_attempts', 'strategy_version'
                    ]::text[] = '{}'::jsonb
                    AND profile_value -> 'generation_retry_policy'
                        ->> 'strategy_version' = 'generation-retry-v1'
                    AND jsonb_typeof(
                        profile_value -> 'generation_retry_policy' -> 'max_attempts'
                    ) = 'number'
                    AND (profile_value -> 'generation_retry_policy'
                        ->> 'max_attempts') ~ '^[1-3]$'
                    AND jsonb_typeof(
                        profile_value -> 'generation_retry_policy' -> 'backoff_seconds'
                    ) = 'array'
                    AND jsonb_array_length(
                        profile_value -> 'generation_retry_policy' -> 'backoff_seconds'
                    ) = (profile_value -> 'generation_retry_policy'
                        ->> 'max_attempts')::integer - 1
                )
            )
            AND (
                (
                    profile_value ->> 'schema_version' IN (
                        'pipeline-execution-profile-v1',
                        'pipeline-execution-profile-v2'
                    )
                    AND NOT profile_value ? 'media_preflight_policy'
                    AND NOT profile_value ? 'media_preflight_policy_hash'
                )
                OR (
                    profile_value ->> 'schema_version' IN (
                        'pipeline-execution-profile-v3',
                        'pipeline-execution-profile-v4'
                    )
                    AND jsonb_typeof(profile_value -> 'media_preflight_policy') = 'object'
                    AND profile_value -> 'media_preflight_policy' ?& ARRAY[
                        'analysis_fps_denominator', 'analysis_fps_numerator',
                        'analysis_height', 'analysis_timeout_seconds', 'analysis_width',
                        'asr_model_id', 'asr_model_revision', 'asr_model_sha256',
                        'black_luma_max', 'boundary_touch_margin_milliseconds',
                        'calibrations', 'expansion_step_milliseconds',
                        'frozen_change_ppm_max', 'funasr_version',
                        'initial_left_expansion_milliseconds',
                        'initial_right_expansion_milliseconds', 'max_analysis_frames',
                        'max_expansion_count', 'max_stderr_bytes', 'max_stdout_bytes',
                        'policy_id', 'policy_version', 'probe_timeout_seconds',
                        'scene_change_ppm_min', 'shot_change_ppm_min', 'speech_device',
                        'subtitle_edge_delta_min', 'subtitle_edge_fraction_ppm_min',
                        'subtitle_min_consecutive_samples',
                        'timed_speech_calibration_sha256',
                        'timed_speech_endpoint_url', 'timed_speech_max_response_bytes',
                        'timed_speech_policy_sha256', 'timed_speech_provider_id',
                        'timed_speech_provider_version', 'timed_speech_service_sha256',
                        'timed_speech_timeout_seconds', 'torch_version',
                        'transition_change_ppm_min', 'utterance_gap_milliseconds',
                        'vad_merge_gap_milliseconds', 'vad_model_id',
                        'vad_model_revision', 'vad_model_sha256', 'white_luma_min',
                        'word_timing_capability'
                    ]::text[]
                    AND (profile_value -> 'media_preflight_policy') - ARRAY[
                        'analysis_fps_denominator', 'analysis_fps_numerator',
                        'analysis_height', 'analysis_timeout_seconds', 'analysis_width',
                        'asr_model_id', 'asr_model_revision', 'asr_model_sha256',
                        'black_luma_max', 'boundary_touch_margin_milliseconds',
                        'calibrations', 'expansion_step_milliseconds',
                        'frozen_change_ppm_max', 'funasr_version',
                        'initial_left_expansion_milliseconds',
                        'initial_right_expansion_milliseconds', 'max_analysis_frames',
                        'max_expansion_count', 'max_stderr_bytes', 'max_stdout_bytes',
                        'policy_id', 'policy_version', 'probe_timeout_seconds',
                        'scene_change_ppm_min', 'shot_change_ppm_min', 'speech_device',
                        'subtitle_edge_delta_min', 'subtitle_edge_fraction_ppm_min',
                        'subtitle_min_consecutive_samples',
                        'timed_speech_calibration_sha256',
                        'timed_speech_endpoint_url', 'timed_speech_max_response_bytes',
                        'timed_speech_policy_sha256', 'timed_speech_provider_id',
                        'timed_speech_provider_version', 'timed_speech_service_sha256',
                        'timed_speech_timeout_seconds', 'torch_version',
                        'transition_change_ppm_min', 'utterance_gap_milliseconds',
                        'vad_merge_gap_milliseconds', 'vad_model_id',
                        'vad_model_revision', 'vad_model_sha256', 'white_luma_min',
                        'word_timing_capability'
                    ]::text[] = '{}'::jsonb
                    AND profile_value -> 'media_preflight_policy'
                        ->> 'word_timing_capability' = 'required'
                    AND jsonb_typeof(
                        profile_value -> 'media_preflight_policy' -> 'calibrations'
                    ) = 'array'
                    AND jsonb_array_length(
                        profile_value -> 'media_preflight_policy' -> 'calibrations'
                    ) = 8
                    AND jsonb_typeof(profile_value -> 'media_preflight_policy_hash')
                        = 'string'
                    AND profile_value ->> 'media_preflight_policy_hash'
                        ~ '^sha256:[0-9a-f]{64}$'
                )
            )
            AND profile_value - ARRAY[
                'adapter_strategy_version', 'generation_retry_policy', 'kind',
                'kernel_parser_strategy_version', 'media_preflight_policy',
                'media_preflight_policy_hash', 'model_id', 'parse_policy',
                'prompt_version', 'provider_id', 'request_parameters',
                'response_schema', 'schema_version', 'vlm_stage_strategy_version'
            ]::text[] = '{}'::jsonb
        )
    )
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_semantic_v4_is_valid(execution_profile, state)
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
        'pipeline-execution-profile-v1',
        'pipeline-execution-profile-v2',
        'pipeline-execution-profile-v3'
    ) THEN
        RAISE EXCEPTION
            'historical v1/v2/v3 execution profile rows are read-only';
    END IF;

    IF new_schema_version IN (
        'pipeline-execution-profile-v1',
        'pipeline-execution-profile-v2',
        'pipeline-execution-profile-v3'
    ) THEN
        RAISE EXCEPTION
            'new v1/v2/v3 execution profile rows are forbidden';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS runtime_historical_execution_profile_write_guard
    ON runtime.pipeline_runs;

CREATE TRIGGER runtime_historical_execution_profile_write_guard
BEFORE INSERT OR UPDATE ON runtime.pipeline_runs
FOR EACH ROW EXECUTE FUNCTION runtime.guard_historical_execution_profile_write();

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check
    ON runtime.pipeline_runs IS
    'v1/v2/v3 are terminal read-only history; v4 is the only executable Semantic Pack profile.';

COMMIT;
