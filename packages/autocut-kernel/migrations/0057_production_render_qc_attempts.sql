-- Durable lease/CAS recovery for full-file QC of one exact production render.
-- This private journal grants no local visibility or publication authority.

BEGIN;

CREATE TABLE runtime.production_render_qc_attempts (
    qc_attempt_id uuid PRIMARY KEY,
    render_attempt_id uuid NOT NULL UNIQUE
        REFERENCES runtime.production_render_attempts (attempt_id),
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    command_slot_id uuid NOT NULL UNIQUE
        REFERENCES runtime.command_slots (command_slot_id),
    rendered_version bigint NOT NULL CHECK (rendered_version >= 2),
    output_object_id uuid NOT NULL REFERENCES storage.blob_objects (object_id),
    render_facts_sha256 text NOT NULL CHECK (
        render_facts_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    qc_policy_sha256 text NOT NULL CHECK (
        qc_policy_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    required_check_set_version text NOT NULL CHECK (
        length(required_check_set_version) <= 128
        AND required_check_set_version ~ '^[a-z0-9][a-z0-9._-]*$'
    ),
    qc_runner_identity_sha256 text NOT NULL CHECK (
        qc_runner_identity_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),

    state text NOT NULL CHECK (state IN ('reserved', 'scanning')),
    version bigint NOT NULL CHECK (version >= 0),
    lease_token uuid,
    lease_expires_at timestamptz,
    reserved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (state = 'reserved' AND version = 0
            AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR
        (state = 'scanning' AND version >= 1
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

CREATE INDEX runtime_production_render_qc_attempt_recovery
    ON runtime.production_render_qc_attempts (state, lease_expires_at);

CREATE OR REPLACE FUNCTION runtime.guard_production_render_qc_attempt_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'production render QC attempts are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'reserved' OR NEW.version <> 0
           OR NEW.lease_token IS NOT NULL OR NEW.lease_expires_at IS NOT NULL THEN
            RAISE EXCEPTION
                'production render QC attempts must begin reserved at version zero';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.qc_attempt_id, NEW.render_attempt_id, NEW.job_id,
        NEW.command_slot_id, NEW.rendered_version, NEW.output_object_id,
        NEW.render_facts_sha256, NEW.qc_policy_sha256,
        NEW.required_check_set_version, NEW.qc_runner_identity_sha256,
        NEW.reserved_at)
       IS DISTINCT FROM
       (OLD.qc_attempt_id, OLD.render_attempt_id, OLD.job_id,
        OLD.command_slot_id, OLD.rendered_version, OLD.output_object_id,
        OLD.render_facts_sha256, OLD.qc_policy_sha256,
        OLD.required_check_set_version, OLD.qc_runner_identity_sha256,
        OLD.reserved_at) THEN
        RAISE EXCEPTION 'production render QC attempt identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION
            'production render QC attempt transition requires exact version increment';
    END IF;

    IF OLD.state = 'reserved' AND NEW.state = 'scanning' THEN
        IF NEW.lease_expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'production render QC lease must expire in the future';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'scanning' AND NEW.state = 'scanning' THEN
        IF NEW.lease_expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'production render QC lease renewal or takeover is invalid';
        END IF;
        IF NEW.lease_token = OLD.lease_token THEN
            IF OLD.lease_expires_at <= clock_timestamp()
               OR NEW.lease_expires_at <= OLD.lease_expires_at THEN
                RAISE EXCEPTION
                    'production render QC lease renewal requires an active lease and later expiry';
            END IF;
        ELSIF OLD.lease_expires_at > clock_timestamp() THEN
            RAISE EXCEPTION 'active production render QC lease cannot be taken over';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid production render QC attempt state transition';
END $$;

CREATE TRIGGER runtime_production_render_qc_attempt_transition_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.production_render_qc_attempts
FOR EACH ROW EXECUTE FUNCTION runtime.guard_production_render_qc_attempt_transition();

CREATE OR REPLACE FUNCTION runtime.assert_production_render_qc_attempt_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM runtime.production_render_attempts AS parent_render
          JOIN runtime.command_slots AS render_slot
            ON render_slot.command_slot_id = parent_render.command_slot_id
          JOIN runtime.jobs AS render_job
            ON render_job.job_id = parent_render.job_id
          JOIN storage.blob_objects AS output_blob
            ON output_blob.object_id = parent_render.output_object_id
          JOIN storage.blob_claims AS output_claim
            ON output_claim.object_id = output_blob.object_id
         WHERE parent_render.attempt_id = NEW.render_attempt_id
           AND parent_render.state = 'rendered'
           AND parent_render.version = NEW.rendered_version
           AND parent_render.output_object_id = NEW.output_object_id
           AND parent_render.render_facts_sha256 = NEW.render_facts_sha256
           AND parent_render.render_facts_json IS NOT NULL
           AND parent_render.job_id = NEW.job_id
           AND parent_render.command_slot_id = NEW.command_slot_id
           AND render_slot.job_id = NEW.job_id
           AND render_slot.state = 'running'
           AND render_slot.command_name = 'RenderProductionRecipeCommand@1'
           AND render_slot.execution_kind = 'deterministic'
           AND render_job.profile IN ('shadow', 'production')
           AND output_blob.object_id = NEW.output_object_id
           AND output_blob.storage_kind = 's3_compatible'
           AND output_blob.byte_length > 0
           AND output_blob.media_type = 'video/mp4'
           AND output_claim.job_id = NEW.job_id
    ) THEN
        RAISE EXCEPTION
            'production render QC attempt must bind one exact rendered output authority';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_production_render_qc_attempt_integrity_check
AFTER INSERT OR UPDATE ON runtime.production_render_qc_attempts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.assert_production_render_qc_attempt_integrity();

-- Once QC has started, only the later atomic QC/release transaction may
-- terminalize the parent. Layer 3a has no operation that deletes the journal.
CREATE OR REPLACE FUNCTION runtime.guard_production_render_attempt_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'production render attempts are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'reserved' OR NEW.version <> 0
           OR NEW.lease_token IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
           OR NEW.output_object_id IS NOT NULL OR NEW.rendered_at IS NOT NULL
           OR NEW.render_facts_json IS NOT NULL OR NEW.render_facts_sha256 IS NOT NULL
           OR NEW.receipt_id IS NOT NULL OR NEW.artifact_set_id IS NOT NULL
           OR NEW.failure_code IS NOT NULL OR NEW.failure_detail IS NOT NULL
           OR NEW.completed_at IS NOT NULL THEN
            RAISE EXCEPTION
                'production render attempts must begin reserved at version zero';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state IN ('committed', 'denied', 'failed') THEN
        RAISE EXCEPTION 'terminal production render attempts are immutable';
    END IF;
    IF (NEW.attempt_id, NEW.job_id, NEW.command_slot_id, NEW.request_hash,
        NEW.recipe_receipt_id, NEW.recipe_artifact_set_id,
        NEW.recipe_member_ordinal, NEW.recipe_namespace,
        NEW.recipe_scope_kind, NEW.recipe_scope_key, NEW.recipe_artifact_type,
        NEW.recipe_logical_id, NEW.recipe_revision, NEW.recipe_content_hash,
        NEW.render_plan_sha256, NEW.render_profile_sha256,
        NEW.renderer_identity_sha256, NEW.execution_limits_sha256,
        NEW.max_output_bytes, NEW.reserved_at)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.job_id, OLD.command_slot_id, OLD.request_hash,
        OLD.recipe_receipt_id, OLD.recipe_artifact_set_id,
        OLD.recipe_member_ordinal, OLD.recipe_namespace,
        OLD.recipe_scope_kind, OLD.recipe_scope_key, OLD.recipe_artifact_type,
        OLD.recipe_logical_id, OLD.recipe_revision, OLD.recipe_content_hash,
        OLD.render_plan_sha256, OLD.render_profile_sha256,
        OLD.renderer_identity_sha256, OLD.execution_limits_sha256,
        OLD.max_output_bytes, OLD.reserved_at) THEN
        RAISE EXCEPTION 'production render attempt identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION
            'production render attempt transition requires exact version increment';
    END IF;
    IF OLD.output_object_id IS NOT NULL
       AND (NEW.output_object_id, NEW.rendered_at,
            NEW.render_facts_json, NEW.render_facts_sha256)
           IS DISTINCT FROM
           (OLD.output_object_id, OLD.rendered_at,
            OLD.render_facts_json, OLD.render_facts_sha256) THEN
        RAISE EXCEPTION 'production render output facts are immutable once known';
    END IF;

    IF OLD.state = 'rendering' AND NEW.state = 'rendering' THEN
        IF (NEW.output_object_id, NEW.rendered_at,
            NEW.render_facts_json, NEW.render_facts_sha256,
            NEW.receipt_id, NEW.artifact_set_id, NEW.failure_code,
            NEW.failure_detail, NEW.completed_at)
           IS DISTINCT FROM
           (OLD.output_object_id, OLD.rendered_at,
            OLD.render_facts_json, OLD.render_facts_sha256,
            OLD.receipt_id, OLD.artifact_set_id, OLD.failure_code,
            OLD.failure_detail, OLD.completed_at)
           OR NEW.lease_expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'production render lease renewal or takeover is invalid';
        END IF;
        IF NEW.lease_token = OLD.lease_token THEN
            IF OLD.lease_expires_at <= clock_timestamp()
               OR NEW.lease_expires_at <= OLD.lease_expires_at THEN
                RAISE EXCEPTION
                    'production render lease renewal requires an active lease and later expiry';
            END IF;
        ELSIF OLD.lease_expires_at > clock_timestamp() THEN
            RAISE EXCEPTION 'active production render lease cannot be taken over';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.state = 'reserved' AND NEW.state = 'rendering' THEN
        IF NEW.lease_expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'production render lease must expire in the future';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'rendering' AND NEW.state = 'rendered' THEN
        IF OLD.lease_expires_at <= clock_timestamp()
           OR NEW.render_facts_json IS NULL
           OR NEW.render_facts_sha256 IS NULL THEN
            RAISE EXCEPTION 'expired or factless production render cannot resolve an attempt';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'rendering' AND NEW.state IN ('denied', 'failed') THEN
        IF OLD.lease_expires_at <= clock_timestamp()
           OR NEW.output_object_id IS NOT NULL THEN
            RAISE EXCEPTION
                'active pre-render rejection cannot claim production output bytes';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'reserved' AND NEW.state IN ('denied', 'failed') THEN
        IF NEW.output_object_id IS NOT NULL THEN
            RAISE EXCEPTION
                'reserved production render rejection cannot claim output bytes';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'rendered'
       AND NEW.state IN ('committed', 'denied', 'failed') THEN
        IF EXISTS (
            SELECT 1
              FROM runtime.production_render_qc_attempts AS qc_attempt
             WHERE qc_attempt.render_attempt_id = OLD.attempt_id
               AND qc_attempt.state IN ('reserved', 'scanning')
        ) THEN
            RAISE EXCEPTION
                'production render with an active QC journal cannot become terminal';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid production render attempt state transition';
END $$;

COMMENT ON TABLE runtime.production_render_qc_attempts IS
    'Durable private full-file QC fencing; not local visibility or publication authority.';

REVOKE ALL ON runtime.production_render_qc_attempts FROM PUBLIC;

COMMIT;
