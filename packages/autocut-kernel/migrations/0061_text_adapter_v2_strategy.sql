-- Migration 0061: Accept text adapter v2 strategy in Stage 1-3 policy constraints.
-- The Stage 1-3 wire format was upgraded from v1 (legacy nested text.format) to
-- v2 (direct {type,name,strict,schema} shape). The constraint functions must
-- accept the new adapter strategy version.
--
-- IMPORTANT: Stage 2 and Stage 3 functions are full replacements preserving
-- every nested validation. Only the adapter_strategy_version literal changes
-- from v1 to v2.

BEGIN;

-- Stage 1: based on original 0019 function, only adapter_strategy_version v1 → v2
CREATE OR REPLACE FUNCTION runtime.stage1_command_policy_shape_is_valid(value jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $function$
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
       OR generation ->> 'adapter_strategy_version' <> 'doubao-ark-text-responses-stream-v2'
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
END;
$function$;

-- Stage 2: full replacement, only adapter_strategy_version v1 → v2
CREATE OR REPLACE FUNCTION runtime.stage2_command_policy_shape_is_valid(value jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $function$
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
        AND generation ->> 'adapter_strategy_version' = 'doubao-ark-text-responses-stream-v2'
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
END $function$;

-- Stage 3: full replacement, only adapter_strategy_version v1 → v2
CREATE OR REPLACE FUNCTION runtime.stage3_command_policy_shape_is_valid(value jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $function$
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
        AND generation ->> 'adapter_strategy_version' = 'doubao-ark-text-responses-stream-v2'
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
END $function$;

COMMIT;
