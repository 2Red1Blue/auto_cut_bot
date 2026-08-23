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

COMMIT;
