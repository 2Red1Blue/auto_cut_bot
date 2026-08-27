-- Immutable v2 runtime-calibration capabilities.
--
-- The v1 calibration record anchor remains historical evidence. A normal
-- runtime needs this additional row and a distinct v2 accepted-record scope,
-- bound to a self-measured PC-CUDA or Mac-CPU timing compatibility identity.

BEGIN;

-- v1 anchors remain in their historical ``shadow_calibration@N`` scopes.
-- A v2 accepted closure has its own per-environment scope, so two machines
-- can calibrate independently against the same static profile version.
ALTER TABLE runtime.calibration_record_anchors
    DROP CONSTRAINT calibration_record_anchors_scope_key_check;
ALTER TABLE runtime.calibration_record_anchors
    ADD CONSTRAINT calibration_record_anchors_scope_key_check CHECK (
        scope_key ~ '^(shadow_calibration|runtime_calibration@(pc_cuda|mac_cpu))@[1-9][0-9]*$'
    );

CREATE OR REPLACE FUNCTION runtime.is_calibration_record_scope(
    checked_namespace text,
    checked_scope_kind text,
    checked_scope_key text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT checked_namespace = 'autocut_authority'
       AND checked_scope_kind = 'calibration'
       AND checked_scope_key ~ '^(shadow_calibration|runtime_calibration@(pc_cuda|mac_cpu))@[1-9][0-9]*$'
$$;

-- 0017 protects validator Jobs and their finalization with the original
-- shadow-only scope grammar.  Rebind those existing guards before a v2 writer
-- can exist; otherwise a valid PC/Mac validator Job would be rejected before
-- it could produce its immutable anchor.
CREATE OR REPLACE FUNCTION runtime.guard_calibration_validator_job_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.job_key LIKE 'autocut_calibration_validator:%' THEN
        IF NEW.job_key !~ '^autocut_calibration_validator:(shadow_calibration|runtime_calibration@(pc_cuda|mac_cpu))@[1-9][0-9]*$'
           OR NEW.profile <> 'authority' THEN
            RAISE EXCEPTION 'calibration validator Job identity must be the exact authority profile key';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.job_key LIKE 'autocut_calibration_validator:%'
       AND (NEW.job_key, NEW.profile) IS DISTINCT FROM (OLD.job_key, OLD.profile) THEN
        RAISE EXCEPTION 'calibration validator Job key and profile are immutable';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION runtime.assert_exact_calibration_validator_finalization(checked_job uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    job_state text;
    job_key text;
    job_profile text;
    open_count integer;
    matching_receipt_count integer;
    finalizer_slot uuid;
    finalizer_set uuid;
    job_set_count integer;
    anchor_count integer;
    member_count integer;
    aggregate_count integer;
    member_record_count integer;
    validation_count integer;
BEGIN
    SELECT job.state, job.job_key, job.profile INTO job_state, job_key, job_profile
      FROM runtime.jobs AS job WHERE job.job_id = checked_job;
    IF NOT FOUND OR job_key !~ '^autocut_calibration_validator:(shadow_calibration|runtime_calibration@(pc_cuda|mac_cpu))@[1-9][0-9]*$' THEN
        RETURN;
    END IF;
    IF job_profile <> 'authority' THEN
        RAISE EXCEPTION 'calibration validator Job identity must be the exact authority profile key';
    END IF;
    IF job_state IN ('pending', 'running') THEN
        RETURN;
    END IF;
    SELECT count(*) INTO open_count FROM runtime.command_slots
     WHERE job_id = checked_job AND state IN ('pending', 'running');
    IF open_count <> 0 THEN
        RAISE EXCEPTION 'calibration validator finalization is blocked by pending or running command slots';
    END IF;
    SELECT count(*) INTO matching_receipt_count
      FROM runtime.command_slots AS slot
      JOIN runtime.command_receipts AS receipt ON receipt.command_slot_id = slot.command_slot_id
     WHERE slot.job_id = checked_job
       AND slot.command_name = 'ValidateCalibrationRecord@2.1.3'
       AND slot.state = job_state
       AND receipt.outcome = job_state;
    IF matching_receipt_count <> 1 THEN
        RAISE EXCEPTION 'terminal calibration validator Job requires exactly one matching validation receipt';
    END IF;
    SELECT slot.command_slot_id, receipt.result_artifact_set_id
      INTO finalizer_slot, finalizer_set
      FROM runtime.command_slots AS slot
      JOIN runtime.command_receipts AS receipt ON receipt.command_slot_id = slot.command_slot_id
     WHERE slot.job_id = checked_job
       AND slot.command_name = 'ValidateCalibrationRecord@2.1.3'
       AND slot.state = job_state
       AND receipt.outcome = job_state;
    SELECT count(*) INTO job_set_count FROM runtime.artifact_sets WHERE job_id = checked_job;
    SELECT count(*) INTO anchor_count FROM runtime.calibration_record_anchors AS anchor
      JOIN runtime.command_slots AS slot ON slot.command_slot_id = anchor.command_slot_id
     WHERE slot.job_id = checked_job;
    IF job_state = 'succeeded' THEN
        SELECT artifact_set.member_count,
               count(*) FILTER (WHERE artifact.artifact_type = 'calibration_record'),
               count(*) FILTER (WHERE artifact.artifact_type = 'calibration_record_member'),
               count(*) FILTER (WHERE artifact.artifact_type = 'calibration_validation_receipt')
          INTO member_count, aggregate_count, member_record_count, validation_count
          FROM runtime.artifact_sets AS artifact_set
          JOIN runtime.artifact_set_members AS member ON member.artifact_set_id = artifact_set.artifact_set_id
          JOIN runtime.artifacts AS artifact ON artifact.artifact_id = member.artifact_id
         WHERE artifact_set.artifact_set_id = finalizer_set
         GROUP BY artifact_set.member_count;
        IF job_set_count <> 1 OR anchor_count <> 1
           OR member_count IS DISTINCT FROM 4 OR aggregate_count IS DISTINCT FROM 1
           OR member_record_count IS DISTINCT FROM 2 OR validation_count IS DISTINCT FROM 1
           OR NOT EXISTS (
                SELECT 1 FROM runtime.calibration_record_anchors AS anchor
                 WHERE anchor.command_slot_id = finalizer_slot
                   AND anchor.artifact_set_id = finalizer_set
           ) THEN
            RAISE EXCEPTION 'successful calibration validator Job requires exactly one four-member anchored record';
        END IF;
    ELSIF finalizer_set IS NOT NULL OR job_set_count <> 0 OR anchor_count <> 0 THEN
        RAISE EXCEPTION 'failed or denied calibration validator Job cannot bind artifacts or anchors';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION runtime.guard_calibration_validator_receipt()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    slot_name text;
    writer_key text;
    writer_profile text;
BEGIN
    SELECT slot.command_name, job.job_key, job.profile
      INTO slot_name, writer_key, writer_profile
      FROM runtime.command_slots AS slot
      JOIN runtime.jobs AS job ON job.job_id = slot.job_id
     WHERE slot.command_slot_id = NEW.command_slot_id;
    IF NOT FOUND OR slot_name <> 'ValidateCalibrationRecord@2.1.3'
       OR writer_profile <> 'authority'
       OR writer_key !~ '^autocut_calibration_validator:(shadow_calibration|runtime_calibration@(pc_cuda|mac_cpu))@[1-9][0-9]*$' THEN
        RETURN NULL;
    END IF;
    IF NEW.outcome = 'succeeded' THEN
        IF NOT EXISTS (
            SELECT 1 FROM runtime.calibration_record_anchors AS anchor
             WHERE anchor.receipt_id = NEW.receipt_id
               AND anchor.command_slot_id = NEW.command_slot_id
               AND anchor.artifact_set_id = NEW.result_artifact_set_id
        ) THEN
            RAISE EXCEPTION 'successful calibration validator receipt requires one immutable anchor';
        END IF;
    ELSIF NEW.outcome = 'denied' THEN
        IF NEW.failure_code IS DISTINCT FROM 'CALIBRATION_RECORD_INVALID'
           OR NEW.result_artifact_set_id IS NOT NULL
           OR EXISTS (SELECT 1 FROM runtime.artifact_sets AS item WHERE item.command_slot_id = NEW.command_slot_id)
           OR EXISTS (SELECT 1 FROM runtime.artifacts AS item JOIN runtime.artifact_sets AS set_item ON set_item.artifact_set_id = item.artifact_set_id WHERE set_item.command_slot_id = NEW.command_slot_id)
           OR EXISTS (SELECT 1 FROM runtime.calibration_record_anchors AS anchor WHERE anchor.command_slot_id = NEW.command_slot_id OR anchor.receipt_id = NEW.receipt_id) THEN
            RAISE EXCEPTION 'non-successful calibration validator receipt cannot own artifact sets, artifacts, or anchors';
        END IF;
    ELSIF NEW.outcome = 'failed' THEN
        IF NEW.failure_code IS DISTINCT FROM 'CALIBRATION_RECORD_VALIDATION_INDETERMINATE'
           OR NEW.result_artifact_set_id IS NOT NULL
           OR EXISTS (SELECT 1 FROM runtime.artifact_sets AS item WHERE item.command_slot_id = NEW.command_slot_id)
           OR EXISTS (SELECT 1 FROM runtime.artifacts AS item JOIN runtime.artifact_sets AS set_item ON set_item.artifact_set_id = item.artifact_set_id WHERE set_item.command_slot_id = NEW.command_slot_id)
           OR EXISTS (SELECT 1 FROM runtime.calibration_record_anchors AS anchor WHERE anchor.command_slot_id = NEW.command_slot_id OR anchor.receipt_id = NEW.receipt_id) THEN
            RAISE EXCEPTION 'non-successful calibration validator receipt cannot own artifact sets, artifacts, or anchors';
        END IF;
    ELSE
        RAISE EXCEPTION 'calibration validator receipt has an invalid outcome';
    END IF;
    RETURN NULL;
END $$;

CREATE TABLE runtime.runtime_calibration_capabilities (
    runtime_capability_id text NOT NULL CHECK (runtime_capability_id IN ('pc_cuda', 'mac_cpu')),
    timing_compatibility_sha256 text NOT NULL CHECK (timing_compatibility_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    runtime_measurement_identity_sha256 text NOT NULL CHECK (runtime_measurement_identity_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    build_audit_sha256 text NOT NULL CHECK (build_audit_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    measurement_identity_json jsonb NOT NULL,
    profile_source_sha256 text NOT NULL CHECK (profile_source_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    registry_snapshot_sha256 text NOT NULL CHECK (registry_snapshot_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    calibration_scope_key text NOT NULL CHECK (calibration_scope_key ~ '^runtime_calibration@(pc_cuda|mac_cpu)@[1-9][0-9]*$'),
    record_sha256 text NOT NULL CHECK (record_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    validation_receipt_sha256 text NOT NULL CHECK (validation_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    receipt_id uuid NOT NULL REFERENCES runtime.command_receipts (receipt_id),
    artifact_set_id uuid NOT NULL REFERENCES runtime.artifact_sets (artifact_set_id),
    command_slot_id uuid NOT NULL REFERENCES runtime.command_slots (command_slot_id),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (runtime_capability_id, timing_compatibility_sha256, runtime_measurement_identity_sha256),
    -- A v1 record may be retained and audited forever, but once it is bound
    -- into v2 it belongs to one measured environment only.  This prevents a
    -- PC CUDA closure from being relabelled as a Mac CPU capability (or vice
    -- versa); each environment needs a distinct accepted measurement record.
    UNIQUE (record_sha256),
    UNIQUE (validation_receipt_sha256),
    UNIQUE (command_slot_id),
    UNIQUE (receipt_id),
    UNIQUE (artifact_set_id),
    CHECK (
        timing_compatibility_sha256 <> ('sha256:' || repeat('0', 64))
        AND runtime_measurement_identity_sha256 <> ('sha256:' || repeat('0', 64))
        AND build_audit_sha256 <> ('sha256:' || repeat('0', 64))
        AND profile_source_sha256 <> ('sha256:' || repeat('0', 64))
        AND registry_snapshot_sha256 <> ('sha256:' || repeat('0', 64))
        AND record_sha256 <> ('sha256:' || repeat('0', 64))
        AND validation_receipt_sha256 <> ('sha256:' || repeat('0', 64))
    )
);

CREATE OR REPLACE FUNCTION runtime.prevent_runtime_calibration_capability_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'runtime calibration capabilities are immutable';
END;
$$;

CREATE TRIGGER runtime_calibration_capability_no_mutation
BEFORE UPDATE OR DELETE ON runtime.runtime_calibration_capabilities
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_runtime_calibration_capability_mutation();

CREATE OR REPLACE FUNCTION runtime.assert_runtime_calibration_capability()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    anchor runtime.calibration_record_anchors%ROWTYPE;
BEGIN
    SELECT * INTO anchor
      FROM runtime.calibration_record_anchors
     WHERE namespace = 'autocut_authority' AND scope_kind = 'calibration'
       AND scope_key = NEW.calibration_scope_key;
    IF NOT FOUND
       OR anchor.record_sha256 <> NEW.record_sha256
       OR anchor.validation_receipt_sha256 <> NEW.validation_receipt_sha256
       OR anchor.profile_source_sha256 <> NEW.profile_source_sha256
       OR anchor.registry_snapshot_sha256 <> NEW.registry_snapshot_sha256
       OR anchor.receipt_id <> NEW.receipt_id
       OR anchor.artifact_set_id <> NEW.artifact_set_id
       OR anchor.command_slot_id <> NEW.command_slot_id
       OR NEW.calibration_scope_key !~ ('^runtime_calibration@' || NEW.runtime_capability_id || '@[1-9][0-9]*$')
       OR jsonb_typeof(NEW.measurement_identity_json) <> 'object'
       OR NEW.measurement_identity_json->>'schema_version' <> 'runtime-measurement-identity-v1'
       OR NEW.measurement_identity_json->>'runtime_capability_id' <> NEW.runtime_capability_id
       OR jsonb_typeof(NEW.measurement_identity_json->'timing_compatibility') <> 'object'
       OR NEW.measurement_identity_json->'timing_compatibility'->>'timing_compatibility_sha256'
            <> NEW.timing_compatibility_sha256
       OR NEW.measurement_identity_json->'timing_compatibility'->>'build_audit_sha256'
            <> NEW.build_audit_sha256
       OR (
            NEW.runtime_capability_id = 'pc_cuda'
            AND NEW.measurement_identity_json->'timing_compatibility'->'runtime'->'device'->>'device_class' <> 'cuda'
       )
       OR (
            NEW.runtime_capability_id = 'mac_cpu'
            AND NEW.measurement_identity_json->'timing_compatibility'->'runtime'->'device'->>'device_class' <> 'cpu'
       ) THEN
        RAISE EXCEPTION 'runtime capability must close over one exact accepted record and measured identity';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER runtime_calibration_capability_exact_target
AFTER INSERT ON runtime.runtime_calibration_capabilities
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_runtime_calibration_capability();

COMMIT;
