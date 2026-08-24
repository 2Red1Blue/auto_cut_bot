-- Durable HTTP pipeline run identity, command leases, Receipts and outbox.
-- This control plane schedules typed commands but never claims downstream success.

BEGIN;

CREATE SCHEMA IF NOT EXISTS runtime;

CREATE TABLE runtime.pipeline_runs (
    run_id text PRIMARY KEY CHECK (run_id ~ '^pipeline_run_[0-9a-f]{32}$'),
    idempotency_key text NOT NULL,
    request_hash text NOT NULL CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),
    profile text NOT NULL CHECK (profile IN ('test', 'shadow')),
    source_kind text NOT NULL CHECK (source_kind IN ('root', 'reference')),
    source_value text NOT NULL CHECK (length(btrim(source_value)) > 0),
    state text NOT NULL CHECK (
        state IN ('accepted', 'running', 'succeeded', 'denied', 'failed')
    ),
    version bigint NOT NULL CHECK (version >= 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (idempotency_key)
);

CREATE TABLE runtime.pipeline_commands (
    command_id uuid PRIMARY KEY,
    run_id text NOT NULL REFERENCES runtime.pipeline_runs (run_id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    stage text NOT NULL CHECK (length(btrim(stage)) > 0),
    state text NOT NULL CHECK (
        state IN ('pending', 'running', 'succeeded', 'denied', 'failed', 'indeterminate')
    ),
    version bigint NOT NULL CHECK (version >= 0),
    lease_id text CHECK (lease_id IS NULL OR length(btrim(lease_id)) > 0),
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    completed_at timestamptz,
    UNIQUE (run_id, ordinal),
    UNIQUE (run_id, stage),
    CHECK (
        (state = 'running' AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL
                           AND completed_at IS NULL)
        OR (state IN ('pending', 'indeterminate') AND lease_id IS NULL
                                                    AND lease_expires_at IS NULL
                                                    AND completed_at IS NULL)
        OR (state IN ('succeeded', 'denied', 'failed') AND lease_id IS NULL
                                                       AND lease_expires_at IS NULL
                                                       AND completed_at IS NOT NULL)
    )
);

CREATE TABLE runtime.pipeline_run_receipts (
    receipt_id uuid PRIMARY KEY,
    command_id uuid NOT NULL UNIQUE REFERENCES runtime.pipeline_commands (command_id)
        DEFERRABLE INITIALLY DEFERRED,
    outcome text NOT NULL CHECK (outcome IN ('succeeded', 'denied', 'failed')),
    recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE TABLE runtime.pipeline_run_outbox (
    outbox_id uuid PRIMARY KEY,
    run_id text NOT NULL UNIQUE REFERENCES runtime.pipeline_runs (run_id),
    state text NOT NULL CHECK (state IN ('pending', 'leased', 'consumed')),
    version bigint NOT NULL CHECK (version >= 0),
    lease_id text CHECK (lease_id IS NULL OR length(btrim(lease_id)) > 0),
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (
        (state = 'leased' AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state IN ('pending', 'consumed') AND lease_id IS NULL AND lease_expires_at IS NULL)
    )
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
        NEW.source_kind, NEW.source_value, NEW.created_at)
       IS DISTINCT FROM
       (OLD.run_id, OLD.idempotency_key, OLD.request_hash, OLD.profile,
        OLD.source_kind, OLD.source_value, OLD.created_at) THEN
        RAISE EXCEPTION 'pipeline run identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'pipeline run transition requires exact version increment';
    END IF;
    IF OLD.state IN ('succeeded', 'denied', 'failed') THEN
        RAISE EXCEPTION 'terminal pipeline run is immutable';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state
       AND NOT ((OLD.state = 'accepted' AND NEW.state IN ('running', 'succeeded', 'denied', 'failed'))
             OR (OLD.state = 'running' AND NEW.state IN ('succeeded', 'denied', 'failed'))) THEN
        RAISE EXCEPTION 'invalid pipeline run state transition';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_pipeline_run_transition_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.pipeline_runs
FOR EACH ROW EXECUTE FUNCTION runtime.guard_pipeline_run_transition();

CREATE OR REPLACE FUNCTION runtime.guard_pipeline_command_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pipeline commands are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'pending' OR NEW.version <> 0 OR NEW.lease_id IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL OR NEW.completed_at IS NOT NULL THEN
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
    IF OLD.state IN ('succeeded', 'denied', 'failed') THEN
        RAISE EXCEPTION 'terminal pipeline command is immutable';
    END IF;
    IF NOT (
        (OLD.state = 'pending' AND NEW.state = 'running')
        OR (OLD.state = 'running' AND NEW.state IN ('succeeded', 'denied', 'failed', 'indeterminate'))
        OR (OLD.state = 'indeterminate' AND NEW.state IN ('succeeded', 'denied', 'failed'))
    ) THEN
        RAISE EXCEPTION 'invalid pipeline command state transition';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_pipeline_command_transition_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.pipeline_commands
FOR EACH ROW EXECUTE FUNCTION runtime.guard_pipeline_command_transition();

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
        RAISE EXCEPTION 'nonterminal pipeline command cannot have a Receipt';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_pipeline_command_receipt_from_command
AFTER INSERT OR UPDATE ON runtime.pipeline_commands
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.assert_pipeline_command_receipt();
CREATE CONSTRAINT TRIGGER runtime_pipeline_command_receipt_from_receipt
AFTER INSERT OR UPDATE OR DELETE ON runtime.pipeline_run_receipts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.assert_pipeline_command_receipt();

CREATE OR REPLACE FUNCTION runtime.prevent_pipeline_receipt_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'pipeline run Receipts are immutable';
END $$;

CREATE TRIGGER runtime_pipeline_receipt_no_mutation
BEFORE UPDATE OR DELETE ON runtime.pipeline_run_receipts
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_pipeline_receipt_mutation();

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
        OR (OLD.state = 'leased' AND NEW.state IN ('pending', 'consumed'))
        OR (OLD.state = 'consumed' AND NEW.state = 'pending')
    ) THEN
        RAISE EXCEPTION 'invalid pipeline outbox state transition';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_pipeline_outbox_transition_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.pipeline_run_outbox
FOR EACH ROW EXECUTE FUNCTION runtime.guard_pipeline_outbox_transition();

REVOKE ALL ON runtime.pipeline_runs FROM PUBLIC;
REVOKE ALL ON runtime.pipeline_commands FROM PUBLIC;
REVOKE ALL ON runtime.pipeline_run_receipts FROM PUBLIC;
REVOKE ALL ON runtime.pipeline_run_outbox FROM PUBLIC;

COMMIT;
