-- Durable execution classification for generic command slots.
--
-- Provider-attempt/receipt closure belongs to a slot's immutable execution
-- kind, not to a growing list of command names.  The historical VLM command
-- is the only supported pre-0018 generation command; every other historical
-- slot is deterministic.

BEGIN;

LOCK TABLE runtime.command_slots, runtime.generation_attempts,
           runtime.command_receipts, runtime.generation_receipt_attempts
    IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE runtime.command_slots
    ADD COLUMN execution_kind text;

-- 0002 intentionally freezes terminal slots.  This one migration supplies a
-- value for that new required column while holding the table lock, then
-- immediately restores the trigger before normal writes can resume.
ALTER TABLE runtime.command_slots
    DISABLE TRIGGER runtime_terminal_slot_no_rewrite;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM runtime.generation_attempts AS attempt
          LEFT JOIN runtime.command_slots AS slot
            ON slot.command_slot_id = attempt.command_slot_id
           AND slot.job_id = attempt.job_id
         WHERE slot.command_slot_id IS NULL
            OR slot.command_name <> 'GenerateVlmEvidenceCommand'
            OR slot.request_hash <> attempt.request_hash
    ) THEN
        RAISE EXCEPTION
            '0018 refuses historical generation attempts without matching generation slots';
    END IF;
END $$;

UPDATE runtime.command_slots
   SET execution_kind = CASE
       WHEN command_name = 'GenerateVlmEvidenceCommand' THEN 'generation'
       ELSE 'deterministic'
   END;

-- Updating historical slots queues the existing deferred lifecycle checks.
-- Drain them before this migration changes the table definition again.
SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE runtime.command_slots
    ENABLE TRIGGER runtime_terminal_slot_no_rewrite;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM runtime.generation_attempts AS attempt
          LEFT JOIN runtime.command_slots AS slot
            ON slot.command_slot_id = attempt.command_slot_id
           AND slot.job_id = attempt.job_id
         WHERE slot.command_slot_id IS NULL
            OR slot.execution_kind <> 'generation'
            OR slot.request_hash <> attempt.request_hash
    ) THEN
        RAISE EXCEPTION
            '0018 refuses historical generation attempts without matching generation slots';
    END IF;
END $$;

ALTER TABLE runtime.command_slots
    ALTER COLUMN execution_kind SET NOT NULL,
    ADD CONSTRAINT command_slots_execution_kind_check
        CHECK (execution_kind IN ('deterministic', 'generation'));

CREATE OR REPLACE FUNCTION runtime.prevent_terminal_slot_rewrite()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.state IN ('succeeded', 'denied', 'failed') THEN
            RAISE EXCEPTION 'terminal command slots are immutable';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.execution_kind IS DISTINCT FROM OLD.execution_kind THEN
        RAISE EXCEPTION 'command slot execution kind is immutable';
    END IF;
    IF OLD.state IN ('succeeded', 'denied', 'failed') THEN
        RAISE EXCEPTION 'terminal command slots are immutable';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state
       AND NOT ((OLD.state = 'pending' AND NEW.state IN ('running', 'succeeded', 'denied', 'failed'))
             OR (OLD.state = 'running' AND NEW.state IN ('succeeded', 'denied', 'failed'))) THEN
        RAISE EXCEPTION 'command slot state cannot move backwards or be rewritten';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION runtime.assert_generation_attempt_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE predecessor runtime.generation_attempts%ROWTYPE;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM runtime.command_slots AS slot
         WHERE slot.command_slot_id = NEW.command_slot_id
           AND slot.job_id = NEW.job_id
           AND slot.execution_kind = 'generation'
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
DECLARE checked_execution_kind text;
DECLARE attempt_count integer;
DECLARE relation_count integer;
DECLARE maximum_ordinal integer;
DECLARE terminal_attempt runtime.generation_attempts%ROWTYPE;
BEGIN
    SELECT receipt.command_slot_id, receipt.outcome, slot.execution_kind
      INTO checked_slot, checked_outcome, checked_execution_kind
      FROM runtime.command_receipts AS receipt
      JOIN runtime.command_slots AS slot
        ON slot.command_slot_id = receipt.command_slot_id
     WHERE receipt.receipt_id = checked_receipt;
    IF NOT FOUND OR checked_execution_kind <> 'generation' THEN
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

COMMIT;
