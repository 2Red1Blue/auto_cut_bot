-- Freeze Stage 1 policy with new HTTP runs. Do not rewrite historical runs.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.pipeline_runs
         WHERE state IN ('accepted', 'running')
           AND (execution_profile ->> 'schema_version'
                = 'pipeline-execution-profile-v6') IS NOT TRUE
    ) THEN
        RAISE EXCEPTION
            '0019 refuses accepted/running pre-v6 runs; resolve them before migration';
    END IF;
END $$;

CREATE FUNCTION runtime.stage1_policy_closed_object(value jsonb, fields text[])
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
BEGIN
    IF jsonb_typeof(value) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    RETURN value ?& fields AND value - fields = '{}'::jsonb;
END $$;

-- This database guard proves closed shape, not semantic authority. The Kernel
-- decoder and installed-source binding still verify the actual registered policy.
CREATE FUNCTION runtime.stage1_command_policy_shape_is_valid(value jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    generation jsonb;
    draft jsonb;
    retry jsonb;
    member jsonb;
    field_name text;
BEGIN
    IF NOT runtime.stage1_policy_closed_object(value, ARRAY[
        'artifact_revision', 'generation', 'draft_policy', 'coverage_policy',
        'dependency_policy', 'retry_policy'
    ]) THEN RETURN false; END IF;
    generation := value -> 'generation';
    draft := value -> 'draft_policy';
    retry := value -> 'retry_policy';
    IF NOT runtime.stage1_policy_closed_object(generation, ARRAY[
        'provider_id', 'model_id', 'prompt_version', 'prompt_template',
        'adapter_strategy_version', 'max_output_tokens', 'temperature'
    ]) OR NOT runtime.stage1_policy_closed_object(draft, ARRAY[
        'max_response_bytes', 'max_prompt_bytes', 'max_input_windows',
        'max_input_objects', 'max_beats', 'max_obligations', 'max_story_threads',
        'max_merge_proposals', 'max_references_per_item', 'max_text_characters',
        'max_total_text_characters'
    ]) OR NOT runtime.stage1_policy_closed_object(value -> 'coverage_policy', ARRAY[
        'minimum_confidence', 'coverage_mode'
    ]) OR NOT runtime.stage1_policy_closed_object(value -> 'dependency_policy', ARRAY[
        'strategy_version', 'canonical_owner_by_object_type', 'edge_projections',
        'attribute_projections', 'external_root_projections'
    ]) OR NOT runtime.stage1_policy_closed_object(retry, ARRAY[
        'strategy_version', 'max_attempts', 'backoff_seconds'
    ]) THEN RETURN false; END IF;

    FOREACH member IN ARRAY ARRAY[value -> 'artifact_revision', generation -> 'max_output_tokens',
                                   retry -> 'max_attempts'] LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number'
           OR (member #>> '{}') !~ '^[1-9][0-9]{0,15}$' THEN RETURN false; END IF;
        IF (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;
    FOR member IN SELECT item FROM jsonb_each(draft) AS entry(name, item) LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number'
           OR (member #>> '{}') !~ '^[1-9][0-9]{0,15}$' THEN RETURN false; END IF;
        IF (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;
    IF (generation ->> 'max_output_tokens')::numeric > 32768
       OR (retry ->> 'max_attempts')::numeric > 3 THEN RETURN false; END IF;
    FOREACH field_name IN ARRAY ARRAY['provider_id', 'model_id', 'prompt_version',
                                      'prompt_template', 'adapter_strategy_version', 'temperature'] LOOP
        IF jsonb_typeof(generation -> field_name) IS DISTINCT FROM 'string'
           OR length(btrim(generation ->> field_name)) = 0 THEN RETURN false; END IF;
    END LOOP;
    IF generation ->> 'provider_id' <> 'doubao-ark-text-responses-stream'
       OR generation ->> 'adapter_strategy_version' <> 'doubao-ark-text-responses-stream-v1'
       OR length(generation ->> 'temperature') > 32
       OR (generation ->> 'temperature') !~ '^(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$'
    THEN RETURN false; END IF;
    IF (generation ->> 'temperature')::numeric NOT BETWEEN 0 AND 2 THEN RETURN false; END IF;
    IF jsonb_typeof(retry -> 'backoff_seconds') IS DISTINCT FROM 'array'
    THEN RETURN false; END IF;
    IF jsonb_array_length(retry -> 'backoff_seconds') <> (retry ->> 'max_attempts')::integer - 1
    THEN RETURN false; END IF;
    FOR member IN SELECT item FROM jsonb_array_elements(retry -> 'backoff_seconds') AS entry(item) LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number'
           OR (member #>> '{}') !~ '^(0|[1-9][0-9]{0,15})$' THEN RETURN false; END IF;
        IF (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;
    RETURN COALESCE(
        retry ->> 'strategy_version' = 'generation-retry-v1'
        AND jsonb_typeof(value -> 'coverage_policy' -> 'minimum_confidence') = 'string'
        AND value -> 'coverage_policy' ->> 'coverage_mode' = 'strict_global'
        AND value -> 'dependency_policy' ->> 'strategy_version' = 'semantic-dependencies-v1'
        AND jsonb_typeof(value -> 'dependency_policy' -> 'canonical_owner_by_object_type') = 'object'
        AND jsonb_typeof(value -> 'dependency_policy' -> 'edge_projections') = 'object'
        AND jsonb_typeof(value -> 'dependency_policy' -> 'attribute_projections') = 'array'
        AND jsonb_typeof(value -> 'dependency_policy' -> 'external_root_projections') = 'array',
        false
    );
END $$;

CREATE FUNCTION runtime.execution_profile_semantic_v6_is_valid(profile_value jsonb, run_state text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v6' THEN
        runtime.execution_profile_semantic_v5_is_valid(
            jsonb_set(profile_value - 'stage1_command_policy', '{schema_version}',
                      '"pipeline-execution-profile-v5"'::jsonb), run_state
        ) AND runtime.stage1_command_policy_shape_is_valid(profile_value -> 'stage1_command_policy')
    ELSE runtime.execution_profile_semantic_v5_is_valid(profile_value, run_state)
         AND run_state IN ('succeeded', 'denied', 'failed')
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_semantic_v6_is_valid(execution_profile, state)
    ) IS TRUE);

CREATE OR REPLACE FUNCTION runtime.guard_historical_execution_profile_write()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (OLD.execution_profile ->> 'schema_version' = 'pipeline-execution-profile-v6') IS NOT TRUE THEN
            RAISE EXCEPTION 'historical pre-v6 execution profile rows are read-only';
        END IF;
    END IF;
    IF (NEW.execution_profile ->> 'schema_version' = 'pipeline-execution-profile-v6') IS NOT TRUE THEN
        RAISE EXCEPTION 'new pre-v6 execution profile rows are forbidden';
    END IF;
    RETURN NEW;
END $$;

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'Pre-v6 rows are terminal read-only history; v6 freezes VLM, Stage 1 and physical-evidence policies.';

COMMIT;
