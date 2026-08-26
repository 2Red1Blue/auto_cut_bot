-- Freeze the full Stage 3 policy and six-stage schedule for new HTTP runs.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.pipeline_runs
         WHERE state IN ('accepted', 'running')
           AND (execution_profile ->> 'schema_version'
                = 'pipeline-execution-profile-v8') IS NOT TRUE
    ) THEN
        RAISE EXCEPTION
            '0021 refuses accepted/running pre-v8 runs; resolve them before migration';
    END IF;
END $$;

CREATE FUNCTION runtime.stage3_command_policy_shape_is_valid(value jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    generation jsonb;
    draft jsonb;
    context jsonb;
    feasibility jsonb;
    retry jsonb;
    member jsonb;
BEGIN
    -- This is intentionally a structural guard, not a duplicate of the Kernel
    -- semantic evaluator.  Establish every nested object/array before using
    -- jsonb_each/jsonb_array_elements so malformed scalar values cannot raise.
    IF NOT runtime.stage1_policy_closed_object(value, ARRAY[
        'artifact_revision', 'generation', 'max_prompt_bytes', 'draft_policy',
        'context_policy', 'feasibility_policy', 'retry_policy', 'blueprint_strategy_version'
    ]) THEN RETURN false; END IF;
    generation := value -> 'generation';
    draft := value -> 'draft_policy';
    context := value -> 'context_policy';
    feasibility := value -> 'feasibility_policy';
    retry := value -> 'retry_policy';
    IF NOT runtime.stage1_policy_closed_object(generation, ARRAY[
        'provider_id', 'model_id', 'prompt_version', 'prompt_template',
        'adapter_strategy_version', 'max_output_tokens', 'temperature'
    ]) OR NOT runtime.stage1_policy_closed_object(context, ARRAY[
        'strategy', 'budget_unit', 'max_story_context_bytes', 'max_batch_context_bytes', 'max_source_members'
    ]) OR NOT runtime.stage1_policy_closed_object(draft, ARRAY[
        'budget_unit', 'max_response_bytes', 'max_json_depth', 'max_stories', 'max_beats_per_story',
        'max_total_beats', 'max_requirements_per_beat', 'max_total_requirements',
        'max_alternatives_per_requirement', 'max_total_alternatives', 'max_references_per_field',
        'max_total_references', 'max_ordering_constraints_per_story',
        'max_total_ordering_constraints', 'max_text_characters', 'max_total_text_characters'
    ]) OR NOT runtime.stage1_policy_closed_object(retry, ARRAY[
        'strategy_version', 'max_attempts', 'backoff_seconds'
    ]) OR NOT runtime.stage1_policy_closed_object(feasibility, ARRAY[
        'strategy_version', 'max_search_states'
    ]) THEN RETURN false; END IF;

    FOREACH member IN ARRAY ARRAY[
        value -> 'artifact_revision', value -> 'max_prompt_bytes',
        generation -> 'max_output_tokens', retry -> 'max_attempts',
        feasibility -> 'max_search_states', context -> 'max_story_context_bytes',
        context -> 'max_batch_context_bytes', context -> 'max_source_members'
    ] LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number' THEN RETURN false; END IF;
        IF (member #>> '{}') !~ '^[1-9][0-9]{0,15}$' THEN RETURN false; END IF;
        IF (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;
    FOREACH member IN ARRAY ARRAY[
        draft -> 'max_response_bytes', draft -> 'max_json_depth', draft -> 'max_stories',
        draft -> 'max_beats_per_story', draft -> 'max_total_beats',
        draft -> 'max_requirements_per_beat', draft -> 'max_total_requirements',
        draft -> 'max_alternatives_per_requirement', draft -> 'max_total_alternatives',
        draft -> 'max_references_per_field', draft -> 'max_total_references',
        draft -> 'max_ordering_constraints_per_story', draft -> 'max_total_ordering_constraints',
        draft -> 'max_text_characters', draft -> 'max_total_text_characters'
    ] LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number' THEN RETURN false; END IF;
        IF (member #>> '{}') !~ '^[1-9][0-9]{0,15}$' THEN RETURN false; END IF;
        IF (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;
    IF (value ->> 'max_prompt_bytes')::numeric > 16777216
       OR (generation ->> 'max_output_tokens')::numeric > 32768
       OR (retry ->> 'max_attempts')::numeric > 3
       OR (feasibility ->> 'max_search_states')::numeric > 1000000
       OR (context ->> 'max_story_context_bytes')::numeric > 67108864
       OR (context ->> 'max_batch_context_bytes')::numeric > 67108864
       OR (context ->> 'max_source_members')::numeric > 8192
       OR (draft ->> 'max_response_bytes')::numeric > 16777216
       OR (draft ->> 'max_json_depth')::numeric > 64
       OR (draft ->> 'max_stories')::numeric > 128
       OR (draft ->> 'max_beats_per_story')::numeric > 128
       OR (draft ->> 'max_total_beats')::numeric > 1024
       OR (draft ->> 'max_requirements_per_beat')::numeric > 64
       OR (draft ->> 'max_total_requirements')::numeric > 4096
       OR (draft ->> 'max_alternatives_per_requirement')::numeric > 128
       OR (draft ->> 'max_total_alternatives')::numeric > 8192
       OR (draft ->> 'max_references_per_field')::numeric > 1024
       OR (draft ->> 'max_total_references')::numeric > 65536
       OR (draft ->> 'max_ordering_constraints_per_story')::numeric > 1024
       OR (draft ->> 'max_total_ordering_constraints')::numeric > 8192
       OR (draft ->> 'max_text_characters')::numeric > 65536
       OR (draft ->> 'max_total_text_characters')::numeric > 4194304
       OR (draft ->> 'max_text_characters')::numeric > (draft ->> 'max_total_text_characters')::numeric
    THEN RETURN false; END IF;
    IF jsonb_typeof(retry -> 'backoff_seconds') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    IF jsonb_array_length(retry -> 'backoff_seconds')
       <> (retry ->> 'max_attempts')::integer - 1 THEN RETURN false; END IF;
    FOR member IN SELECT item FROM jsonb_array_elements(retry -> 'backoff_seconds') AS entry(item) LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number' THEN RETURN false; END IF;
        IF (member #>> '{}') !~ '^(0|[1-9][0-9]{0,15})$' THEN RETURN false; END IF;
        IF (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;

    RETURN COALESCE(
        value ->> 'blueprint_strategy_version' = 'unpartitioned-batch-v1'
        AND feasibility ->> 'strategy_version' = 'editorial-material-feasibility-v1'
        AND context ->> 'strategy' = 'unpartitioned-batch-v1'
        AND context ->> 'budget_unit' = 'bytes'
        AND draft ->> 'budget_unit' = 'bytes'
        AND retry ->> 'strategy_version' = 'generation-retry-v1'
        AND generation ->> 'provider_id' = 'doubao-ark-text-responses-stream'
        AND generation ->> 'adapter_strategy_version' = 'doubao-ark-text-responses-stream-v1'
        AND jsonb_typeof(generation -> 'provider_id') = 'string'
        AND jsonb_typeof(generation -> 'model_id') = 'string'
        AND jsonb_typeof(generation -> 'prompt_version') = 'string'
        AND jsonb_typeof(generation -> 'prompt_template') = 'string'
        AND jsonb_typeof(generation -> 'adapter_strategy_version') = 'string'
        AND jsonb_typeof(generation -> 'temperature') = 'string'
        AND length(btrim(generation ->> 'model_id')) > 0
        AND length(btrim(generation ->> 'prompt_version')) > 0
        AND length(btrim(generation ->> 'prompt_template')) > 0
        AND (generation ->> 'temperature') ~ '^(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$'
        AND (generation ->> 'temperature')::numeric BETWEEN 0 AND 2,
        false
    );
END $$;

CREATE FUNCTION runtime.execution_profile_semantic_v8_is_valid(profile_value jsonb, run_state text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v8' THEN
        runtime.execution_profile_semantic_v7_is_valid(
            jsonb_set(profile_value - 'stage3_command_policy', '{schema_version}',
                      '"pipeline-execution-profile-v7"'::jsonb), run_state
        ) AND runtime.stage3_command_policy_shape_is_valid(profile_value -> 'stage3_command_policy')
    ELSE runtime.execution_profile_semantic_v7_is_valid(profile_value, run_state)
         AND run_state IN ('succeeded', 'denied', 'failed')
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_semantic_v8_is_valid(execution_profile, state)
    ) IS TRUE);

CREATE OR REPLACE FUNCTION runtime.guard_historical_execution_profile_write()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND (OLD.execution_profile ->> 'schema_version' = 'pipeline-execution-profile-v8') IS NOT TRUE THEN
        RAISE EXCEPTION 'historical pre-v8 execution profile rows are read-only';
    END IF;
    IF (NEW.execution_profile ->> 'schema_version' = 'pipeline-execution-profile-v8') IS NOT TRUE THEN
        RAISE EXCEPTION 'new pre-v8 execution profile rows are forbidden';
    END IF;
    RETURN NEW;
END $$;

COMMIT;
