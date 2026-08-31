-- Persist a closed VLM recompute selection as immutable Run identity.

BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE runtime.pipeline_runs
    ADD COLUMN recompute_request jsonb,
    ADD COLUMN recompute_request_hash text,
    ADD CONSTRAINT pipeline_runs_recompute_request_pair_check CHECK (
        (recompute_request IS NULL AND recompute_request_hash IS NULL)
        OR (
            jsonb_typeof(recompute_request) = 'object'
            AND recompute_request_hash ~ '^sha256:[0-9a-f]{64}$'
        )
    );

COMMENT ON COLUMN runtime.pipeline_runs.recompute_request IS
    'Optional immutable closed VLM recompute request; canonical shape/hash is revalidated by the runtime reader.';
COMMENT ON COLUMN runtime.pipeline_runs.recompute_request_hash IS
    'Canonical SHA-256 of recompute_request; part of immutable Run identity.';

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
        NEW.execution_profile_hash, NEW.recompute_request,
        NEW.recompute_request_hash, NEW.created_at)
       IS DISTINCT FROM
       (OLD.run_id, OLD.idempotency_key, OLD.request_hash, OLD.profile,
        OLD.source_kind, OLD.source_value, OLD.execution_profile,
        OLD.execution_profile_hash, OLD.recompute_request,
        OLD.recompute_request_hash, OLD.created_at) THEN
        RAISE EXCEPTION 'pipeline run identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'pipeline run transition requires exact version increment';
    END IF;
    IF OLD.state IN ('succeeded', 'denied', 'failed', 'recompute_needed') THEN
        RAISE EXCEPTION 'terminal pipeline run is immutable';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state
       AND NOT (
            (OLD.state = 'accepted' AND NEW.state IN (
                'running', 'awaiting_calibration', 'recompute_needed',
                'succeeded', 'denied', 'failed'
            ))
            OR (OLD.state = 'running' AND NEW.state IN (
                'awaiting_calibration', 'recompute_needed',
                'succeeded', 'denied', 'failed'
            ))
            OR (OLD.state = 'awaiting_calibration' AND NEW.state = 'accepted')
       ) THEN
        RAISE EXCEPTION 'invalid pipeline run state transition';
    END IF;
    RETURN NEW;
END $$;

COMMIT;
