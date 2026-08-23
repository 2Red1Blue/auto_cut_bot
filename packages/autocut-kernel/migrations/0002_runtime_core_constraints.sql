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

COMMIT;
