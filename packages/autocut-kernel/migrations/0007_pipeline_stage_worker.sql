-- Ordered HTTP stages, explicit predecessor blocking, and renewable worker leases.

BEGIN;

ALTER TABLE runtime.pipeline_commands
    ADD COLUMN blocking_command_id uuid
        REFERENCES runtime.pipeline_commands (command_id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE runtime.pipeline_commands
    DROP CONSTRAINT IF EXISTS pipeline_commands_state_check,
    DROP CONSTRAINT IF EXISTS pipeline_commands_check;

ALTER TABLE runtime.pipeline_commands
    ADD CONSTRAINT pipeline_commands_state_check CHECK (
        state IN (
            'pending', 'running', 'succeeded', 'denied', 'failed',
            'indeterminate', 'blocked'
        )
    ),
    ADD CONSTRAINT pipeline_commands_lease_and_terminal_check CHECK (
        (state = 'running' AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL
                           AND completed_at IS NULL AND blocking_command_id IS NULL)
        OR (state IN ('pending', 'indeterminate') AND lease_id IS NULL
                                                    AND lease_expires_at IS NULL
                                                    AND completed_at IS NULL
                                                    AND blocking_command_id IS NULL)
        OR (state IN ('succeeded', 'denied', 'failed') AND lease_id IS NULL
                                                       AND lease_expires_at IS NULL
                                                       AND completed_at IS NOT NULL
                                                       AND blocking_command_id IS NULL)
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
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'pipeline command transition requires exact version increment';
    END IF;
    IF OLD.state IN ('succeeded', 'denied', 'failed', 'blocked') THEN
        RAISE EXCEPTION 'terminal pipeline command is immutable';
    END IF;
    IF NOT (
        (OLD.state = 'pending' AND NEW.state IN ('running', 'blocked'))
        OR (OLD.state = 'running' AND NEW.state IN (
            'running', 'succeeded', 'denied', 'failed', 'indeterminate'
        ))
        OR (OLD.state = 'indeterminate' AND NEW.state IN ('succeeded', 'denied', 'failed'))
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

-- Upgrade active 0005 runs in place. The deterministic UUID makes the
-- backfill idempotent if an operator has already inserted the VLM ordinal.
INSERT INTO runtime.pipeline_commands
    (command_id, run_id, ordinal, stage, state, version)
SELECT md5(run.run_id || ':vlm')::uuid,
       run.run_id,
       1,
       'vlm',
       'pending',
       0
  FROM runtime.pipeline_runs AS run
  JOIN runtime.pipeline_commands AS source
    ON source.run_id = run.run_id
   AND source.ordinal = 0
   AND source.stage = 'source_prep'
 WHERE run.state IN ('accepted', 'running')
   AND NOT EXISTS (
       SELECT 1 FROM runtime.pipeline_commands AS existing
        WHERE existing.run_id = run.run_id AND existing.ordinal = 1
   )
ON CONFLICT DO NOTHING;

UPDATE runtime.pipeline_commands AS vlm
   SET state = 'blocked',
       version = vlm.version + 1,
       blocking_command_id = source.command_id,
       completed_at = transaction_timestamp(),
       updated_at = transaction_timestamp()
  FROM runtime.pipeline_commands AS source,
       runtime.pipeline_runs AS run
 WHERE vlm.run_id = source.run_id
   AND run.run_id = source.run_id
   AND run.state IN ('accepted', 'running')
   AND source.ordinal = 0
   AND source.stage = 'source_prep'
   AND source.state IN ('denied', 'failed')
   AND vlm.ordinal = 1
   AND vlm.stage = 'vlm'
   AND vlm.state = 'pending';

UPDATE runtime.pipeline_runs AS run
   SET state = CASE
           WHEN EXISTS (
               SELECT 1 FROM runtime.pipeline_commands AS source
                WHERE source.run_id = run.run_id
                  AND source.ordinal = 0
                  AND source.state = 'failed'
           ) THEN 'failed'
           ELSE 'denied'
       END,
       version = run.version + 1,
       updated_at = transaction_timestamp()
 WHERE run.state IN ('accepted', 'running')
   AND EXISTS (
       SELECT 1 FROM runtime.pipeline_commands AS source
        WHERE source.run_id = run.run_id
          AND source.ordinal = 0
          AND source.state IN ('denied', 'failed')
   );

CREATE OR REPLACE FUNCTION runtime.assert_pipeline_blocker_relation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state <> 'blocked' THEN
        RETURN NULL;
    END IF;
    PERFORM 1
      FROM runtime.pipeline_commands AS blocker
     WHERE blocker.command_id = NEW.blocking_command_id
       AND blocker.run_id = NEW.run_id
       AND blocker.ordinal < NEW.ordinal
       AND blocker.state IN ('denied', 'failed');
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'blocked pipeline command requires an earlier denied/failed command in the same run';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_pipeline_blocker_relation
AFTER INSERT OR UPDATE ON runtime.pipeline_commands
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.assert_pipeline_blocker_relation();

CREATE OR REPLACE FUNCTION runtime.guard_pipeline_outbox_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pipeline outbox rows are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'pending' OR NEW.version <> 0 OR NEW.lease_id IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL THEN
            RAISE EXCEPTION 'pipeline outbox must begin pending at version zero';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.outbox_id, NEW.run_id, NEW.created_at)
       IS DISTINCT FROM (OLD.outbox_id, OLD.run_id, OLD.created_at) THEN
        RAISE EXCEPTION 'pipeline outbox identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'pipeline outbox transition requires exact version increment';
    END IF;
    IF NOT (
        (OLD.state = 'pending' AND NEW.state = 'leased')
        OR (OLD.state = 'leased' AND NEW.state IN ('leased', 'pending', 'consumed'))
        OR (OLD.state = 'consumed' AND NEW.state = 'pending')
    ) THEN
        RAISE EXCEPTION 'invalid pipeline outbox state transition';
    END IF;
    RETURN NEW;
END $$;

COMMIT;
