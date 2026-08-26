-- Immutable authority CalibrationRecord persistence.
--
-- ValidateCalibrationRecord is a pure, read-only validator.  Its accepted
-- result is closed here rather than by a mutable logical head: one exact
-- profile scope owns one four-member record and one immutable anchor.

BEGIN;

LOCK TABLE runtime.jobs, runtime.command_slots, runtime.command_receipts,
           runtime.artifact_sets, runtime.artifacts, runtime.artifact_set_members
    IN SHARE ROW EXCLUSIVE MODE;

-- A migration must never bless rows written before its protected writer
-- existed.  A partially-created anchor relation is equally non-provenant.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.artifacts
         WHERE namespace = 'autocut_authority'
           AND scope_kind = 'calibration'
           AND scope_key ~ '^shadow_calibration@[1-9][0-9]*$'
    ) THEN
        RAISE EXCEPTION
            '0017 refuses pre-existing protected calibration artifacts; validator provenance is required';
    END IF;
    IF to_regclass('runtime.calibration_record_anchors') IS NOT NULL THEN
        RAISE EXCEPTION
            '0017 refuses a pre-existing calibration record anchor relation; validator provenance is required';
    END IF;
END $$;

CREATE TABLE runtime.calibration_record_anchors (
    namespace text NOT NULL CHECK (namespace = 'autocut_authority'),
    scope_kind text NOT NULL CHECK (scope_kind = 'calibration'),
    scope_key text NOT NULL CHECK (scope_key ~ '^shadow_calibration@[1-9][0-9]*$'),
    record_sha256 text NOT NULL UNIQUE CHECK (record_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    profile_source_sha256 text NOT NULL CHECK (profile_source_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    registry_snapshot_sha256 text NOT NULL CHECK (registry_snapshot_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    measurement_manifest_sha256 text NOT NULL CHECK (measurement_manifest_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    measurement_results_sha256 text NOT NULL CHECK (measurement_results_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    asr_member_sha256 text NOT NULL CHECK (asr_member_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    vad_member_sha256 text NOT NULL CHECK (vad_member_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    validation_receipt_sha256 text NOT NULL CHECK (validation_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    receipt_id uuid NOT NULL UNIQUE REFERENCES runtime.command_receipts (receipt_id),
    artifact_set_id uuid NOT NULL UNIQUE REFERENCES runtime.artifact_sets (artifact_set_id),
    aggregate_member_ordinal integer NOT NULL CHECK (aggregate_member_ordinal = 0),
    validation_member_ordinal integer NOT NULL CHECK (validation_member_ordinal = 3),
    command_slot_id uuid NOT NULL UNIQUE REFERENCES runtime.command_slots (command_slot_id),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (namespace, scope_kind, scope_key),
    CHECK (
        record_sha256 <> ('sha256:' || repeat('0', 64))
        AND profile_source_sha256 <> ('sha256:' || repeat('0', 64))
        AND registry_snapshot_sha256 <> ('sha256:' || repeat('0', 64))
        AND measurement_manifest_sha256 <> ('sha256:' || repeat('0', 64))
        AND measurement_results_sha256 <> ('sha256:' || repeat('0', 64))
        AND asr_member_sha256 <> ('sha256:' || repeat('0', 64))
        AND vad_member_sha256 <> ('sha256:' || repeat('0', 64))
        AND validation_receipt_sha256 <> ('sha256:' || repeat('0', 64))
    ),
    CHECK (
        record_sha256 <> asr_member_sha256
        AND record_sha256 <> vad_member_sha256
        AND record_sha256 <> validation_receipt_sha256
        AND asr_member_sha256 <> vad_member_sha256
        AND asr_member_sha256 <> validation_receipt_sha256
        AND vad_member_sha256 <> validation_receipt_sha256
    )
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
       AND checked_scope_key ~ '^shadow_calibration@[1-9][0-9]*$'
$$;

CREATE OR REPLACE FUNCTION runtime.guard_calibration_record_artifact()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    slot_name text;
    writer_key text;
    writer_profile text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF runtime.is_calibration_record_scope(OLD.namespace, OLD.scope_kind, OLD.scope_key) THEN
            RAISE EXCEPTION 'calibration record authority artifacts are immutable';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF runtime.is_calibration_record_scope(OLD.namespace, OLD.scope_kind, OLD.scope_key)
           OR runtime.is_calibration_record_scope(NEW.namespace, NEW.scope_kind, NEW.scope_key) THEN
            RAISE EXCEPTION 'calibration record authority artifacts are immutable';
        END IF;
        RETURN NEW;
    END IF;

    IF NOT runtime.is_calibration_record_scope(NEW.namespace, NEW.scope_kind, NEW.scope_key) THEN
        RETURN NEW;
    END IF;

    SELECT slot.command_name, job.job_key, job.profile
      INTO slot_name, writer_key, writer_profile
      FROM runtime.artifact_sets AS artifact_set
      JOIN runtime.command_slots AS slot ON slot.command_slot_id = artifact_set.command_slot_id
      JOIN runtime.jobs AS job ON job.job_id = artifact_set.job_id
     WHERE artifact_set.artifact_set_id = NEW.artifact_set_id;

    IF NOT FOUND
       OR slot_name <> 'ValidateCalibrationRecord@2.1.3'
       OR writer_key <> ('autocut_calibration_validator:' || NEW.scope_key)
       OR writer_profile <> 'authority'
       OR NEW.revision <> 1
       OR NEW.content_hash = ('sha256:' || repeat('0', 64))
       OR NOT (
            (NEW.artifact_type = 'calibration_record'
             AND NEW.logical_id = ('calibration-record/aggregate/' || NEW.scope_key || '/1'))
         OR (NEW.artifact_type = 'calibration_record_member'
             AND NEW.logical_id IN (
                 'calibration-record/member/asr/' || NEW.scope_key || '/1',
                 'calibration-record/member/vad/' || NEW.scope_key || '/1'
             ))
         OR (NEW.artifact_type = 'calibration_validation_receipt'
             AND NEW.logical_id = ('calibration-record/validation/' || NEW.scope_key || '/1'))
       ) THEN
        RAISE EXCEPTION 'calibration record authority write requires the dedicated validator and exact member identity';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER runtime_guard_calibration_record_artifact
BEFORE INSERT OR UPDATE OR DELETE ON runtime.artifacts
FOR EACH ROW EXECUTE FUNCTION runtime.guard_calibration_record_artifact();

CREATE OR REPLACE FUNCTION runtime.assert_calibration_record_artifact_set()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    checked_set uuid;
    expected_scope_key text;
    protected_count integer;
    actual_count integer;
    expected_member_count integer;
    writer_slot uuid;
    writer_command text;
    writer_key text;
    writer_profile text;
    writer_state text;
BEGIN
    checked_set := COALESCE(NEW.artifact_set_id, OLD.artifact_set_id);
    SELECT count(*)
      INTO protected_count
      FROM runtime.artifacts AS artifact
     WHERE artifact.artifact_set_id = checked_set
       AND runtime.is_calibration_record_scope(artifact.namespace, artifact.scope_kind, artifact.scope_key);

    SELECT slot.command_slot_id, slot.command_name, job.job_key, job.profile, slot.state
      INTO writer_slot, writer_command, writer_key, writer_profile, writer_state
      FROM runtime.artifact_sets AS artifact_set
      JOIN runtime.command_slots AS slot ON slot.command_slot_id = artifact_set.command_slot_id
      JOIN runtime.jobs AS job ON job.job_id = artifact_set.job_id
     WHERE artifact_set.artifact_set_id = checked_set;
    IF protected_count = 0 THEN
        IF writer_command = 'ValidateCalibrationRecord@2.1.3'
           AND writer_key ~ '^autocut_calibration_validator:shadow_calibration@[1-9][0-9]*$'
           AND writer_profile = 'authority' THEN
            RAISE EXCEPTION 'calibration validator may own only its protected four-member artifact set';
        END IF;
        RETURN NULL;
    END IF;

    SELECT artifact.scope_key INTO expected_scope_key
      FROM runtime.artifacts AS artifact
     WHERE artifact.artifact_set_id = checked_set
       AND runtime.is_calibration_record_scope(artifact.namespace, artifact.scope_kind, artifact.scope_key)
     LIMIT 1;
    SELECT artifact_set.member_count, count(member.artifact_id)
      INTO expected_member_count, actual_count
      FROM runtime.artifact_sets AS artifact_set
      JOIN runtime.artifact_set_members AS member ON member.artifact_set_id = artifact_set.artifact_set_id
     WHERE artifact_set.artifact_set_id = checked_set
     GROUP BY artifact_set.member_count;

    IF NOT FOUND OR expected_member_count <> 4 OR actual_count <> 4 OR protected_count <> 4
       OR writer_command IS DISTINCT FROM 'ValidateCalibrationRecord@2.1.3'
       OR writer_key IS DISTINCT FROM ('autocut_calibration_validator:' || expected_scope_key)
       OR writer_profile IS DISTINCT FROM 'authority'
       OR writer_state IS DISTINCT FROM 'succeeded'
       OR NOT EXISTS (
            SELECT 1 FROM runtime.artifact_set_members AS member
            JOIN runtime.artifacts AS artifact ON artifact.artifact_id = member.artifact_id
            WHERE member.artifact_set_id = checked_set AND member.ordinal = 0
              AND artifact.namespace = 'autocut_authority' AND artifact.scope_kind = 'calibration'
              AND artifact.scope_key = expected_scope_key AND artifact.artifact_type = 'calibration_record'
              AND artifact.logical_id = ('calibration-record/aggregate/' || expected_scope_key || '/1')
              AND artifact.revision = 1
       )
       OR NOT EXISTS (
            SELECT 1 FROM runtime.artifact_set_members AS member
            JOIN runtime.artifacts AS artifact ON artifact.artifact_id = member.artifact_id
            WHERE member.artifact_set_id = checked_set AND member.ordinal = 1
              AND artifact.namespace = 'autocut_authority' AND artifact.scope_kind = 'calibration'
              AND artifact.scope_key = expected_scope_key AND artifact.artifact_type = 'calibration_record_member'
              AND artifact.logical_id = ('calibration-record/member/asr/' || expected_scope_key || '/1')
              AND artifact.revision = 1
       )
       OR NOT EXISTS (
            SELECT 1 FROM runtime.artifact_set_members AS member
            JOIN runtime.artifacts AS artifact ON artifact.artifact_id = member.artifact_id
            WHERE member.artifact_set_id = checked_set AND member.ordinal = 2
              AND artifact.namespace = 'autocut_authority' AND artifact.scope_kind = 'calibration'
              AND artifact.scope_key = expected_scope_key AND artifact.artifact_type = 'calibration_record_member'
              AND artifact.logical_id = ('calibration-record/member/vad/' || expected_scope_key || '/1')
              AND artifact.revision = 1
       )
       OR NOT EXISTS (
            SELECT 1 FROM runtime.artifact_set_members AS member
            JOIN runtime.artifacts AS artifact ON artifact.artifact_id = member.artifact_id
            WHERE member.artifact_set_id = checked_set AND member.ordinal = 3
              AND artifact.namespace = 'autocut_authority' AND artifact.scope_kind = 'calibration'
              AND artifact.scope_key = expected_scope_key AND artifact.artifact_type = 'calibration_validation_receipt'
              AND artifact.logical_id = ('calibration-record/validation/' || expected_scope_key || '/1')
              AND artifact.revision = 1
       ) THEN
        RAISE EXCEPTION 'calibration record artifact set must contain the exact four ordered members';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM runtime.command_receipts AS receipt
         WHERE receipt.command_slot_id = writer_slot
           AND receipt.outcome = 'succeeded'
           AND receipt.result_artifact_set_id = checked_set
    ) OR NOT EXISTS (
        SELECT 1 FROM runtime.calibration_record_anchors AS anchor
         WHERE anchor.command_slot_id = writer_slot
           AND anchor.artifact_set_id = checked_set
    ) THEN
        RAISE EXCEPTION 'calibration record artifact set requires its exact succeeded receipt and immutable anchor';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER runtime_calibration_record_artifact_set_from_artifact
AFTER INSERT OR UPDATE OR DELETE ON runtime.artifacts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_calibration_record_artifact_set();
CREATE CONSTRAINT TRIGGER runtime_calibration_record_artifact_set_from_member
AFTER INSERT OR UPDATE OR DELETE ON runtime.artifact_set_members
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_calibration_record_artifact_set();
CREATE CONSTRAINT TRIGGER runtime_calibration_record_artifact_set_from_set
AFTER INSERT OR UPDATE ON runtime.artifact_sets
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_calibration_record_artifact_set();

CREATE OR REPLACE FUNCTION runtime.prevent_calibration_record_anchor_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'calibration record anchors are immutable';
END;
$$;

CREATE TRIGGER runtime_calibration_record_anchor_no_mutation
BEFORE UPDATE OR DELETE ON runtime.calibration_record_anchors
FOR EACH ROW EXECUTE FUNCTION runtime.prevent_calibration_record_anchor_mutation();

CREATE OR REPLACE FUNCTION runtime.assert_calibration_record_anchor()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    aggregate_payload jsonb;
    validation_payload jsonb;
    validator_job_state text;
BEGIN
    SELECT aggregate.payload_json, validation.payload_json, job.state
      INTO aggregate_payload, validation_payload, validator_job_state
      FROM runtime.command_receipts AS receipt
      JOIN runtime.command_slots AS slot ON slot.command_slot_id = receipt.command_slot_id
      JOIN runtime.jobs AS job ON job.job_id = slot.job_id
      JOIN runtime.artifact_sets AS artifact_set
        ON artifact_set.artifact_set_id = receipt.result_artifact_set_id
       AND artifact_set.command_slot_id = slot.command_slot_id
       AND artifact_set.member_count = 4
      JOIN runtime.artifact_set_members AS aggregate_member
        ON aggregate_member.artifact_set_id = artifact_set.artifact_set_id
       AND aggregate_member.ordinal = NEW.aggregate_member_ordinal
      JOIN runtime.artifacts AS aggregate ON aggregate.artifact_id = aggregate_member.artifact_id
      JOIN runtime.artifact_set_members AS asr_member
        ON asr_member.artifact_set_id = artifact_set.artifact_set_id AND asr_member.ordinal = 1
      JOIN runtime.artifacts AS asr ON asr.artifact_id = asr_member.artifact_id
      JOIN runtime.artifact_set_members AS vad_member
        ON vad_member.artifact_set_id = artifact_set.artifact_set_id AND vad_member.ordinal = 2
      JOIN runtime.artifacts AS vad ON vad.artifact_id = vad_member.artifact_id
      JOIN runtime.artifact_set_members AS validation_member
        ON validation_member.artifact_set_id = artifact_set.artifact_set_id
       AND validation_member.ordinal = NEW.validation_member_ordinal
      JOIN runtime.artifacts AS validation ON validation.artifact_id = validation_member.artifact_id
     WHERE receipt.receipt_id = NEW.receipt_id
       AND receipt.result_artifact_set_id = NEW.artifact_set_id
       AND receipt.outcome = 'succeeded'
       AND slot.command_slot_id = NEW.command_slot_id
       AND slot.command_name = 'ValidateCalibrationRecord@2.1.3'
       AND slot.state = 'succeeded'
       AND job.job_key = ('autocut_calibration_validator:' || NEW.scope_key)
       AND job.profile = 'authority'
       AND aggregate.namespace = NEW.namespace AND aggregate.scope_kind = NEW.scope_kind
       AND aggregate.scope_key = NEW.scope_key AND aggregate.artifact_type = 'calibration_record'
       AND aggregate.logical_id = ('calibration-record/aggregate/' || NEW.scope_key || '/1')
       AND aggregate.revision = 1 AND aggregate.content_hash = NEW.record_sha256
       AND asr.namespace = NEW.namespace AND asr.scope_kind = NEW.scope_kind
       AND asr.scope_key = NEW.scope_key AND asr.artifact_type = 'calibration_record_member'
       AND asr.logical_id = ('calibration-record/member/asr/' || NEW.scope_key || '/1')
       AND asr.revision = 1 AND asr.content_hash = NEW.asr_member_sha256
       AND vad.namespace = NEW.namespace AND vad.scope_kind = NEW.scope_kind
       AND vad.scope_key = NEW.scope_key AND vad.artifact_type = 'calibration_record_member'
       AND vad.logical_id = ('calibration-record/member/vad/' || NEW.scope_key || '/1')
       AND vad.revision = 1 AND vad.content_hash = NEW.vad_member_sha256
       AND validation.namespace = NEW.namespace AND validation.scope_kind = NEW.scope_kind
       AND validation.scope_key = NEW.scope_key AND validation.artifact_type = 'calibration_validation_receipt'
       AND validation.logical_id = ('calibration-record/validation/' || NEW.scope_key || '/1')
       AND validation.revision = 1 AND validation.content_hash = NEW.validation_receipt_sha256;

    IF NOT FOUND
       OR validator_job_state IS DISTINCT FROM 'succeeded'
       OR jsonb_typeof(aggregate_payload) IS DISTINCT FROM 'object'
       OR aggregate_payload->>'schema_version' IS DISTINCT FROM 'calibration-record-v1'
       OR aggregate_payload->>'record_kind' IS DISTINCT FROM 'shadow_native_timing'
       OR aggregate_payload->>'member_count' IS DISTINCT FROM '2'
       OR jsonb_typeof(aggregate_payload->'identity') IS DISTINCT FROM 'object'
       OR aggregate_payload->'identity'->>'profile_source_sha256' IS DISTINCT FROM NEW.profile_source_sha256
       OR aggregate_payload->'identity'->>'registry_snapshot_sha256' IS DISTINCT FROM NEW.registry_snapshot_sha256
       OR aggregate_payload->>'measurement_manifest_sha256' IS DISTINCT FROM NEW.measurement_manifest_sha256
       OR aggregate_payload->>'measurement_results_sha256' IS DISTINCT FROM NEW.measurement_results_sha256
       OR aggregate_payload->>'asr_member_sha256' IS DISTINCT FROM NEW.asr_member_sha256
       OR aggregate_payload->>'vad_member_sha256' IS DISTINCT FROM NEW.vad_member_sha256
       OR jsonb_typeof(validation_payload) IS DISTINCT FROM 'object'
       OR validation_payload->>'schema_version' IS DISTINCT FROM 'calibration-record-validation-receipt-v1'
       OR validation_payload->>'decision' IS DISTINCT FROM 'accepted'
       OR validation_payload->>'validator_command' IS DISTINCT FROM 'ValidateCalibrationRecord@2.1.3'
       OR validation_payload->>'record_sha256' IS DISTINCT FROM NEW.record_sha256
       OR validation_payload->>'asr_member_sha256' IS DISTINCT FROM NEW.asr_member_sha256
       OR validation_payload->>'vad_member_sha256' IS DISTINCT FROM NEW.vad_member_sha256
       OR validation_payload->>'measurement_manifest_sha256' IS DISTINCT FROM NEW.measurement_manifest_sha256
       OR validation_payload->>'measurement_results_sha256' IS DISTINCT FROM NEW.measurement_results_sha256 THEN
        RAISE EXCEPTION 'calibration record anchor does not close over its exact accepted validator result';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER runtime_calibration_record_anchor_exact_target
AFTER INSERT ON runtime.calibration_record_anchors
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_calibration_record_anchor();

-- The generic Pipeline finalizer remains the rule for ordinary Jobs.  The
-- calibration validator has a different, equally closed terminal proof and
-- must never need a synthetic FinalizeRunOutcome or a fifth member.
CREATE OR REPLACE FUNCTION runtime.assert_exact_run_finalization(checked_job uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    job_state text;
    job_key text;
    job_profile text;
    finalizer_count integer;
    finalizer_outcome text;
    finalizer_set uuid;
    open_count integer;
    run_outcome_count integer;
    member_count integer;
BEGIN
    SELECT job.state, job.job_key, job.profile INTO job_state, job_key, job_profile
      FROM runtime.jobs AS job WHERE job.job_id = checked_job;
    IF NOT FOUND OR job_state IN ('pending', 'running') THEN
        RETURN;
    END IF;
    IF job_key LIKE 'autocut_calibration_validator:%' THEN
        IF job_key !~ '^autocut_calibration_validator:shadow_calibration@[1-9][0-9]*$'
           OR job_profile <> 'authority' THEN
            RAISE EXCEPTION 'calibration validator Job identity must be the exact authority profile key';
        END IF;
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

CREATE OR REPLACE FUNCTION runtime.guard_calibration_validator_job_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.job_key LIKE 'autocut_calibration_validator:%' THEN
        IF NEW.job_key !~ '^autocut_calibration_validator:shadow_calibration@[1-9][0-9]*$'
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

CREATE TRIGGER runtime_calibration_validator_job_identity_guard
BEFORE INSERT OR UPDATE ON runtime.jobs
FOR EACH ROW EXECUTE FUNCTION runtime.guard_calibration_validator_job_identity();

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
    IF NOT FOUND OR job_key !~ '^autocut_calibration_validator:shadow_calibration@[1-9][0-9]*$' THEN
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

CREATE OR REPLACE FUNCTION runtime.check_calibration_validator_finalization_from_job()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM runtime.assert_exact_calibration_validator_finalization(NEW.job_id);
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_calibration_validator_finalization_from_job
AFTER INSERT OR UPDATE ON runtime.jobs
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_calibration_validator_finalization_from_job();

CREATE OR REPLACE FUNCTION runtime.check_calibration_validator_finalization_from_slot()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM runtime.assert_exact_calibration_validator_finalization(NEW.job_id);
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_calibration_validator_finalization_from_slot
AFTER INSERT OR UPDATE ON runtime.command_slots
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_calibration_validator_finalization_from_slot();

CREATE OR REPLACE FUNCTION runtime.guard_calibration_validator_receipt()
RETURNS trigger
LANGUAGE plpgsql
AS $$
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
       OR writer_key !~ '^autocut_calibration_validator:shadow_calibration@[1-9][0-9]*$' THEN
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
           OR EXISTS (
                SELECT 1 FROM runtime.artifact_sets AS artifact_set
                 WHERE artifact_set.command_slot_id = NEW.command_slot_id
           )
           OR EXISTS (
                SELECT 1 FROM runtime.artifacts AS artifact
                JOIN runtime.artifact_sets AS artifact_set
                  ON artifact_set.artifact_set_id = artifact.artifact_set_id
                 WHERE artifact_set.command_slot_id = NEW.command_slot_id
           )
           OR EXISTS (
                SELECT 1 FROM runtime.calibration_record_anchors AS anchor
                 WHERE anchor.command_slot_id = NEW.command_slot_id OR anchor.receipt_id = NEW.receipt_id
           ) THEN
            RAISE EXCEPTION 'non-successful calibration validator receipt cannot own artifact sets, artifacts, or anchors';
        END IF;
    ELSIF NEW.outcome = 'failed' THEN
        IF NEW.failure_code IS DISTINCT FROM 'CALIBRATION_RECORD_VALIDATION_INDETERMINATE'
           OR NEW.result_artifact_set_id IS NOT NULL
           OR EXISTS (
                SELECT 1 FROM runtime.artifact_sets AS artifact_set
                 WHERE artifact_set.command_slot_id = NEW.command_slot_id
           )
           OR EXISTS (
                SELECT 1 FROM runtime.artifacts AS artifact
                JOIN runtime.artifact_sets AS artifact_set
                  ON artifact_set.artifact_set_id = artifact.artifact_set_id
                 WHERE artifact_set.command_slot_id = NEW.command_slot_id
           )
           OR EXISTS (
                SELECT 1 FROM runtime.calibration_record_anchors AS anchor
                 WHERE anchor.command_slot_id = NEW.command_slot_id OR anchor.receipt_id = NEW.receipt_id
           ) THEN
            RAISE EXCEPTION 'non-successful calibration validator receipt cannot own artifact sets, artifacts, or anchors';
        END IF;
    ELSE
        RAISE EXCEPTION 'calibration validator receipt has an invalid outcome';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER runtime_calibration_validator_receipt_guard
AFTER INSERT ON runtime.command_receipts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.guard_calibration_validator_receipt();

COMMIT;
