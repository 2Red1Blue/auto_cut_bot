-- Freeze the complete HTTP VLM execution strategy independently of client intent.

BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.pipeline_runs
         WHERE state IN ('accepted', 'running')
    ) THEN
        RAISE EXCEPTION
            '0008 refuses legacy accepted/running pipeline runs; resolve them before migration';
    END IF;
END $$;

ALTER TABLE runtime.pipeline_runs
    ADD COLUMN execution_profile jsonb NOT NULL DEFAULT
        '{"kind":"legacy_unresolved","schema_version":"pipeline-execution-profile-v1"}'::jsonb,
    ADD COLUMN execution_profile_hash text NOT NULL DEFAULT
        'sha256:b4acaadc1ae943188bc5d5e0ea7292b5240bf9c8c6f4e3171f4bf32b2c90cee7';

ALTER TABLE runtime.pipeline_runs
    ALTER COLUMN execution_profile DROP DEFAULT,
    ALTER COLUMN execution_profile_hash DROP DEFAULT,
    ADD CONSTRAINT pipeline_runs_execution_profile_hash_check CHECK (
        execution_profile_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        jsonb_typeof(execution_profile) = 'object'
        AND execution_profile ->> 'schema_version' = 'pipeline-execution-profile-v1'
        AND (
            (
                execution_profile ->> 'kind' = 'legacy_unresolved'
                AND execution_profile
                    - ARRAY['kind', 'schema_version']::text[] = '{}'::jsonb
            )
            OR (
                execution_profile ->> 'kind' = 'doubao_vlm'
                AND execution_profile ?& ARRAY[
                    'adapter_strategy_version', 'kind', 'kernel_parser_strategy_version',
                    'model_id', 'parse_policy', 'prompt_version', 'provider_id',
                    'request_parameters', 'response_schema', 'schema_version',
                    'vlm_stage_strategy_version'
                ]::text[]
                AND execution_profile - ARRAY[
                    'adapter_strategy_version', 'kind', 'kernel_parser_strategy_version',
                    'model_id', 'parse_policy', 'prompt_version', 'provider_id',
                    'request_parameters', 'response_schema', 'schema_version',
                    'vlm_stage_strategy_version'
                ]::text[] = '{}'::jsonb
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
                AND length(btrim(execution_profile ->> 'vlm_stage_strategy_version')) > 0
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

COMMENT ON COLUMN runtime.pipeline_runs.execution_profile IS
    'Immutable closed strategy frozen at first idempotency claim; legacy_unresolved is never VLM-executable.';
COMMENT ON COLUMN runtime.pipeline_runs.execution_profile_hash IS
    'SHA-256 of the application canonical execution-profile JSON, distinct from request_hash.';

CREATE OR REPLACE FUNCTION runtime.guard_pipeline_run_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pipeline runs are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'accepted' OR NEW.version <> 0 THEN
            RAISE EXCEPTION 'pipeline run must begin accepted at version zero';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.run_id, NEW.idempotency_key, NEW.request_hash, NEW.profile,
        NEW.source_kind, NEW.source_value, NEW.execution_profile,
        NEW.execution_profile_hash, NEW.created_at)
       IS DISTINCT FROM
       (OLD.run_id, OLD.idempotency_key, OLD.request_hash, OLD.profile,
        OLD.source_kind, OLD.source_value, OLD.execution_profile,
        OLD.execution_profile_hash, OLD.created_at) THEN
        RAISE EXCEPTION 'pipeline run identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'pipeline run transition requires exact version increment';
    END IF;
    IF OLD.state IN ('succeeded', 'denied', 'failed') THEN
        RAISE EXCEPTION 'terminal pipeline run is immutable';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state
       AND NOT ((OLD.state = 'accepted' AND NEW.state IN ('running', 'succeeded', 'denied', 'failed'))
             OR (OLD.state = 'running' AND NEW.state IN ('succeeded', 'denied', 'failed'))) THEN
        RAISE EXCEPTION 'invalid pipeline run state transition';
    END IF;
    RETURN NEW;
END $$;

COMMIT;
