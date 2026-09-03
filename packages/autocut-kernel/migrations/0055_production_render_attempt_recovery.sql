-- Durable lease/CAS recovery for one deterministic production render.
-- This relation binds private rendered bytes but grants no visibility or publication.

BEGIN;

CREATE TABLE runtime.production_render_attempts (
    attempt_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    command_slot_id uuid NOT NULL UNIQUE
        REFERENCES runtime.command_slots (command_slot_id),
    request_hash text NOT NULL CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),

    recipe_receipt_id uuid NOT NULL
        REFERENCES runtime.command_receipts (receipt_id) DEFERRABLE INITIALLY DEFERRED,
    recipe_artifact_set_id uuid NOT NULL
        REFERENCES runtime.artifact_sets (artifact_set_id) DEFERRABLE INITIALLY DEFERRED,
    recipe_member_ordinal integer NOT NULL CHECK (recipe_member_ordinal >= 0),
    recipe_namespace text NOT NULL CHECK (recipe_namespace = 'pipeline'),
    recipe_scope_kind text NOT NULL CHECK (recipe_scope_kind = 'job'),
    recipe_scope_key text NOT NULL CHECK (length(btrim(recipe_scope_key)) > 0),
    recipe_artifact_type text NOT NULL CHECK (recipe_artifact_type = 'recipe'),
    recipe_logical_id text NOT NULL CHECK (
        recipe_logical_id LIKE 'production\_recipe@%' ESCAPE '\'
        AND length(recipe_logical_id) > length('production_recipe@')
    ),
    recipe_revision bigint NOT NULL CHECK (recipe_revision >= 1),
    recipe_content_hash text NOT NULL CHECK (
        recipe_content_hash ~ '^sha256:[0-9a-f]{64}$'
    ),

    render_plan_sha256 text NOT NULL CHECK (
        render_plan_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    render_profile_sha256 text NOT NULL CHECK (
        render_profile_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    renderer_identity_sha256 text NOT NULL CHECK (
        renderer_identity_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    max_output_bytes bigint NOT NULL CHECK (max_output_bytes > 0),

    state text NOT NULL CHECK (
        state IN ('reserved', 'rendering', 'rendered', 'committed', 'denied', 'failed')
    ),
    version bigint NOT NULL CHECK (version >= 0),
    lease_token uuid,
    lease_expires_at timestamptz,
    output_object_id uuid REFERENCES storage.blob_objects (object_id),
    receipt_id uuid REFERENCES runtime.command_receipts (receipt_id)
        DEFERRABLE INITIALLY DEFERRED,
    artifact_set_id uuid REFERENCES runtime.artifact_sets (artifact_set_id)
        DEFERRABLE INITIALLY DEFERRED,
    failure_code text CHECK (
        failure_code IS NULL OR length(btrim(failure_code)) > 0
    ),
    failure_detail jsonb CHECK (
        failure_detail IS NULL OR jsonb_typeof(failure_detail) = 'object'
    ),
    reserved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    rendered_at timestamptz,
    completed_at timestamptz,
    CHECK (rendered_at IS NULL OR rendered_at >= reserved_at),
    CHECK (completed_at IS NULL OR completed_at >= reserved_at),

    CHECK (
        (state = 'reserved' AND version = 0
            AND lease_token IS NULL AND lease_expires_at IS NULL
            AND output_object_id IS NULL AND rendered_at IS NULL
            AND receipt_id IS NULL AND artifact_set_id IS NULL
            AND failure_code IS NULL AND failure_detail IS NULL
            AND completed_at IS NULL)
        OR
        (state = 'rendering' AND version >= 1
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
            AND output_object_id IS NULL AND rendered_at IS NULL
            AND receipt_id IS NULL AND artifact_set_id IS NULL
            AND failure_code IS NULL AND failure_detail IS NULL
            AND completed_at IS NULL)
        OR
        (state = 'rendered' AND version >= 2
            AND lease_token IS NULL AND lease_expires_at IS NULL
            AND output_object_id IS NOT NULL AND rendered_at IS NOT NULL
            AND receipt_id IS NULL AND artifact_set_id IS NULL
            AND failure_code IS NULL AND failure_detail IS NULL
            AND completed_at IS NULL)
        OR
        (state = 'committed' AND version >= 3
            AND lease_token IS NULL AND lease_expires_at IS NULL
            AND output_object_id IS NOT NULL AND rendered_at IS NOT NULL
            AND receipt_id IS NOT NULL AND artifact_set_id IS NOT NULL
            AND failure_code IS NULL AND failure_detail IS NULL
            AND completed_at IS NOT NULL)
        OR
        (state IN ('denied', 'failed') AND version >= 1
            AND lease_token IS NULL AND lease_expires_at IS NULL
            AND ((output_object_id IS NULL AND rendered_at IS NULL)
                OR (output_object_id IS NOT NULL AND rendered_at IS NOT NULL))
            AND receipt_id IS NOT NULL AND artifact_set_id IS NULL
            AND failure_code IS NOT NULL AND failure_detail IS NOT NULL
            AND completed_at IS NOT NULL)
    )
);

CREATE INDEX runtime_production_render_attempt_recovery
    ON runtime.production_render_attempts (state, lease_expires_at);

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
        NEW.renderer_identity_sha256, NEW.max_output_bytes, NEW.reserved_at)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.job_id, OLD.command_slot_id, OLD.request_hash,
        OLD.recipe_receipt_id, OLD.recipe_artifact_set_id,
        OLD.recipe_member_ordinal, OLD.recipe_namespace,
        OLD.recipe_scope_kind, OLD.recipe_scope_key, OLD.recipe_artifact_type,
        OLD.recipe_logical_id, OLD.recipe_revision, OLD.recipe_content_hash,
        OLD.render_plan_sha256, OLD.render_profile_sha256,
        OLD.renderer_identity_sha256, OLD.max_output_bytes, OLD.reserved_at) THEN
        RAISE EXCEPTION 'production render attempt identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION
            'production render attempt transition requires exact version increment';
    END IF;
    IF OLD.output_object_id IS NOT NULL
       AND (NEW.output_object_id, NEW.rendered_at)
           IS DISTINCT FROM (OLD.output_object_id, OLD.rendered_at) THEN
        RAISE EXCEPTION 'production render output identity is immutable once known';
    END IF;

    IF OLD.state = 'rendering' AND NEW.state = 'rendering' THEN
        IF (NEW.output_object_id, NEW.rendered_at, NEW.receipt_id,
            NEW.artifact_set_id, NEW.failure_code, NEW.failure_detail,
            NEW.completed_at)
           IS DISTINCT FROM
           (OLD.output_object_id, OLD.rendered_at, OLD.receipt_id,
            OLD.artifact_set_id, OLD.failure_code, OLD.failure_detail,
            OLD.completed_at)
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
        IF OLD.lease_expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'expired production render lease cannot resolve an attempt';
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
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid production render attempt state transition';
END $$;

CREATE TRIGGER runtime_production_render_attempt_transition_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.production_render_attempts
FOR EACH ROW EXECUTE FUNCTION runtime.guard_production_render_attempt_transition();

CREATE OR REPLACE FUNCTION runtime.assert_production_render_attempt_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM runtime.command_slots AS render_slot
          JOIN runtime.jobs AS render_job ON render_job.job_id = render_slot.job_id
         WHERE render_slot.command_slot_id = NEW.command_slot_id
           AND render_slot.job_id = NEW.job_id
           AND render_job.profile IN ('shadow', 'production')
           AND render_slot.command_name = 'RenderProductionRecipeCommand@1'
           AND render_slot.execution_kind = 'deterministic'
           AND render_slot.request_hash = NEW.request_hash
           AND render_slot.state = CASE
               WHEN NEW.state IN ('reserved', 'rendering', 'rendered') THEN 'running'
               WHEN NEW.state = 'committed' THEN 'succeeded'
               ELSE NEW.state
           END
    ) THEN
        RAISE EXCEPTION
            'production render attempt must bind its exact deterministic command slot';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM runtime.command_receipts AS recipe_receipt
          JOIN runtime.command_slots AS recipe_slot
            ON recipe_slot.command_slot_id = recipe_receipt.command_slot_id
          JOIN runtime.artifact_sets AS recipe_set
            ON recipe_set.artifact_set_id = recipe_receipt.result_artifact_set_id
          JOIN runtime.jobs AS recipe_job ON recipe_job.job_id = recipe_set.job_id
          JOIN runtime.artifact_set_members AS recipe_member
            ON recipe_member.artifact_set_id = recipe_set.artifact_set_id
          JOIN runtime.artifacts AS recipe_artifact
            ON recipe_artifact.artifact_set_id = recipe_member.artifact_set_id
           AND recipe_artifact.artifact_id = recipe_member.artifact_id
         WHERE recipe_receipt.receipt_id = NEW.recipe_receipt_id
           AND recipe_receipt.outcome = 'succeeded'
           AND recipe_receipt.result_artifact_set_id = NEW.recipe_artifact_set_id
           AND recipe_slot.command_name = 'CompileProductionRecipeCommand@1'
           AND recipe_slot.execution_kind = 'deterministic'
           AND recipe_slot.state = 'succeeded'
           AND recipe_slot.job_id = NEW.job_id
           AND recipe_set.artifact_set_id = NEW.recipe_artifact_set_id
           AND recipe_set.command_slot_id = recipe_slot.command_slot_id
           AND recipe_set.job_id = NEW.job_id
           AND recipe_set.member_count >= 3
           AND recipe_job.profile IN ('shadow', 'production')
           AND recipe_member.ordinal = NEW.recipe_member_ordinal
           AND recipe_member.ordinal > 0
           AND recipe_member.ordinal < recipe_set.member_count - 1
           AND recipe_artifact.namespace = NEW.recipe_namespace
           AND recipe_artifact.scope_kind = NEW.recipe_scope_kind
           AND recipe_artifact.scope_key = NEW.recipe_scope_key
           AND recipe_artifact.artifact_type = NEW.recipe_artifact_type
           AND recipe_artifact.logical_id = NEW.recipe_logical_id
           AND recipe_artifact.revision = NEW.recipe_revision
           AND recipe_artifact.content_hash = NEW.recipe_content_hash
           AND recipe_artifact.scope_key = recipe_job.job_key
           AND EXISTS (
               SELECT 1
                 FROM runtime.artifact_set_members AS report_member
                 JOIN runtime.artifacts AS report_artifact
                   ON report_artifact.artifact_set_id = report_member.artifact_set_id
                  AND report_artifact.artifact_id = report_member.artifact_id
                WHERE report_member.artifact_set_id = recipe_set.artifact_set_id
                  AND report_member.ordinal = 0
                  AND report_artifact.artifact_type = 'physical_edit_compilation_report'
                  AND report_artifact.logical_id = 'physical_edit_compilation_report'
                  AND report_artifact.namespace = NEW.recipe_namespace
                  AND report_artifact.scope_kind = NEW.recipe_scope_kind
                  AND report_artifact.scope_key = NEW.recipe_scope_key
                  AND report_artifact.revision = NEW.recipe_revision
           )
           AND EXISTS (
               SELECT 1
                 FROM runtime.artifact_set_members AS admission_member
                 JOIN runtime.artifacts AS admission_artifact
                   ON admission_artifact.artifact_set_id = admission_member.artifact_set_id
                  AND admission_artifact.artifact_id = admission_member.artifact_id
                WHERE admission_member.artifact_set_id = recipe_set.artifact_set_id
                  AND admission_member.ordinal = recipe_set.member_count - 1
                  AND admission_artifact.artifact_type = 'physical_edit_admission'
                  AND admission_artifact.logical_id = 'physical_edit_admission'
                  AND admission_artifact.namespace = NEW.recipe_namespace
                  AND admission_artifact.scope_kind = NEW.recipe_scope_kind
                  AND admission_artifact.scope_key = NEW.recipe_scope_key
                  AND admission_artifact.revision = NEW.recipe_revision
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM runtime.artifact_set_members AS other_recipe_member
                 JOIN runtime.artifacts AS other_recipe
                   ON other_recipe.artifact_set_id = other_recipe_member.artifact_set_id
                  AND other_recipe.artifact_id = other_recipe_member.artifact_id
                WHERE other_recipe_member.artifact_set_id = recipe_set.artifact_set_id
                  AND other_recipe_member.ordinal > 0
                  AND other_recipe_member.ordinal < recipe_set.member_count - 1
                  AND (
                      other_recipe.artifact_type <> 'recipe'
                      OR other_recipe.logical_id NOT LIKE 'production\_recipe@%' ESCAPE '\'
                      OR length(other_recipe.logical_id) <= length('production_recipe@')
                      OR other_recipe.namespace <> 'pipeline'
                      OR other_recipe.scope_kind <> 'job'
                      OR other_recipe.scope_key <> recipe_job.job_key
                      OR other_recipe.revision <> NEW.recipe_revision
                  )
           )
    ) THEN
        RAISE EXCEPTION
            'production render attempt recipe is not an exact admitted Stage 4 member';
    END IF;
    IF NEW.output_object_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
          FROM storage.blob_objects AS output_blob
          JOIN storage.blob_claims AS output_claim
            ON output_claim.object_id = output_blob.object_id
         WHERE output_blob.object_id = NEW.output_object_id
           AND output_blob.storage_kind = 's3_compatible'
           AND output_blob.byte_length > 0
           AND output_blob.byte_length <= NEW.max_output_bytes
           AND output_blob.media_type = 'video/mp4'
           AND output_claim.job_id = NEW.job_id
    ) THEN
        RAISE EXCEPTION
            'production render output must be an exact external Blob claimed by its Job';
    END IF;
    IF NEW.state = 'committed' AND NOT EXISTS (
        SELECT 1
          FROM runtime.command_receipts AS terminal_receipt
          JOIN runtime.artifact_sets AS terminal_set
            ON terminal_set.artifact_set_id = terminal_receipt.result_artifact_set_id
         WHERE terminal_receipt.receipt_id = NEW.receipt_id
           AND terminal_receipt.command_slot_id = NEW.command_slot_id
           AND terminal_receipt.outcome = 'succeeded'
           AND terminal_receipt.result_artifact_set_id = NEW.artifact_set_id
           AND terminal_set.artifact_set_id = NEW.artifact_set_id
           AND terminal_set.command_slot_id = NEW.command_slot_id
           AND terminal_set.job_id = NEW.job_id
    ) THEN
        RAISE EXCEPTION
            'committed production render must bind its exact Receipt and ArtifactSet';
    END IF;
    IF NEW.state IN ('denied', 'failed') AND NOT EXISTS (
        SELECT 1
          FROM runtime.command_receipts AS terminal_receipt
         WHERE terminal_receipt.receipt_id = NEW.receipt_id
           AND terminal_receipt.command_slot_id = NEW.command_slot_id
           AND terminal_receipt.outcome = NEW.state
           AND terminal_receipt.result_artifact_set_id IS NULL
           AND terminal_receipt.failure_code = NEW.failure_code
           AND terminal_receipt.failure_detail = NEW.failure_detail
    ) THEN
        RAISE EXCEPTION
            'rejected production render must bind its exact terminal Receipt';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_production_render_attempt_integrity_check
AFTER INSERT OR UPDATE ON runtime.production_render_attempts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.assert_production_render_attempt_integrity();

COMMENT ON TABLE runtime.production_render_attempts IS
    'Durable private render fencing and recovery; not local visibility or publication authority.';

REVOKE ALL ON runtime.production_render_attempts FROM PUBLIC;

COMMIT;
