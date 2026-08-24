-- Freeze the complete media-preflight/FunASR strategy for every new pipeline run.

BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.pipeline_runs
         WHERE state IN ('accepted', 'running')
           AND execution_profile ->> 'schema_version'
               <> 'pipeline-execution-profile-v3'
    ) THEN
        RAISE EXCEPTION
            '0012 refuses accepted/running pipeline runs without frozen media-preflight identity';
    END IF;
END $$;

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        jsonb_typeof(execution_profile) = 'object'
        AND (
            (
                execution_profile ->> 'schema_version' = 'pipeline-execution-profile-v1'
                AND execution_profile ->> 'kind' = 'legacy_unresolved'
                AND execution_profile
                    - ARRAY['kind', 'schema_version']::text[] = '{}'::jsonb
                AND state IN ('succeeded', 'denied', 'failed')
            )
            OR (
                execution_profile ->> 'schema_version' IN (
                    'pipeline-execution-profile-v1',
                    'pipeline-execution-profile-v2',
                    'pipeline-execution-profile-v3'
                )
                AND execution_profile ->> 'kind' = 'doubao_vlm'
                AND execution_profile ?& ARRAY[
                    'adapter_strategy_version', 'kind', 'kernel_parser_strategy_version',
                    'model_id', 'parse_policy', 'prompt_version', 'provider_id',
                    'request_parameters', 'response_schema', 'schema_version',
                    'vlm_stage_strategy_version'
                ]::text[]
                AND execution_profile
                    - ARRAY[
                        'adapter_strategy_version', 'generation_retry_policy', 'kind',
                        'kernel_parser_strategy_version', 'media_preflight_policy',
                        'media_preflight_policy_hash', 'model_id', 'parse_policy',
                        'prompt_version', 'provider_id', 'request_parameters',
                        'response_schema', 'schema_version', 'vlm_stage_strategy_version'
                    ]::text[] = '{}'::jsonb
                AND (
                    (
                        execution_profile ->> 'schema_version'
                            = 'pipeline-execution-profile-v1'
                        AND NOT execution_profile ? 'generation_retry_policy'
                        AND NOT execution_profile ? 'media_preflight_policy'
                        AND NOT execution_profile ? 'media_preflight_policy_hash'
                        AND state IN ('succeeded', 'denied', 'failed')
                    )
                    OR (
                        execution_profile ->> 'schema_version'
                            = 'pipeline-execution-profile-v2'
                        AND execution_profile ? 'generation_retry_policy'
                        AND NOT execution_profile ? 'media_preflight_policy'
                        AND NOT execution_profile ? 'media_preflight_policy_hash'
                        AND state IN ('succeeded', 'denied', 'failed')
                    )
                    OR (
                        execution_profile ->> 'schema_version'
                            = 'pipeline-execution-profile-v3'
                        AND execution_profile ? 'generation_retry_policy'
                        AND execution_profile ? 'media_preflight_policy'
                        AND execution_profile ? 'media_preflight_policy_hash'
                    )
                )
                AND (
                    execution_profile ->> 'schema_version'
                        = 'pipeline-execution-profile-v1'
                    OR (
                        jsonb_typeof(
                            execution_profile -> 'generation_retry_policy'
                        ) = 'object'
                        AND (execution_profile -> 'generation_retry_policy') ?& ARRAY[
                            'backoff_seconds', 'max_attempts', 'strategy_version'
                        ]::text[]
                        AND (execution_profile -> 'generation_retry_policy')
                            - ARRAY[
                                'backoff_seconds', 'max_attempts', 'strategy_version'
                            ]::text[] = '{}'::jsonb
                        AND execution_profile -> 'generation_retry_policy'
                            ->> 'strategy_version' = 'generation-retry-v1'
                        AND jsonb_typeof(
                            execution_profile -> 'generation_retry_policy'
                            -> 'max_attempts'
                        ) = 'number'
                        AND (execution_profile -> 'generation_retry_policy'
                            ->> 'max_attempts') ~ '^[1-3]$'
                        AND jsonb_typeof(
                            execution_profile -> 'generation_retry_policy'
                            -> 'backoff_seconds'
                        ) = 'array'
                        AND jsonb_array_length(
                            execution_profile -> 'generation_retry_policy'
                            -> 'backoff_seconds'
                        ) = (execution_profile -> 'generation_retry_policy'
                            ->> 'max_attempts')::integer - 1
                    )
                )
                AND jsonb_typeof(execution_profile -> 'provider_id') = 'string'
                AND length(btrim(execution_profile ->> 'provider_id')) > 0
                AND jsonb_typeof(execution_profile -> 'model_id') = 'string'
                AND length(btrim(execution_profile ->> 'model_id')) > 0
                AND jsonb_typeof(execution_profile -> 'adapter_strategy_version') = 'string'
                AND length(btrim(execution_profile ->> 'adapter_strategy_version')) > 0
                AND jsonb_typeof(execution_profile -> 'prompt_version') = 'string'
                AND length(btrim(execution_profile ->> 'prompt_version')) > 0
                AND jsonb_typeof(
                    execution_profile -> 'kernel_parser_strategy_version'
                ) = 'string'
                AND length(btrim(
                    execution_profile ->> 'kernel_parser_strategy_version'
                )) > 0
                AND jsonb_typeof(execution_profile -> 'vlm_stage_strategy_version') = 'string'
                AND length(btrim(
                    execution_profile ->> 'vlm_stage_strategy_version'
                )) > 0
                AND jsonb_typeof(execution_profile -> 'response_schema') = 'object'
                AND jsonb_typeof(execution_profile -> 'request_parameters') = 'object'
                AND execution_profile -> 'request_parameters' ?& ARRAY[
                    'adapter_strategy_version', 'max_output_tokens',
                    'temperature', 'video_fps'
                ]::text[]
                AND (execution_profile -> 'request_parameters') - ARRAY[
                    'adapter_strategy_version', 'max_output_tokens',
                    'temperature', 'video_fps'
                ]::text[] = '{}'::jsonb
                AND jsonb_typeof(execution_profile -> 'parse_policy') = 'object'
                AND execution_profile -> 'parse_policy' ?& ARRAY[
                    'max_observations', 'max_response_bytes',
                    'max_summary_characters', 'max_total_summary_characters',
                    'minimum_confidence'
                ]::text[]
                AND (execution_profile -> 'parse_policy') - ARRAY[
                    'max_observations', 'max_response_bytes',
                    'max_summary_characters', 'max_total_summary_characters',
                    'minimum_confidence'
                ]::text[] = '{}'::jsonb
                AND (
                    execution_profile ->> 'schema_version'
                        <> 'pipeline-execution-profile-v3'
                    OR (
                        jsonb_typeof(
                            execution_profile -> 'media_preflight_policy'
                        ) = 'object'
                        AND execution_profile -> 'media_preflight_policy' ?& ARRAY[
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
                            'timed_speech_policy_sha256',
                            'timed_speech_provider_id', 'timed_speech_provider_version',
                            'timed_speech_service_sha256',
                            'timed_speech_timeout_seconds', 'torch_version',
                            'transition_change_ppm_min', 'vad_model_id',
                            'vad_merge_gap_milliseconds', 'vad_model_revision',
                            'vad_model_sha256', 'white_luma_min',
                            'utterance_gap_milliseconds',
                            'word_timing_capability'
                        ]::text[]
                        AND (execution_profile -> 'media_preflight_policy') - ARRAY[
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
                            'timed_speech_policy_sha256',
                            'timed_speech_provider_id', 'timed_speech_provider_version',
                            'timed_speech_service_sha256',
                            'timed_speech_timeout_seconds', 'torch_version',
                            'transition_change_ppm_min', 'vad_model_id',
                            'vad_merge_gap_milliseconds', 'vad_model_revision',
                            'vad_model_sha256', 'white_luma_min',
                            'utterance_gap_milliseconds',
                            'word_timing_capability'
                        ]::text[] = '{}'::jsonb
                        AND execution_profile -> 'media_preflight_policy'
                            ->> 'word_timing_capability' = 'required'
                        AND jsonb_typeof(
                            execution_profile -> 'media_preflight_policy' -> 'calibrations'
                        ) = 'array'
                        AND jsonb_array_length(
                            execution_profile -> 'media_preflight_policy' -> 'calibrations'
                        ) = 8
                        AND jsonb_typeof(
                            execution_profile -> 'media_preflight_policy_hash'
                        ) = 'string'
                        AND execution_profile ->> 'media_preflight_policy_hash'
                            ~ '^sha256:[0-9a-f]{64}$'
                    )
                )
            )
        )
    ) IS TRUE);

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check
    ON runtime.pipeline_runs IS
    'v3 runs freeze VLM, retry, media-preflight, SenseVoice, FSMN-VAD, timing and calibration identity; older profiles are terminal-only.';

COMMIT;
