-- Freeze Stage 2 policy with new HTTP runs. Historical profiles remain evidence only.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.pipeline_runs
         WHERE state IN ('accepted', 'running')
           AND (execution_profile ->> 'schema_version'
                = 'pipeline-execution-profile-v7') IS NOT TRUE
    ) THEN
        RAISE EXCEPTION
            '0020 refuses accepted/running pre-v7 runs; resolve them before migration';
    END IF;
END $$;

-- This database guard proves closed shape, not policy authority.  The Kernel
-- decoder and installed-resource binding verify the registered policy itself.
CREATE FUNCTION runtime.stage2_command_policy_shape_is_valid(value jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    generation jsonb;
    draft jsonb;
    candidate jsonb;
    job jsonb;
    story jsonb;
    retry jsonb;
    member jsonb;
BEGIN
    IF NOT runtime.stage1_policy_closed_object(value, ARRAY[
        'artifact_revision', 'generation', 'max_prompt_bytes', 'draft_policy',
        'candidate_policy', 'job_policy', 'story_policy', 'retry_policy'
    ]) THEN RETURN false; END IF;
    generation := value -> 'generation';
    draft := value -> 'draft_policy';
    candidate := value -> 'candidate_policy';
    job := value -> 'job_policy';
    story := value -> 'story_policy';
    retry := value -> 'retry_policy';
    IF NOT runtime.stage1_policy_closed_object(generation, ARRAY[
        'provider_id', 'model_id', 'prompt_version', 'prompt_template',
        'adapter_strategy_version', 'max_output_tokens', 'temperature'
    ]) OR NOT runtime.stage1_policy_closed_object(draft, ARRAY[
        'max_response_bytes', 'max_json_depth', 'max_proposals',
        'max_material_requirements_per_proposal', 'max_total_material_requirements',
        'max_references_per_field', 'max_total_references', 'max_genre_tags',
        'max_text_characters', 'max_total_text_characters'
    ]) OR NOT runtime.stage1_policy_closed_object(candidate, ARRAY[
        'strategy_version', 'minimum_confidence', 'required_measurement_kinds'
    ]) OR NOT runtime.stage1_policy_closed_object(job, ARRAY[
        'policy_id', 'policy_version', 'story_design_policy_sha256', 'proposal_count',
        'selected_story_count', 'max_search_states', 'target_duration_seconds',
        'source_reuse_policy', 'source_constraints', 'completion_policy'
    ]) OR NOT runtime.stage1_policy_closed_object(story, ARRAY[
        'policy_id', 'policy_version', 'allowed_genre_tags', 'editing_profiles',
        'teaser_strategies', 'required_physical_requirements', 'selection_strategy'
    ]) OR NOT runtime.stage1_policy_closed_object(retry, ARRAY[
        'strategy_version', 'max_attempts', 'backoff_seconds'
    ]) THEN RETURN false; END IF;

    FOREACH member IN ARRAY ARRAY[
        value -> 'artifact_revision', value -> 'max_prompt_bytes',
        generation -> 'max_output_tokens', retry -> 'max_attempts'
    ] LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number'
           OR (member #>> '{}') !~ '^[1-9][0-9]{0,15}$'
           OR (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;
    FOR member IN SELECT item FROM jsonb_each(draft) AS entry(name, item) LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number'
           OR (member #>> '{}') !~ '^[1-9][0-9]{0,15}$'
           OR (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;
    IF (value ->> 'max_prompt_bytes')::numeric > 16777216
       OR (generation ->> 'max_output_tokens')::numeric > 32768
       OR (retry ->> 'max_attempts')::numeric > 3 THEN RETURN false; END IF;
    IF NOT runtime.stage1_policy_closed_object(job -> 'proposal_count', ARRAY['min', 'max'])
       OR NOT runtime.stage1_policy_closed_object(job -> 'target_duration_seconds', ARRAY['min', 'max'])
       OR NOT runtime.stage1_policy_closed_object(job -> 'source_constraints', ARRAY[
           'allowed_source_refs', 'forbidden_source_refs', 'authorization_purpose'
       ]) THEN RETURN false; END IF;
    FOREACH member IN ARRAY ARRAY[
        job -> 'proposal_count' -> 'min', job -> 'proposal_count' -> 'max',
        job -> 'selected_story_count', job -> 'max_search_states',
        job -> 'target_duration_seconds' -> 'min', job -> 'target_duration_seconds' -> 'max'
    ] LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number'
           OR (member #>> '{}') !~ '^[1-9][0-9]{0,15}$'
           OR (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;
    IF (job -> 'proposal_count' ->> 'min')::numeric > (job -> 'proposal_count' ->> 'max')::numeric
       OR (job ->> 'selected_story_count')::numeric > (job -> 'proposal_count' ->> 'max')::numeric
       OR (job -> 'target_duration_seconds' ->> 'min')::numeric
          > (job -> 'target_duration_seconds' ->> 'max')::numeric THEN RETURN false; END IF;
    IF jsonb_typeof(job -> 'source_constraints' -> 'allowed_source_refs') IS DISTINCT FROM 'array'
       OR jsonb_typeof(job -> 'source_constraints' -> 'forbidden_source_refs') IS DISTINCT FROM 'array'
       OR jsonb_typeof(job -> 'source_constraints' -> 'authorization_purpose') IS DISTINCT FROM 'string'
       OR job -> 'source_constraints' ->> 'authorization_purpose' <> 'render_source'
       OR jsonb_typeof(story -> 'allowed_genre_tags') IS DISTINCT FROM 'array'
       OR jsonb_typeof(story -> 'editing_profiles') IS DISTINCT FROM 'array'
       OR jsonb_typeof(story -> 'teaser_strategies') IS DISTINCT FROM 'array'
       OR jsonb_typeof(story -> 'required_physical_requirements') IS DISTINCT FROM 'array'
    THEN RETURN false; END IF;
    IF jsonb_typeof(retry -> 'backoff_seconds') IS DISTINCT FROM 'array'
       OR jsonb_array_length(retry -> 'backoff_seconds')
          <> (retry ->> 'max_attempts')::integer - 1 THEN RETURN false; END IF;
    FOR member IN SELECT item FROM jsonb_array_elements(retry -> 'backoff_seconds') AS entry(item) LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number'
           OR (member #>> '{}') !~ '^(0|[1-9][0-9]{0,15})$'
           OR (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;
    RETURN COALESCE(
        generation ->> 'provider_id' = 'doubao-ark-text-responses-stream'
        AND generation ->> 'adapter_strategy_version' = 'doubao-ark-text-responses-stream-v1'
        AND jsonb_typeof(generation -> 'model_id') = 'string'
        AND jsonb_typeof(generation -> 'prompt_version') = 'string'
        AND jsonb_typeof(generation -> 'prompt_template') = 'string'
        AND jsonb_typeof(generation -> 'temperature') = 'string'
        AND length(btrim(generation ->> 'model_id')) > 0
        AND length(btrim(generation ->> 'prompt_version')) > 0
        AND length(btrim(generation ->> 'prompt_template')) > 0
        AND (generation ->> 'temperature') ~ '^(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$'
        AND (generation ->> 'temperature')::numeric BETWEEN 0 AND 2
        AND candidate ->> 'strategy_version' = 'candidate-catalog-v1'
        AND jsonb_typeof(candidate -> 'minimum_confidence') = 'string'
        AND candidate ->> 'minimum_confidence' ~ '^(0|1|0\.[0-9]*[1-9])$'
        AND jsonb_typeof(candidate -> 'required_measurement_kinds') = 'array'
        AND retry ->> 'strategy_version' = 'generation-retry-v1'
        AND jsonb_typeof(job -> 'policy_id') = 'string'
        AND jsonb_typeof(job -> 'policy_version') = 'string'
        AND jsonb_typeof(job -> 'story_design_policy_sha256') = 'string'
        AND job ->> 'story_design_policy_sha256' ~ '^sha256:[0-9a-f]{64}$'
        AND job ->> 'completion_policy' = 'all_or_nothing'
        AND job ->> 'source_reuse_policy' IN ('allow', 'forbid')
        AND jsonb_typeof(story -> 'policy_id') = 'string'
        AND jsonb_typeof(story -> 'policy_version') = 'string'
        AND story ->> 'selection_strategy' = 'first_feasible_lexicographic_v1',
        false
    );
END $$;

CREATE FUNCTION runtime.execution_profile_semantic_v7_is_valid(profile_value jsonb, run_state text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v7' THEN
        runtime.execution_profile_semantic_v6_is_valid(
            jsonb_set(profile_value - 'stage2_command_policy', '{schema_version}',
                      '"pipeline-execution-profile-v6"'::jsonb), run_state
        ) AND runtime.stage2_command_policy_shape_is_valid(profile_value -> 'stage2_command_policy')
    ELSE runtime.execution_profile_semantic_v6_is_valid(profile_value, run_state)
         AND run_state IN ('succeeded', 'denied', 'failed')
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_semantic_v7_is_valid(execution_profile, state)
    ) IS TRUE);

CREATE OR REPLACE FUNCTION runtime.guard_historical_execution_profile_write()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (OLD.execution_profile ->> 'schema_version' = 'pipeline-execution-profile-v7') IS NOT TRUE THEN
            RAISE EXCEPTION 'historical pre-v7 execution profile rows are read-only';
        END IF;
    END IF;
    IF (NEW.execution_profile ->> 'schema_version' = 'pipeline-execution-profile-v7') IS NOT TRUE THEN
        RAISE EXCEPTION 'new pre-v7 execution profile rows are forbidden';
    END IF;
    RETURN NEW;
END $$;

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'Pre-v7 rows are terminal read-only history; v7 freezes VLM, Stage 1, Stage 2 and physical-evidence policies.';

COMMIT;
