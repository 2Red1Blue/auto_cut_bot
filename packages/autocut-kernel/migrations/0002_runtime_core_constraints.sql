-- Follow-up runtime invariants kept separate so a migration runner can apply
-- the MVP core incrementally on a fresh disposable PostgreSQL instance.

BEGIN;

CREATE OR REPLACE FUNCTION runtime.assert_head_matches_artifact()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE artifact runtime.artifacts%ROWTYPE;
BEGIN
    SELECT * INTO artifact FROM runtime.artifacts WHERE artifact_id = NEW.artifact_id;
    IF NOT FOUND OR (artifact.job_id, artifact.namespace, artifact.scope_kind, artifact.scope_key,
                     artifact.artifact_type, artifact.logical_id, artifact.revision)
        IS DISTINCT FROM (NEW.job_id, NEW.namespace, NEW.scope_kind, NEW.scope_key,
                          NEW.artifact_type, NEW.logical_id, NEW.revision) THEN
        RAISE EXCEPTION 'logical head must name its exact scoped artifact revision';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_logical_head_exact_target_check
AFTER INSERT OR UPDATE ON runtime.logical_heads
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_head_matches_artifact();

-- ------------------------------------------------------------------
-- Immutability: committed rows MUST NOT be mutated or deleted.
-- ------------------------------------------------------------------

CREATE OR REPLACE FUNCTION runtime.prevent_receipt_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'committed receipts are immutable';
END $$;

CREATE TRIGGER runtime_receipt_no_update
BEFORE UPDATE OR DELETE ON runtime.command_receipts
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_receipt_mutation();

CREATE OR REPLACE FUNCTION runtime.prevent_artifact_set_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'committed artifact sets are immutable';
END $$;

CREATE TRIGGER runtime_artifact_set_no_update
BEFORE UPDATE OR DELETE ON runtime.artifact_sets
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_artifact_set_mutation();

CREATE OR REPLACE FUNCTION runtime.prevent_artifact_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'committed artifacts are immutable';
END $$;

CREATE TRIGGER runtime_artifact_no_update
BEFORE UPDATE OR DELETE ON runtime.artifacts
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_artifact_mutation();

CREATE OR REPLACE FUNCTION runtime.prevent_artifact_set_member_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'committed artifact set members are immutable';
END $$;

CREATE TRIGGER runtime_artifact_set_member_no_update
BEFORE UPDATE OR DELETE ON runtime.artifact_set_members
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_artifact_set_member_mutation();

-- ------------------------------------------------------------------
-- Cross-table integrity: every artifact MUST belong to the same job
-- as its enclosing artifact set.
-- ------------------------------------------------------------------

CREATE OR REPLACE FUNCTION runtime.assert_artifact_job_matches_set()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM runtime.artifact_sets
         WHERE artifact_set_id = NEW.artifact_set_id AND job_id = NEW.job_id
    ) THEN
        RAISE EXCEPTION 'artifact job must match its artifact set job';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_artifact_job_matches_set_check
AFTER INSERT OR UPDATE ON runtime.artifacts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_artifact_job_matches_set();

-- ------------------------------------------------------------------
-- Command lifecycle: a receipt is the immutable proof of a terminal
-- command.  Check this at commit so the adapter can write the receipt and
-- terminal slot transition in either statement order within one transaction.
-- ------------------------------------------------------------------

CREATE OR REPLACE FUNCTION runtime.assert_command_slot_receipt_lifecycle()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    checked_slot uuid;
    slot_state text;
    receipt_count integer;
    receipt_outcome text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        checked_slot := OLD.command_slot_id;
    ELSE
        checked_slot := NEW.command_slot_id;
    END IF;
    SELECT state INTO slot_state
      FROM runtime.command_slots WHERE command_slot_id = checked_slot;
    -- A receipt DELETE may be paired with slot deletion by a future migration;
    -- this migration never permits deleting a terminal slot.
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT count(*), min(outcome) INTO receipt_count, receipt_outcome
      FROM runtime.command_receipts WHERE command_slot_id = checked_slot;
    IF slot_state IN ('succeeded', 'denied', 'failed') THEN
        IF receipt_count <> 1 OR receipt_outcome IS DISTINCT FROM slot_state THEN
            RAISE EXCEPTION 'terminal command slot must have exactly one matching receipt';
        END IF;
    ELSIF receipt_count <> 0 THEN
        RAISE EXCEPTION 'pending or running command slot must not have a receipt';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_command_slot_receipt_from_slot
AFTER INSERT OR UPDATE ON runtime.command_slots
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_command_slot_receipt_lifecycle();
CREATE CONSTRAINT TRIGGER runtime_command_slot_receipt_from_receipt
AFTER INSERT OR UPDATE OR DELETE ON runtime.command_receipts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_command_slot_receipt_lifecycle();

-- States only move forward.  A terminal Job or command slot is append-only;
-- its terminal outcome cannot be replaced by a later completion.
CREATE OR REPLACE FUNCTION runtime.prevent_job_state_rewrite()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state IN ('succeeded', 'denied', 'failed') THEN
        RAISE EXCEPTION 'terminal jobs are immutable';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state
       AND NOT ((OLD.state = 'pending' AND NEW.state IN ('running', 'succeeded', 'denied', 'failed'))
             OR (OLD.state = 'running' AND NEW.state IN ('succeeded', 'denied', 'failed'))) THEN
        RAISE EXCEPTION 'job state cannot move backwards or be rewritten';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_job_state_no_rewrite
BEFORE UPDATE ON runtime.jobs
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_job_state_rewrite();

CREATE OR REPLACE FUNCTION runtime.prevent_terminal_slot_rewrite()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.state IN ('succeeded', 'denied', 'failed') THEN
            RAISE EXCEPTION 'terminal command slots are immutable';
        END IF;
        RETURN OLD;
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

CREATE TRIGGER runtime_terminal_slot_no_rewrite
BEFORE UPDATE OR DELETE ON runtime.command_slots
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_terminal_slot_rewrite();

COMMIT;
