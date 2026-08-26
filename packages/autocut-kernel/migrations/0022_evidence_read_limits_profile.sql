-- Freeze independent evidence JSON budgets; do not backfill historical runs.
BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.pipeline_runs
         WHERE state IN ('accepted', 'running')
           AND (execution_profile ->> 'schema_version'
                = 'pipeline-execution-profile-v9') IS NOT TRUE
    ) THEN
        RAISE EXCEPTION
            '0022 refuses accepted/running pre-v9 runs; resolve them before migration';
    END IF;
END $$;

CREATE FUNCTION runtime.evidence_read_limits_shape_is_valid(value jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    member jsonb;
BEGIN
    IF NOT runtime.stage1_policy_closed_object(value, ARRAY[
        'max_blob_bytes', 'max_total_blob_bytes'
    ]) THEN RETURN false; END IF;
    FOREACH member IN ARRAY ARRAY[value -> 'max_blob_bytes', value -> 'max_total_blob_bytes'] LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'number' THEN RETURN false; END IF;
        -- Check the JSON numeric representation before casting: 1.0 is not an
        -- integer leaf. Exact numeric, never floating point, preserves limits.
        IF (member #>> '{}') !~ '^[1-9][0-9]{0,15}$' THEN RETURN false; END IF;
        IF (member #>> '{}')::numeric > 9007199254740991 THEN RETURN false; END IF;
    END LOOP;
    RETURN (value ->> 'max_blob_bytes')::numeric <= (value ->> 'max_total_blob_bytes')::numeric;
END $$;

CREATE FUNCTION runtime.execution_profile_semantic_v9_is_valid(profile_value jsonb, run_state text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    CASE WHEN profile_value ->> 'schema_version' = 'pipeline-execution-profile-v9' THEN
        runtime.execution_profile_semantic_v8_is_valid(
            jsonb_set(profile_value - 'evidence_read_limits', '{schema_version}',
                      '"pipeline-execution-profile-v8"'::jsonb), run_state
        ) AND runtime.evidence_read_limits_shape_is_valid(profile_value -> 'evidence_read_limits')
    ELSE runtime.execution_profile_semantic_v8_is_valid(profile_value, run_state)
         AND run_state IN ('succeeded', 'denied', 'failed')
    END
);

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT pipeline_runs_execution_profile_closed_check,
    ADD CONSTRAINT pipeline_runs_execution_profile_closed_check CHECK ((
        runtime.execution_profile_semantic_v9_is_valid(execution_profile, state)
    ) IS TRUE);

CREATE OR REPLACE FUNCTION runtime.guard_historical_execution_profile_write()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND (OLD.execution_profile ->> 'schema_version' = 'pipeline-execution-profile-v9') IS NOT TRUE THEN
        RAISE EXCEPTION 'historical pre-v9 execution profile rows are read-only';
    END IF;
    IF (NEW.execution_profile ->> 'schema_version' = 'pipeline-execution-profile-v9') IS NOT TRUE THEN
        RAISE EXCEPTION 'new pre-v9 execution profile rows are forbidden';
    END IF;
    RETURN NEW;
END $$;

COMMENT ON CONSTRAINT pipeline_runs_execution_profile_closed_check ON runtime.pipeline_runs IS
    'Pre-v9 rows remain terminal read-only history; v9 freezes independent per-blob and whole-batch evidence byte budgets.';

COMMIT;
