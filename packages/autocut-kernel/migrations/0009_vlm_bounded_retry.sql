-- Upgrade one VLM command from a single invocation to a bounded durable chain.

BEGIN;

ALTER TABLE runtime.generation_attempts
    DISABLE TRIGGER runtime_generation_attempt_transition_guard;

ALTER TABLE runtime.generation_attempts
    ADD COLUMN attempt_ordinal integer,
    ADD COLUMN previous_attempt_id uuid,
    ADD COLUMN retry_policy_hash text,
    ADD COLUMN max_attempts integer,
    ADD COLUMN failure_disposition text,
    ADD COLUMN dispatch_lease_token text,
    ADD COLUMN dispatch_lease_expires_at timestamptz,
    ADD COLUMN not_before_at timestamptz;

DO $$
DECLARE command_slot_unique record;
BEGIN
    FOR command_slot_unique IN
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'runtime.generation_attempts'::regclass
           AND contype = 'u'
           AND pg_get_constraintdef(oid) = 'UNIQUE (command_slot_id)'
    LOOP
        EXECUTE format(
            'ALTER TABLE runtime.generation_attempts DROP CONSTRAINT %I',
            command_slot_unique.conname
        );
    END LOOP;
END $$;

-- Drop the obsolete one-Attempt-per-slot constraint before touching existing
-- rows.  PostgreSQL will reject that DDL after an UPDATE has queued deferred
-- integrity-trigger events in the same transaction.
UPDATE runtime.generation_attempts
   SET attempt_ordinal = 1,
       retry_policy_hash =
           'sha256:70f279a4b886d1aaf1498b432af937495e431113db3f38728a635ed24a6fbe39',
       max_attempts = 1,
       not_before_at = reserved_at,
       failure_disposition = CASE WHEN state = 'failed' THEN 'nonretryable' END,
       dispatch_lease_token = CASE
           WHEN state = 'dispatched' THEN 'migration-expired-' || attempt_id::text
       END,
       dispatch_lease_expires_at = CASE
           WHEN state = 'dispatched' THEN transaction_timestamp()
       END;

-- Flush deferred row-integrity checks before the following ALTER TABLE.  A
-- populated legacy database otherwise retains pending trigger events and
-- PostgreSQL correctly refuses the DDL even though the migration is atomic.
SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE runtime.generation_attempts
    ALTER COLUMN attempt_ordinal SET NOT NULL,
    ALTER COLUMN retry_policy_hash SET NOT NULL,
    ALTER COLUMN max_attempts SET NOT NULL,
    ALTER COLUMN not_before_at SET NOT NULL,
    ADD CONSTRAINT generation_attempt_previous_fk
        FOREIGN KEY (previous_attempt_id)
        REFERENCES runtime.generation_attempts (attempt_id)
        DEFERRABLE INITIALLY DEFERRED,
    ADD CONSTRAINT generation_attempt_slot_ordinal_unique
        UNIQUE (command_slot_id, attempt_ordinal),
    ADD CONSTRAINT generation_attempt_previous_unique UNIQUE (previous_attempt_id),
    ADD CONSTRAINT generation_attempt_ordinal_positive CHECK (attempt_ordinal >= 1),
    ADD CONSTRAINT generation_attempt_budget_bounded CHECK (max_attempts BETWEEN 1 AND 3),
    ADD CONSTRAINT generation_attempt_budget_covers_ordinal
        CHECK (max_attempts >= attempt_ordinal),
    ADD CONSTRAINT generation_attempt_retry_policy_sha256
        CHECK (retry_policy_hash ~ '^sha256:[0-9a-f]{64}$'),
    ADD CONSTRAINT generation_attempt_predecessor_shape CHECK (
        (attempt_ordinal = 1 AND previous_attempt_id IS NULL)
        OR (attempt_ordinal > 1 AND previous_attempt_id IS NOT NULL)
    ),
    ADD CONSTRAINT generation_attempt_failure_disposition_closed CHECK (
        (state = 'failed' AND failure_disposition IN
            ('retryable', 'nonretryable', 'repairable'))
        OR (state <> 'failed' AND failure_disposition IS NULL)
    ),
    ADD CONSTRAINT generation_attempt_dispatch_lease_shape CHECK (
        (dispatch_lease_token IS NULL AND dispatch_lease_expires_at IS NULL
            AND state <> 'dispatched')
        OR (dispatch_lease_token IS NOT NULL
            AND length(btrim(dispatch_lease_token)) > 0
            AND dispatch_lease_expires_at IS NOT NULL
            AND state IN ('dispatched', 'indeterminate'))
        OR (dispatch_lease_token IS NULL AND dispatch_lease_expires_at IS NULL
            AND state = 'indeterminate')
    );

CREATE TABLE runtime.generation_receipt_attempts (
    receipt_id uuid NOT NULL REFERENCES runtime.command_receipts (receipt_id)
        DEFERRABLE INITIALLY DEFERRED,
    attempt_id uuid NOT NULL REFERENCES runtime.generation_attempts (attempt_id)
        DEFERRABLE INITIALLY DEFERRED,
    attempt_ordinal integer NOT NULL CHECK (attempt_ordinal >= 1),
    PRIMARY KEY (receipt_id, attempt_ordinal),
    UNIQUE (attempt_id)
);

INSERT INTO runtime.generation_receipt_attempts
    (receipt_id, attempt_id, attempt_ordinal)
SELECT receipt.receipt_id, attempt.attempt_id, 1
  FROM runtime.generation_attempts AS attempt
  JOIN runtime.command_receipts AS receipt
    ON receipt.command_slot_id = attempt.command_slot_id;

CREATE OR REPLACE FUNCTION runtime.guard_generation_attempt_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'generation attempts are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'reserved' OR NEW.version <> 0
           OR NEW.provider_request_id IS NOT NULL OR NEW.raw_response_object_id IS NOT NULL
           OR NEW.receipt_id IS NOT NULL OR NEW.artifact_set_id IS NOT NULL
           OR NEW.failure_code IS NOT NULL OR NEW.failure_detail IS NOT NULL
           OR NEW.failure_disposition IS NOT NULL
           OR NEW.dispatch_lease_token IS NOT NULL
           OR NEW.dispatch_lease_expires_at IS NOT NULL THEN
            RAISE EXCEPTION 'generation attempts must begin as a clean reservation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.attempt_id, NEW.job_id, NEW.command_slot_id, NEW.request_hash,
        NEW.provider_id, NEW.provider_idempotency_key,
        NEW.request_payload_object_id, NEW.attempt_ordinal,
        NEW.previous_attempt_id, NEW.retry_policy_hash, NEW.max_attempts,
        NEW.not_before_at, NEW.reserved_at)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.job_id, OLD.command_slot_id, OLD.request_hash,
        OLD.provider_id, OLD.provider_idempotency_key,
        OLD.request_payload_object_id, OLD.attempt_ordinal,
        OLD.previous_attempt_id, OLD.retry_policy_hash, OLD.max_attempts,
        OLD.not_before_at, OLD.reserved_at) THEN
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
    IF OLD.state = NEW.state AND OLD.state IN ('dispatched', 'indeterminate') THEN
        IF (NEW.raw_response_object_id, NEW.receipt_id, NEW.artifact_set_id,
            NEW.failure_code, NEW.failure_detail, NEW.failure_disposition,
            NEW.dispatched_at, NEW.responded_at, NEW.completed_at)
           IS DISTINCT FROM
           (OLD.raw_response_object_id, OLD.receipt_id, OLD.artifact_set_id,
            OLD.failure_code, OLD.failure_detail, OLD.failure_disposition,
            OLD.dispatched_at, OLD.responded_at, OLD.completed_at)
           OR (OLD.provider_request_id IS NOT NULL
               AND NEW.provider_request_id IS DISTINCT FROM OLD.provider_request_id) THEN
            RAISE EXCEPTION 'active generation recovery may only bind request identity or rotate lease';
        END IF;
        RETURN NEW;
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

ALTER TABLE runtime.generation_attempts
    ENABLE TRIGGER runtime_generation_attempt_transition_guard;

CREATE OR REPLACE FUNCTION runtime.assert_generation_attempt_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE predecessor runtime.generation_attempts%ROWTYPE;
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
    IF NEW.attempt_ordinal > 1 THEN
        SELECT * INTO predecessor
          FROM runtime.generation_attempts
         WHERE attempt_id = NEW.previous_attempt_id;
        IF NOT FOUND
           OR predecessor.command_slot_id <> NEW.command_slot_id
           OR predecessor.job_id <> NEW.job_id
           OR predecessor.attempt_ordinal <> NEW.attempt_ordinal - 1
           OR predecessor.state <> 'failed'
           OR predecessor.failure_disposition <> 'retryable'
           OR predecessor.request_hash <> NEW.request_hash
           OR predecessor.provider_id <> NEW.provider_id
           OR predecessor.request_payload_object_id <> NEW.request_payload_object_id
           OR predecessor.retry_policy_hash <> NEW.retry_policy_hash
           OR predecessor.max_attempts <> NEW.max_attempts THEN
            RAISE EXCEPTION 'generation retry predecessor must be the exact retryable prior attempt';
        END IF;
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

CREATE OR REPLACE FUNCTION runtime.assert_generation_receipt_chain(checked_receipt uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE checked_slot uuid;
DECLARE checked_outcome text;
DECLARE checked_command text;
DECLARE attempt_count integer;
DECLARE relation_count integer;
DECLARE maximum_ordinal integer;
DECLARE terminal_attempt runtime.generation_attempts%ROWTYPE;
BEGIN
    SELECT receipt.command_slot_id, receipt.outcome, slot.command_name
      INTO checked_slot, checked_outcome, checked_command
      FROM runtime.command_receipts AS receipt
      JOIN runtime.command_slots AS slot
        ON slot.command_slot_id = receipt.command_slot_id
     WHERE receipt.receipt_id = checked_receipt;
    IF NOT FOUND OR checked_command <> 'GenerateVlmEvidenceCommand' THEN
        RETURN;
    END IF;
    SELECT count(*), max(attempt_ordinal)
      INTO attempt_count, maximum_ordinal
      FROM runtime.generation_attempts
     WHERE command_slot_id = checked_slot;
    SELECT count(*) INTO relation_count
      FROM runtime.generation_receipt_attempts AS relation
     WHERE relation.receipt_id = checked_receipt;
    IF attempt_count = 0 OR relation_count <> attempt_count
       OR maximum_ordinal <> attempt_count
       OR EXISTS (
           SELECT 1
             FROM runtime.generation_receipt_attempts AS relation
             LEFT JOIN runtime.generation_attempts AS attempt
               ON attempt.attempt_id = relation.attempt_id
              AND attempt.attempt_ordinal = relation.attempt_ordinal
              AND attempt.command_slot_id = checked_slot
            WHERE relation.receipt_id = checked_receipt
              AND attempt.attempt_id IS NULL
       ) THEN
        RAISE EXCEPTION 'generation Receipt must bind the complete contiguous Attempt chain';
    END IF;
    SELECT * INTO terminal_attempt
      FROM runtime.generation_attempts
     WHERE command_slot_id = checked_slot AND attempt_ordinal = maximum_ordinal;
    IF checked_outcome = 'succeeded' THEN
        IF terminal_attempt.state <> 'committed'
           OR terminal_attempt.receipt_id <> checked_receipt THEN
            RAISE EXCEPTION 'successful generation Receipt must bind its committed final Attempt';
        END IF;
    ELSIF terminal_attempt.state <> 'failed'
       OR (terminal_attempt.failure_disposition = 'retryable'
           AND terminal_attempt.attempt_ordinal < terminal_attempt.max_attempts) THEN
        RAISE EXCEPTION 'rejected generation Receipt requires a terminal failed Attempt';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION runtime.check_generation_receipt_chain()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM runtime.assert_generation_receipt_chain(NEW.receipt_id);
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_generation_receipt_chain_from_receipt
AFTER INSERT OR UPDATE ON runtime.command_receipts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_generation_receipt_chain();

CREATE CONSTRAINT TRIGGER runtime_generation_receipt_chain_from_relation
AFTER INSERT OR UPDATE ON runtime.generation_receipt_attempts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_generation_receipt_chain();

CREATE OR REPLACE FUNCTION runtime.prevent_generation_receipt_attempt_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'generation Receipt Attempt bindings are immutable';
END $$;

CREATE TRIGGER runtime_generation_receipt_attempt_no_mutation
BEFORE UPDATE OR DELETE ON runtime.generation_receipt_attempts
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_generation_receipt_attempt_mutation();

REVOKE ALL ON runtime.generation_receipt_attempts FROM PUBLIC;

COMMIT;
