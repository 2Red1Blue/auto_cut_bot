-- Immutable production-QC collector capability acceptance.
--
-- AcceptProductionRenderQcCollectorCapability@1 is the only writer allowed to
-- materialize an accepted collector capability.  The logical identity is the
-- complete (profile_id, qc_runner_identity_sha256, policy_source_sha256,
-- registry_snapshot_sha256) tuple; the scope key is deterministic from that
-- tuple so a capability can never be relabeled onto another profile or policy
-- lineage.  A denied or failed validator command owns no set, member, or row.

BEGIN;

LOCK TABLE runtime.jobs, runtime.command_slots, runtime.command_receipts,
           runtime.artifact_sets, runtime.artifacts, runtime.artifact_set_members
    IN SHARE ROW EXCLUSIVE MODE;

-- A migration must never bless rows written before its protected writer
-- existed.  A partially-created capability relation is equally non-provenant.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.artifacts
         WHERE namespace = 'autocut_authority'
           AND scope_kind = 'production_qc_collector_capability'
    ) THEN
        RAISE EXCEPTION
            '0059 refuses pre-existing protected QC collector capability artifacts; validator provenance is required';
    END IF;
    IF to_regclass('runtime.production_qc_collector_capabilities') IS NOT NULL THEN
        RAISE EXCEPTION
            '0059 refuses a pre-existing QC collector capability relation; validator provenance is required';
    END IF;
END $$;

CREATE TABLE runtime.production_qc_collector_capabilities (
    namespace text NOT NULL CHECK (namespace = 'autocut_authority'),
    scope_kind text NOT NULL CHECK (scope_kind = 'production_qc_collector_capability'),
    scope_key text NOT NULL CHECK (
        scope_key ~ '^production_qc_collector_capability:[a-z0-9][a-z0-9._-]{0,127}:[0-9a-f]{64}:[0-9a-f]{64}:[0-9a-f]{64}$'
    ),
    profile_id text NOT NULL CHECK (
        profile_id ~ '^[a-z][a-z0-9._-]{0,127}$'
    ),
    qc_runner_identity_sha256 text NOT NULL CHECK (
        qc_runner_identity_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND qc_runner_identity_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    policy_source_sha256 text NOT NULL CHECK (
        policy_source_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND policy_source_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    registry_snapshot_sha256 text NOT NULL CHECK (
        registry_snapshot_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND registry_snapshot_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    collector_registry_sha256 text NOT NULL CHECK (
        collector_registry_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND collector_registry_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    required_check_set_version text NOT NULL CHECK (
        required_check_set_version = 'production-av-qc-v1'
    ),
    runner_schema_version text NOT NULL CHECK (
        runner_schema_version = 'production-qc-runner-v1'
    ),
    fixed_environment_sha256 text NOT NULL CHECK (
        fixed_environment_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND fixed_environment_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    ffmpeg_executable_sha256 text NOT NULL CHECK (
        ffmpeg_executable_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND ffmpeg_executable_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    ffmpeg_executable_byte_length bigint NOT NULL CHECK (ffmpeg_executable_byte_length > 0),
    ffmpeg_version_output_sha256 text NOT NULL CHECK (
        ffmpeg_version_output_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND ffmpeg_version_output_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    ffprobe_executable_sha256 text NOT NULL CHECK (
        ffprobe_executable_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND ffprobe_executable_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    ffprobe_executable_byte_length bigint NOT NULL CHECK (ffprobe_executable_byte_length > 0),
    ffprobe_version_output_sha256 text NOT NULL CHECK (
        ffprobe_version_output_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND ffprobe_version_output_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    capability_request_json text NOT NULL CHECK (
        octet_length(capability_request_json) BETWEEN 1 AND 65536
    ),
    capability_request_sha256 text NOT NULL UNIQUE CHECK (
        capability_request_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND capability_request_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    measurement_member_sha256 text NOT NULL CHECK (
        measurement_member_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND measurement_member_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    capability_member_sha256 text NOT NULL CHECK (
        capability_member_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND capability_member_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    decision text NOT NULL CHECK (decision = 'accepted'),
    receipt_id uuid NOT NULL UNIQUE REFERENCES runtime.command_receipts (receipt_id),
    artifact_set_id uuid NOT NULL UNIQUE REFERENCES runtime.artifact_sets (artifact_set_id),
    command_slot_id uuid NOT NULL UNIQUE REFERENCES runtime.command_slots (command_slot_id),
    authority_revision integer NOT NULL CHECK (authority_revision >= 1),
    authority_bundle_sha256 text NOT NULL CHECK (
        authority_bundle_sha256 ~ '^sha256:[0-9a-f]{64}$'
        AND authority_bundle_sha256 <> 'sha256:' || repeat('0', 64)
    ),
    source_commit text NOT NULL CHECK (source_commit ~ '^[0-9a-f]{40}$'),
    inventory_commit text NOT NULL CHECK (inventory_commit ~ '^[0-9a-f]{40}$'),
    lock_commit text NOT NULL CHECK (lock_commit ~ '^[0-9a-f]{40}$'),
    accepted_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (namespace, scope_kind, scope_key),
    UNIQUE (profile_id, qc_runner_identity_sha256, policy_source_sha256, registry_snapshot_sha256),
    CHECK (measurement_member_sha256 <> capability_member_sha256)
);

CREATE OR REPLACE FUNCTION runtime.is_production_qc_collector_capability_scope(
    checked_namespace text,
    checked_scope_kind text,
    checked_scope_key text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT checked_namespace = 'autocut_authority'
       AND checked_scope_kind = 'production_qc_collector_capability'
       AND checked_scope_key ~ '^production_qc_collector_capability:[a-z0-9][a-z0-9._-]{0,127}:[0-9a-f]{64}:[0-9a-f]{64}:[0-9a-f]{64}$'
$$;

-- Artifacts inside the capability scope may only be created by the exact
-- validator command running under its dedicated authority Job, and they are
-- immutable afterwards.
CREATE OR REPLACE FUNCTION runtime.guard_production_qc_collector_capability_artifact()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    slot_name text;
    writer_key text;
    writer_profile text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF runtime.is_production_qc_collector_capability_scope(
            OLD.namespace, OLD.scope_kind, OLD.scope_key
        ) THEN
            RAISE EXCEPTION 'production QC collector capability artifacts are immutable';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF runtime.is_production_qc_collector_capability_scope(
            OLD.namespace, OLD.scope_kind, OLD.scope_key
        ) OR runtime.is_production_qc_collector_capability_scope(
            NEW.namespace, NEW.scope_kind, NEW.scope_key
        ) THEN
            RAISE EXCEPTION 'production QC collector capability artifacts are immutable';
        END IF;
        RETURN NEW;
    END IF;

    IF NOT runtime.is_production_qc_collector_capability_scope(
        NEW.namespace, NEW.scope_kind, NEW.scope_key
    ) THEN
        RETURN NEW;
    END IF;

    SELECT slot.command_name, job.job_key, job.profile
      INTO slot_name, writer_key, writer_profile
      FROM runtime.artifact_sets AS artifact_set
      JOIN runtime.command_slots AS slot ON slot.command_slot_id = artifact_set.command_slot_id
      JOIN runtime.jobs AS job ON job.job_id = artifact_set.job_id
     WHERE artifact_set.artifact_set_id = NEW.artifact_set_id;

    IF NOT FOUND
       OR slot_name <> 'AcceptProductionRenderQcCollectorCapability@1'
       OR writer_profile <> 'authority'
       OR writer_key IS DISTINCT FROM (
            'autocut_production_qc_collector_validator:'
            || (regexp_match(
                    NEW.scope_key,
                    '^production_qc_collector_capability:([a-z0-9][a-z0-9._-]{0,127}):[0-9a-f]{64}:[0-9a-f]{64}:[0-9a-f]{64}$'
                ))[1]
            || ':'
            || (regexp_match(
                    NEW.scope_key,
                    '^production_qc_collector_capability:[a-z0-9][a-z0-9._-]{0,127}:([0-9a-f]{64}):[0-9a-f]{64}:[0-9a-f]{64}$'
                ))[1]
            || ':'
            || (regexp_match(
                    NEW.scope_key,
                    '^production_qc_collector_capability:[a-z0-9][a-z0-9._-]{0,127}:[0-9a-f]{64}:([0-9a-f]{64}):[0-9a-f]{64}$'
                ))[1]
            || ':'
            || (regexp_match(
                    NEW.scope_key,
                    '^production_qc_collector_capability:[a-z0-9][a-z0-9._-]{0,127}:[0-9a-f]{64}:[0-9a-f]{64}:([0-9a-f]{64})$'
                ))[1]
       )
       OR NEW.revision <> 1
       OR NEW.content_hash = ('sha256:' || repeat('0', 64))
       OR NOT (
            (NEW.artifact_type = 'production_qc_collector_measurement'
             AND NEW.logical_id = 'measurement')
         OR (NEW.artifact_type = 'production_qc_collector_capability'
             AND NEW.logical_id = 'decision')
       ) THEN
        RAISE EXCEPTION
            'production QC collector capability artifacts require the exact validator provenance';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS production_qc_collector_capability_artifact_guard
    ON runtime.artifacts;
CREATE TRIGGER production_qc_collector_capability_artifact_guard
    BEFORE INSERT OR UPDATE OR DELETE ON runtime.artifacts
    FOR EACH ROW EXECUTE FUNCTION
        runtime.guard_production_qc_collector_capability_artifact();

-- The capability relation itself is insert-only, and its scope key must be
-- the deterministic projection of the accepted identity tuple.  The request
-- digest must be the canonical SHA-256 of the stored request JSON.
CREATE OR REPLACE FUNCTION runtime.guard_production_qc_collector_capability_row()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'production QC collector capabilities are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'production QC collector capabilities are insert-only';
    END IF;
    IF NEW.scope_key IS DISTINCT FROM (
        'production_qc_collector_capability:'
        || NEW.profile_id || ':'
        || substring(NEW.policy_source_sha256 from 8) || ':'
        || substring(NEW.registry_snapshot_sha256 from 8) || ':'
        || substring(NEW.qc_runner_identity_sha256 from 8)
    ) THEN
        RAISE EXCEPTION 'production QC collector capability scope key is not deterministic';
    END IF;
    IF NEW.capability_request_sha256
       IS DISTINCT FROM ('sha256:' || encode(sha256(convert_to(NEW.capability_request_json, 'UTF8')), 'hex')) THEN
        RAISE EXCEPTION 'production QC collector capability request digest does not match its stored JSON';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS production_qc_collector_capability_row_guard
    ON runtime.production_qc_collector_capabilities;
CREATE TRIGGER production_qc_collector_capability_row_guard
    BEFORE INSERT OR UPDATE OR DELETE ON runtime.production_qc_collector_capabilities
    FOR EACH ROW EXECUTE FUNCTION
        runtime.guard_production_qc_collector_capability_row();

-- The capability validator Job terminalizes through its own protected command,
-- not through FinalizeRunOutcome.  This override preserves the calibration
-- validator exemption from 0017 and adds the exact capability lineage check.
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
    IF job_key LIKE 'autocut_production_qc_collector_validator:%' THEN
        IF job_key !~ '^autocut_production_qc_collector_validator:[a-z0-9][a-z0-9._-]{0,127}:[0-9a-f]{64}:[0-9a-f]{64}:[0-9a-f]{64}$'
           OR job_profile <> 'authority' THEN
            RAISE EXCEPTION 'production QC collector validator Job identity must be the exact authority capability lineage';
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

COMMIT;
