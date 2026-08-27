-- Calibration availability is a target-level scheduling state, not a failed
-- Receipt and not a reason to make the Pipeline control plane unavailable.

BEGIN;

LOCK TABLE runtime.pipeline_runs IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE runtime.pipeline_commands IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE runtime.pipeline_runs
    DROP CONSTRAINT IF EXISTS pipeline_runs_state_check,
    ADD CONSTRAINT pipeline_runs_state_check CHECK (
        state IN (
            'accepted', 'running', 'awaiting_calibration', 'recompute_needed',
            'succeeded', 'denied', 'failed'
        )
    );

ALTER TABLE runtime.pipeline_commands
    DROP CONSTRAINT IF EXISTS pipeline_commands_state_check,
    DROP CONSTRAINT IF EXISTS pipeline_commands_lease_and_terminal_check,
    ADD CONSTRAINT pipeline_commands_state_check CHECK (
        state IN (
            'pending', 'running', 'succeeded', 'denied', 'failed',
            'indeterminate', 'awaiting_calibration', 'recompute_needed', 'blocked'
        )
    ),
    ADD CONSTRAINT pipeline_commands_lease_and_terminal_check CHECK (
        (state = 'running' AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL
                           AND completed_at IS NULL AND blocking_command_id IS NULL)
        OR (state IN ('pending', 'indeterminate', 'awaiting_calibration', 'recompute_needed')
            AND lease_id IS NULL AND lease_expires_at IS NULL
            AND completed_at IS NULL AND blocking_command_id IS NULL)
        OR (state IN ('succeeded', 'denied', 'failed')
            AND lease_id IS NULL AND lease_expires_at IS NULL
            AND completed_at IS NOT NULL AND blocking_command_id IS NULL)
        OR (state = 'blocked' AND lease_id IS NULL AND lease_expires_at IS NULL
                              AND completed_at IS NOT NULL
                              AND blocking_command_id IS NOT NULL
                              AND blocking_command_id <> command_id)
    );

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
        OR (OLD.state = 'indeterminate' AND NEW.state IN ('succeeded', 'denied', 'failed'))
        OR (OLD.state = 'awaiting_calibration' AND NEW.state = 'pending')
    ) THEN
        RAISE EXCEPTION 'invalid pipeline command state transition';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION runtime.assert_pipeline_command_receipt()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    checked_command uuid;
    command_state text;
    receipt_count integer;
    receipt_outcome text;
BEGIN
    checked_command := COALESCE(NEW.command_id, OLD.command_id);
    SELECT state INTO command_state
      FROM runtime.pipeline_commands WHERE command_id = checked_command;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT count(*), min(outcome) INTO receipt_count, receipt_outcome
      FROM runtime.pipeline_run_receipts WHERE command_id = checked_command;
    IF command_state IN ('succeeded', 'denied', 'failed') THEN
        IF receipt_count <> 1 OR receipt_outcome IS DISTINCT FROM command_state THEN
            RAISE EXCEPTION 'terminal pipeline command requires one matching Receipt';
        END IF;
    ELSIF receipt_count <> 0 THEN
        RAISE EXCEPTION 'non-Receipt pipeline command cannot have a Receipt';
    END IF;
    RETURN NULL;
END $$;

COMMENT ON CONSTRAINT pipeline_commands_lease_and_terminal_check
    ON runtime.pipeline_commands IS
    'awaiting_calibration and recompute_needed are receipt-less outcomes; only an explicit authority reconciliation may wake awaiting_calibration, and neither state is a retry or publication permission.';

COMMIT;
