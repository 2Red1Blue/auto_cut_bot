-- Durable provider evidence and explicit Job finalization.
-- Extends the runtime core without introducing a generic persistence escape hatch.

BEGIN;

CREATE SCHEMA IF NOT EXISTS storage;

CREATE TABLE storage.blob_objects (
    object_id uuid PRIMARY KEY,
    content_hash text NOT NULL UNIQUE CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    byte_length bigint NOT NULL CHECK (byte_length >= 0),
    media_type text NOT NULL CHECK (length(btrim(media_type)) > 0),
    content_bytes bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (octet_length(content_bytes) = byte_length),
    CHECK (content_hash = 'sha256:' || encode(sha256(content_bytes), 'hex'))
);

CREATE TABLE storage.blob_claims (
    blob_claim_id uuid PRIMARY KEY,
    object_id uuid NOT NULL REFERENCES storage.blob_objects (object_id),
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    claimed_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (job_id, object_id)
);

CREATE OR REPLACE FUNCTION storage.prevent_blob_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable blob objects and claims cannot be mutated';
END $$;

CREATE TRIGGER storage_blob_object_no_mutation
BEFORE UPDATE OR DELETE ON storage.blob_objects
FOR EACH ROW EXECUTE FUNCTION storage.prevent_blob_mutation();

CREATE TRIGGER storage_blob_claim_no_mutation
BEFORE UPDATE OR DELETE ON storage.blob_claims
FOR EACH ROW EXECUTE FUNCTION storage.prevent_blob_mutation();

CREATE TABLE runtime.generation_attempts (
    attempt_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    command_slot_id uuid NOT NULL UNIQUE REFERENCES runtime.command_slots (command_slot_id),
    request_hash text NOT NULL CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),
    provider_id text NOT NULL CHECK (length(btrim(provider_id)) > 0),
    provider_idempotency_key text NOT NULL CHECK (
        length(btrim(provider_idempotency_key)) > 0
    ),
    request_payload_object_id uuid NOT NULL REFERENCES storage.blob_objects (object_id),
    state text NOT NULL CHECK (
        state IN ('reserved', 'dispatched', 'responded', 'indeterminate',
                  'reconciled', 'committed', 'failed')
    ),
    version bigint NOT NULL CHECK (version >= 0),
    provider_request_id text UNIQUE CHECK (
        provider_request_id IS NULL OR length(btrim(provider_request_id)) > 0
    ),
    raw_response_object_id uuid REFERENCES storage.blob_objects (object_id),
    receipt_id uuid REFERENCES runtime.command_receipts (receipt_id)
        DEFERRABLE INITIALLY DEFERRED,
    artifact_set_id uuid REFERENCES runtime.artifact_sets (artifact_set_id)
        DEFERRABLE INITIALLY DEFERRED,
    failure_code text,
    failure_detail jsonb,
    reserved_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    dispatched_at timestamptz,
    responded_at timestamptz,
    completed_at timestamptz,
    UNIQUE (provider_id, provider_idempotency_key),
    CHECK (
        (state IN ('responded', 'reconciled', 'committed') AND raw_response_object_id IS NOT NULL)
        OR state IN ('reserved', 'dispatched', 'indeterminate', 'failed')
    ),
    CHECK (
        (state = 'committed' AND receipt_id IS NOT NULL AND artifact_set_id IS NOT NULL
                             AND completed_at IS NOT NULL)
        OR (state <> 'committed' AND receipt_id IS NULL AND artifact_set_id IS NULL)
    ),
    CHECK (
        (state = 'failed' AND failure_code IS NOT NULL AND failure_detail IS NOT NULL
                          AND completed_at IS NOT NULL)
        OR (state <> 'failed' AND failure_code IS NULL AND failure_detail IS NULL)
    )
);

CREATE OR REPLACE FUNCTION runtime.guard_generation_attempt_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'generation attempts are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'reserved' OR NEW.version <> 0
           OR NEW.provider_request_id IS NOT NULL OR NEW.raw_response_object_id IS NOT NULL
           OR NEW.receipt_id IS NOT NULL OR NEW.artifact_set_id IS NOT NULL THEN
            RAISE EXCEPTION 'generation attempts must begin as a clean reservation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.attempt_id, NEW.job_id, NEW.command_slot_id, NEW.request_hash,
        NEW.provider_id, NEW.provider_idempotency_key,
        NEW.request_payload_object_id, NEW.reserved_at)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.job_id, OLD.command_slot_id, OLD.request_hash,
        OLD.provider_id, OLD.provider_idempotency_key,
        OLD.request_payload_object_id, OLD.reserved_at) THEN
        RAISE EXCEPTION 'generation attempt identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'generation attempt transition requires exact version increment';
    END IF;
    IF OLD.provider_request_id IS NOT NULL
       AND NEW.provider_request_id IS DISTINCT FROM OLD.provider_request_id THEN
        RAISE EXCEPTION 'generation provider request identity is immutable once known';
    END IF;
    IF OLD.raw_response_object_id IS NOT NULL
       AND NEW.raw_response_object_id IS DISTINCT FROM OLD.raw_response_object_id THEN
        RAISE EXCEPTION 'generation raw-response identity is immutable once known';
    END IF;
    IF NOT (
        (OLD.state = 'reserved' AND NEW.state IN ('dispatched', 'failed'))
        OR (OLD.state = 'dispatched' AND NEW.state IN ('responded', 'indeterminate', 'failed'))
        OR (OLD.state = 'responded' AND NEW.state IN ('committed', 'failed'))
        OR (OLD.state = 'indeterminate' AND NEW.state IN ('reconciled', 'failed'))
        OR (OLD.state = 'reconciled' AND NEW.state IN ('committed', 'failed'))
    ) THEN
        RAISE EXCEPTION 'invalid generation attempt state transition';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_generation_attempt_transition_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.generation_attempts
FOR EACH ROW EXECUTE FUNCTION runtime.guard_generation_attempt_transition();

CREATE OR REPLACE FUNCTION runtime.assert_generation_attempt_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM runtime.command_slots AS slot
         WHERE slot.command_slot_id = NEW.command_slot_id
           AND slot.job_id = NEW.job_id
           AND slot.command_name = 'GenerateVlmEvidenceCommand'
           AND slot.request_hash = NEW.request_hash
    ) THEN
        RAISE EXCEPTION 'generation attempt must bind its exact generation command identity';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM storage.blob_claims
         WHERE job_id = NEW.job_id AND object_id = NEW.request_payload_object_id
    ) THEN
        RAISE EXCEPTION 'generation request payload must be an immutable blob claimed by its Job';
    END IF;
    IF NEW.raw_response_object_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM storage.blob_claims
         WHERE job_id = NEW.job_id AND object_id = NEW.raw_response_object_id
    ) THEN
        RAISE EXCEPTION 'generation raw response must be an immutable blob claimed by its Job';
    END IF;
    IF NEW.state = 'committed' AND NOT EXISTS (
        SELECT 1
          FROM runtime.command_receipts AS receipt
          JOIN runtime.artifact_sets AS artifact_set
            ON artifact_set.artifact_set_id = receipt.result_artifact_set_id
         WHERE receipt.receipt_id = NEW.receipt_id
           AND receipt.command_slot_id = NEW.command_slot_id
           AND receipt.outcome = 'succeeded'
           AND artifact_set.artifact_set_id = NEW.artifact_set_id
           AND artifact_set.command_slot_id = NEW.command_slot_id
           AND artifact_set.job_id = NEW.job_id
    ) THEN
        RAISE EXCEPTION 'committed generation must bind its exact command Receipt and ArtifactSet';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_generation_attempt_integrity_check
AFTER INSERT OR UPDATE ON runtime.generation_attempts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.assert_generation_attempt_integrity();

-- Only one slot may own run finalization for a Job, irrespective of idempotency key.
CREATE UNIQUE INDEX runtime_one_run_finalizer_per_job
    ON runtime.command_slots (job_id) WHERE command_name = 'FinalizeRunOutcome';

CREATE OR REPLACE FUNCTION runtime.reject_slot_insert_for_terminal_job()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE job_state text;
BEGIN
    SELECT state INTO job_state FROM runtime.jobs WHERE job_id = NEW.job_id FOR UPDATE;
    IF job_state IN ('succeeded', 'denied', 'failed') THEN
        RAISE EXCEPTION 'terminal Job cannot accept a fresh command slot';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_terminal_job_rejects_fresh_slot
BEFORE INSERT ON runtime.command_slots
FOR EACH ROW EXECUTE FUNCTION runtime.reject_slot_insert_for_terminal_job();

CREATE OR REPLACE FUNCTION runtime.assert_exact_run_finalization(checked_job uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    job_state text;
    finalizer_count integer;
    finalizer_outcome text;
    finalizer_set uuid;
    open_count integer;
    run_outcome_count integer;
    member_count integer;
BEGIN
    SELECT state INTO job_state FROM runtime.jobs WHERE job_id = checked_job;
    IF NOT FOUND OR job_state IN ('pending', 'running') THEN
        RETURN;
    END IF;
    SELECT count(*) INTO finalizer_count
      FROM runtime.command_slots AS slot
      JOIN runtime.command_receipts AS receipt ON receipt.command_slot_id = slot.command_slot_id
     WHERE slot.job_id = checked_job
       AND slot.command_name = 'FinalizeRunOutcome'
       AND slot.state = job_state
       AND receipt.outcome = job_state;
    IF finalizer_count <> 1 THEN
        RAISE EXCEPTION 'terminal Job requires exactly one matching FinalizeRunOutcome receipt';
    END IF;
    SELECT receipt.outcome, receipt.result_artifact_set_id
      INTO finalizer_outcome, finalizer_set
      FROM runtime.command_slots AS slot
      JOIN runtime.command_receipts AS receipt ON receipt.command_slot_id = slot.command_slot_id
     WHERE slot.job_id = checked_job
       AND slot.command_name = 'FinalizeRunOutcome'
       AND slot.state = job_state
       AND receipt.outcome = job_state;
    SELECT count(*) INTO open_count FROM runtime.command_slots
     WHERE job_id = checked_job AND state IN ('pending', 'running');
    IF open_count <> 0 THEN
        RAISE EXCEPTION 'run finalization is blocked by pending or running command slots';
    END IF;
    IF job_state = 'succeeded' THEN
        SELECT artifact_set.member_count,
               count(*) FILTER (WHERE artifact.artifact_type = 'run_outcome')
          INTO member_count, run_outcome_count
          FROM runtime.artifact_sets AS artifact_set
          JOIN runtime.artifact_set_members AS member
            ON member.artifact_set_id = artifact_set.artifact_set_id
          JOIN runtime.artifacts AS artifact ON artifact.artifact_id = member.artifact_id
         WHERE artifact_set.artifact_set_id = finalizer_set
         GROUP BY artifact_set.member_count;
        IF member_count IS DISTINCT FROM 1 OR run_outcome_count IS DISTINCT FROM 1 THEN
            RAISE EXCEPTION 'successful FinalizeRunOutcome requires exactly one run_outcome member';
        END IF;
    ELSIF finalizer_set IS NOT NULL THEN
        RAISE EXCEPTION 'failed or denied FinalizeRunOutcome cannot bind an ArtifactSet';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION runtime.check_job_finalization_from_job()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM runtime.assert_exact_run_finalization(NEW.job_id);
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_job_explicit_finalization_check
AFTER INSERT OR UPDATE ON runtime.jobs
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_job_finalization_from_job();

CREATE OR REPLACE FUNCTION runtime.check_job_finalization_from_slot()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM runtime.assert_exact_run_finalization(NEW.job_id);
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_slot_explicit_finalization_check
AFTER INSERT OR UPDATE ON runtime.command_slots
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_job_finalization_from_slot();

REVOKE ALL ON SCHEMA storage FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA storage FROM PUBLIC;
REVOKE ALL ON runtime.generation_attempts FROM PUBLIC;

COMMIT;
