-- A recovered local service can discover that an indeterminate Media Preflight
-- command now lacks calibration or has a changed live timing identity.  These
-- are durable receipt-less states, not a fabricated terminal Receipt.

BEGIN;

CREATE OR REPLACE FUNCTION runtime.guard_pipeline_command_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pipeline commands are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'pending' OR NEW.version <> 0 OR NEW.lease_id IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL OR NEW.completed_at IS NOT NULL
           OR NEW.blocking_command_id IS NOT NULL THEN
            RAISE EXCEPTION 'pipeline command must begin pending at version zero';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.command_id, NEW.run_id, NEW.ordinal, NEW.stage, NEW.created_at)
       IS DISTINCT FROM
       (OLD.command_id, OLD.run_id, OLD.ordinal, OLD.stage, OLD.created_at) THEN
        RAISE EXCEPTION 'pipeline command identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'pipeline command transition requires exact version increment';
    END IF;
    IF OLD.state IN (
        'succeeded', 'denied', 'failed', 'blocked', 'recompute_needed'
    ) THEN
        RAISE EXCEPTION 'terminal pipeline command is immutable';
    END IF;
    IF NOT (
        (OLD.state = 'pending' AND NEW.state IN ('running', 'blocked'))
        OR (OLD.state = 'running' AND NEW.state IN (
            'running', 'succeeded', 'denied', 'failed', 'indeterminate',
            'awaiting_calibration', 'recompute_needed'
        ))
        OR (OLD.state = 'indeterminate' AND NEW.state IN (
            'succeeded', 'denied', 'failed', 'awaiting_calibration', 'recompute_needed'
        ))
        OR (OLD.state = 'awaiting_calibration' AND NEW.state = 'pending')
    ) THEN
        RAISE EXCEPTION 'invalid pipeline command state transition';
    END IF;
    RETURN NEW;
END $$;

COMMIT;
