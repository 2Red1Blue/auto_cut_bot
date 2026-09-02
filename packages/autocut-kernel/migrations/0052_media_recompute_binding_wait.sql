-- A media recompute target is reserved before external evidence binding.
-- awaiting_binding is receipt-less and non-executable; activation moves it
-- back to pending after the binder commits its immutable evidence claims.

BEGIN;

ALTER TABLE runtime.pipeline_commands
    DROP CONSTRAINT IF EXISTS pipeline_commands_state_check,
    DROP CONSTRAINT IF EXISTS pipeline_commands_lease_and_terminal_check,
    ADD CONSTRAINT pipeline_commands_state_check CHECK (
        state IN (
            'pending', 'running', 'succeeded', 'denied', 'failed',
            'indeterminate', 'awaiting_calibration', 'recompute_needed',
            'awaiting_binding', 'binding', 'blocked'
        )
    ),
    ADD CONSTRAINT pipeline_commands_lease_and_terminal_check CHECK (
        (state = 'running' AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL
                           AND completed_at IS NULL AND blocking_command_id IS NULL)
        OR (state = 'binding' AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL
                              AND completed_at IS NULL AND blocking_command_id IS NULL)
        OR (state IN (
                'pending', 'indeterminate', 'awaiting_calibration',
                'recompute_needed', 'awaiting_binding'
            ) AND lease_id IS NULL AND lease_expires_at IS NULL
                AND completed_at IS NULL AND blocking_command_id IS NULL)
        OR (state IN ('succeeded', 'denied', 'failed')
            AND lease_id IS NULL AND lease_expires_at IS NULL
            AND completed_at IS NOT NULL AND blocking_command_id IS NULL)
        OR (state = 'blocked' AND lease_id IS NULL AND lease_expires_at IS NULL
                              AND completed_at IS NOT NULL
                              AND blocking_command_id IS NOT NULL
                              AND blocking_command_id <> command_id)
    );

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
    IF NEW.state IN ('awaiting_binding', 'binding') THEN
        IF NEW.stage <> 'media_preflight' OR NOT EXISTS (
            SELECT 1
              FROM runtime.pipeline_runs AS run
             WHERE run.run_id = NEW.run_id
               AND run.recompute_request->>'stage' = 'media_preflight'
        ) THEN
            RAISE EXCEPTION 'awaiting_binding/binding is only valid for media recompute targets';
        END IF;
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
        (OLD.state = 'pending' AND NEW.state IN ('running', 'blocked', 'awaiting_binding'))
        OR (OLD.state = 'awaiting_binding' AND NEW.state = 'binding')
        OR (
            OLD.state = 'binding' AND NEW.state IN ('binding', 'pending', 'awaiting_binding')
            AND (
                NEW.state <> 'binding'
                OR NEW.lease_id = OLD.lease_id
                OR OLD.lease_expires_at < transaction_timestamp()
            )
        )
        OR (OLD.state = 'running' AND NEW.state IN (
            'running', 'succeeded', 'denied', 'failed', 'indeterminate',
            'awaiting_calibration', 'recompute_needed'
        ))
        OR (OLD.state = 'indeterminate' AND NEW.state IN (
            'succeeded', 'denied', 'failed', 'awaiting_calibration',
            'recompute_needed'
        ))
        OR (OLD.state = 'awaiting_calibration' AND NEW.state = 'pending')
    ) THEN
        RAISE EXCEPTION 'invalid pipeline command state transition';
    END IF;
    RETURN NEW;
END $$;

COMMIT;
