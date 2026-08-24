-- Add an explicit, hash-bound generation retry policy without changing old runs.

BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

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
            )
            OR (
                execution_profile ->> 'schema_version' IN (
                    'pipeline-execution-profile-v1',
                    'pipeline-execution-profile-v2'
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
                        'kernel_parser_strategy_version', 'model_id', 'parse_policy',
                        'prompt_version', 'provider_id', 'request_parameters',
                        'response_schema', 'schema_version', 'vlm_stage_strategy_version'
                    ]::text[] = '{}'::jsonb
                AND (
                    (
                        execution_profile ->> 'schema_version'
                            = 'pipeline-execution-profile-v1'
                        AND NOT execution_profile ? 'generation_retry_policy'
                    )
                    OR (
                        execution_profile ->> 'schema_version'
                            = 'pipeline-execution-profile-v2'
                        AND execution_profile ? 'generation_retry_policy'
                        AND jsonb_typeof(
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
                        AND (
                            (execution_profile -> 'generation_retry_policy'
                                ->> 'max_attempts') = '1'
                            OR (
                                jsonb_typeof(execution_profile
                                    -> 'generation_retry_policy'
                                    -> 'backoff_seconds' -> 0) = 'number'
                                AND (execution_profile
                                    -> 'generation_retry_policy'
                                    -> 'backoff_seconds' -> 0)::text
                                    ~ '^(0|[1-9][0-9]*)$'
                                AND (
                                    (execution_profile -> 'generation_retry_policy'
                                        ->> 'max_attempts') = '2'
                                    OR (
                                        jsonb_typeof(execution_profile
                                            -> 'generation_retry_policy'
                                            -> 'backoff_seconds' -> 1) = 'number'
                                        AND (execution_profile
                                            -> 'generation_retry_policy'
                                            -> 'backoff_seconds' -> 1)::text
                                            ~ '^(0|[1-9][0-9]*)$'
                                    )
                                )
                            )
                        )
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
            )
        )
    ) IS TRUE);

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check
    ON runtime.pipeline_runs IS
    'v1 runs remain frozen at one attempt; v2 binds a closed generation retry policy.';

COMMIT;
