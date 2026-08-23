-- Local Pipeline MVP durable runtime core.
-- This migration is intentionally independent of legacy ArtifactBus/ORM code.

BEGIN;

CREATE SCHEMA IF NOT EXISTS runtime;

CREATE TABLE runtime.jobs (
    job_id uuid PRIMARY KEY,
    job_key text NOT NULL UNIQUE,
    profile text NOT NULL CHECK (profile IN ('test', 'shadow', 'production')),
    state text NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'denied', 'failed')),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE TABLE runtime.command_slots (
    command_slot_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    idempotency_key text NOT NULL,
    command_name text NOT NULL,
    request_hash text NOT NULL CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'denied', 'failed')),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    completed_at timestamptz,
    UNIQUE (job_id, idempotency_key),
    CHECK (
        (state IN ('pending', 'running') AND completed_at IS NULL)
        OR (state IN ('succeeded', 'denied', 'failed') AND completed_at IS NOT NULL)
    )
);

CREATE TABLE runtime.artifact_sets (
    artifact_set_id uuid PRIMARY KEY,
    command_slot_id uuid NOT NULL UNIQUE REFERENCES runtime.command_slots (command_slot_id)
        DEFERRABLE INITIALLY DEFERRED,
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id) DEFERRABLE INITIALLY DEFERRED,
    set_hash text NOT NULL CHECK (set_hash ~ '^sha256:[0-9a-f]{64}$'),
    member_count integer NOT NULL CHECK (member_count > 0),
    committed_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    -- A command slot owns exactly one committed set.  Hash equality is scoped
    -- to the Job so independently-run Jobs may produce identical content.
    UNIQUE (job_id, set_hash),
    UNIQUE (artifact_set_id, job_id)
);

CREATE TABLE runtime.artifacts (
    artifact_id uuid PRIMARY KEY,
    artifact_set_id uuid NOT NULL,
    artifact_type text NOT NULL,
    logical_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    namespace text NOT NULL,
    scope_kind text NOT NULL,
    scope_key text NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    payload_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (artifact_set_id, job_id)
        REFERENCES runtime.artifact_sets (artifact_set_id, job_id)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT runtime_artifacts_scope_revision_key
        UNIQUE (job_id, namespace, scope_kind, scope_key, artifact_type, logical_id, revision),
    UNIQUE (artifact_set_id, artifact_id)
);

CREATE TABLE runtime.artifact_set_members (
    artifact_set_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    artifact_id uuid NOT NULL,
    PRIMARY KEY (artifact_set_id, ordinal),
    UNIQUE (artifact_set_id, artifact_id),
    FOREIGN KEY (artifact_set_id, artifact_id)
        REFERENCES runtime.artifacts (artifact_set_id, artifact_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE runtime.command_receipts (
    receipt_id uuid PRIMARY KEY,
    command_slot_id uuid NOT NULL UNIQUE
        REFERENCES runtime.command_slots (command_slot_id) DEFERRABLE INITIALLY DEFERRED,
    outcome text NOT NULL CHECK (outcome IN ('succeeded', 'denied', 'failed')),
    result_artifact_set_id uuid
        REFERENCES runtime.artifact_sets (artifact_set_id) DEFERRABLE INITIALLY DEFERRED,
    failure_code text,
    failure_detail jsonb,
    recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (
        (outcome = 'succeeded' AND result_artifact_set_id IS NOT NULL AND failure_code IS NULL AND failure_detail IS NULL)
        OR (outcome IN ('denied', 'failed') AND result_artifact_set_id IS NULL AND failure_code IS NOT NULL AND failure_detail IS NOT NULL)
    )
);

CREATE TABLE runtime.logical_heads (
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id) DEFERRABLE INITIALLY DEFERRED,
    namespace text NOT NULL,
    scope_kind text NOT NULL,
    scope_key text NOT NULL,
    artifact_type text NOT NULL,
    logical_id text NOT NULL,
    artifact_id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    PRIMARY KEY (job_id, namespace, scope_kind, scope_key, artifact_type, logical_id),
    FOREIGN KEY (artifact_id) REFERENCES runtime.artifacts (artifact_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE OR REPLACE FUNCTION runtime.assert_artifact_set_complete()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    checked_set uuid;
    actual_count integer;
    max_ordinal integer;
BEGIN
    checked_set := COALESCE(NEW.artifact_set_id, OLD.artifact_set_id);
    SELECT count(*), max(ordinal) INTO actual_count, max_ordinal
      FROM runtime.artifact_set_members WHERE artifact_set_id = checked_set;
    IF actual_count <> (SELECT member_count FROM runtime.artifact_sets WHERE artifact_set_id = checked_set)
       OR actual_count = 0 OR max_ordinal <> actual_count - 1 THEN
        RAISE EXCEPTION 'artifact set members are incomplete';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_artifact_set_complete_from_set
AFTER INSERT OR UPDATE ON runtime.artifact_sets
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_artifact_set_complete();
CREATE CONSTRAINT TRIGGER runtime_artifact_set_complete_from_member
AFTER INSERT OR UPDATE OR DELETE ON runtime.artifact_set_members
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_artifact_set_complete();

CREATE OR REPLACE FUNCTION runtime.assert_receipt_set_slot()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.outcome = 'succeeded' AND NOT EXISTS (
        SELECT 1 FROM runtime.artifact_sets
         WHERE artifact_set_id = NEW.result_artifact_set_id AND command_slot_id = NEW.command_slot_id
    ) THEN
        RAISE EXCEPTION 'successful receipt must reference its command slot artifact set';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_receipt_set_slot_check
AFTER INSERT OR UPDATE ON runtime.command_receipts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_receipt_set_slot();

CREATE OR REPLACE FUNCTION runtime.assert_artifact_set_job()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM runtime.command_slots
         WHERE command_slot_id = NEW.command_slot_id AND job_id = NEW.job_id
    ) THEN
        RAISE EXCEPTION 'artifact set job must match its command slot job';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_artifact_set_job_check
AFTER INSERT OR UPDATE ON runtime.artifact_sets
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_artifact_set_job();

REVOKE ALL ON SCHEMA runtime FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA runtime FROM PUBLIC;

COMMIT;
