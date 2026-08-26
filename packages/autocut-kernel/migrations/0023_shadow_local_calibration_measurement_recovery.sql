-- Durable recovery aggregate for MeasureShadowLocalCalibrationCommand@1.
--
-- This is deliberately isolated from the complete-source shadow-calibration
-- aggregate in 0016.  It persists unaccepted local measurements only: no
-- calibration record, registry anchor, installed profile, or publish decision
-- is represented by these tables.

BEGIN;

CREATE TABLE runtime.shadow_local_calibration_measurement_attempts (
    attempt_id uuid PRIMARY KEY,
    command_slot_id uuid NOT NULL REFERENCES runtime.command_slots (command_slot_id),
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    plan_hash text NOT NULL CHECK (plan_hash ~ '^sha256:[0-9a-f]{64}$'),
    attempt_ordinal integer NOT NULL CHECK (attempt_ordinal >= 1),
    previous_attempt_id uuid UNIQUE REFERENCES runtime.shadow_local_calibration_measurement_attempts (attempt_id),
    state text NOT NULL CHECK (state IN ('prepared', 'collecting', 'ready', 'indeterminate', 'committed', 'denied')),
    version bigint NOT NULL CHECK (version >= 0),
    recovery_lease_token text,
    recovery_lease_expires_at timestamptz,
    plan_json jsonb NOT NULL,
    retry_decision_reference_sha256 text CHECK (
        retry_decision_reference_sha256 IS NULL
        OR retry_decision_reference_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    retry_member_case_sha256 text CHECK (
        retry_member_case_sha256 IS NULL
        OR retry_member_case_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    retry_predecessor_version bigint CHECK (
        retry_predecessor_version IS NULL OR retry_predecessor_version >= 0
    ),
    retry_reason_code text CHECK (
        retry_reason_code IS NULL
        OR retry_reason_code IN ('NATIVE_OUTCOME_UNKNOWN', 'REQUEST_NOT_STARTED')
    ),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    completed_at timestamptz,
    UNIQUE (job_id, plan_hash, attempt_ordinal),
    CHECK ((recovery_lease_token IS NULL) = (recovery_lease_expires_at IS NULL)),
    CHECK (
        (attempt_ordinal = 1 AND previous_attempt_id IS NULL
         AND retry_decision_reference_sha256 IS NULL AND retry_member_case_sha256 IS NULL
         AND retry_predecessor_version IS NULL AND retry_reason_code IS NULL)
        OR (attempt_ordinal > 1 AND previous_attempt_id IS NOT NULL
            AND retry_decision_reference_sha256 IS NOT NULL AND retry_member_case_sha256 IS NOT NULL
            AND retry_predecessor_version IS NOT NULL AND retry_reason_code IS NOT NULL)
    ),
    CHECK ((state IN ('committed', 'denied')) = (completed_at IS NOT NULL))
);

CREATE INDEX runtime_shadow_local_measurement_attempt_recovery_idx
    ON runtime.shadow_local_calibration_measurement_attempts (state, recovery_lease_expires_at)
    WHERE state IN ('collecting', 'indeterminate');
CREATE INDEX runtime_shadow_local_measurement_attempt_slot_idx
    ON runtime.shadow_local_calibration_measurement_attempts (command_slot_id, attempt_ordinal);

CREATE TABLE runtime.shadow_local_calibration_measurement_members (
    attempt_id uuid NOT NULL REFERENCES runtime.shadow_local_calibration_measurement_attempts (attempt_id),
    case_sha256 text NOT NULL CHECK (case_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    member_ordinal integer NOT NULL CHECK (member_ordinal >= 0),
    case_json jsonb NOT NULL,
    request_json jsonb NOT NULL,
    source_job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    source_blob_object_id uuid NOT NULL REFERENCES storage.blob_objects (object_id),
    source_blob_content_hash text NOT NULL CHECK (source_blob_content_hash ~ '^sha256:[0-9a-f]{64}$'),
    source_blob_byte_length bigint NOT NULL CHECK (source_blob_byte_length >= 0),
    source_blob_media_type text NOT NULL,
    source_blob_reference_sha256 text NOT NULL CHECK (
        source_blob_reference_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    binding_sha256 text NOT NULL CHECK (binding_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    service_profile_sha256 text NOT NULL CHECK (
        service_profile_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    max_response_bytes bigint NOT NULL CHECK (max_response_bytes > 0),
    state text NOT NULL CHECK (
        state IN ('pending', 'invoking', 'not_started', 'staged', 'indeterminate', 'rejected')
    ),
    version bigint NOT NULL CHECK (version >= 0),
    lease_token text,
    lease_expires_at timestamptz,
    raw_blob_object_id uuid REFERENCES storage.blob_objects (object_id),
    raw_content_hash text CHECK (raw_content_hash IS NULL OR raw_content_hash ~ '^sha256:[0-9a-f]{64}$'),
    raw_byte_length bigint CHECK (raw_byte_length IS NULL OR raw_byte_length > 0),
    raw_media_type text,
    evidence_json jsonb,
    busy_proof_blob_object_id uuid REFERENCES storage.blob_objects (object_id),
    busy_proof_content_hash text CHECK (
        busy_proof_content_hash IS NULL OR busy_proof_content_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    busy_proof_byte_length bigint CHECK (
        busy_proof_byte_length IS NULL OR busy_proof_byte_length > 0
    ),
    busy_proof_media_type text,
    busy_proof_json jsonb,
    PRIMARY KEY (attempt_id, case_sha256),
    UNIQUE (attempt_id, member_ordinal),
    CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL)),
    CHECK (
        (state = 'staged' AND raw_blob_object_id IS NOT NULL AND raw_content_hash IS NOT NULL
         AND raw_byte_length IS NOT NULL AND raw_media_type IS NOT NULL AND evidence_json IS NOT NULL
         AND busy_proof_blob_object_id IS NULL AND busy_proof_content_hash IS NULL
         AND busy_proof_byte_length IS NULL AND busy_proof_media_type IS NULL AND busy_proof_json IS NULL
         AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR (state = 'not_started' AND raw_blob_object_id IS NULL AND raw_content_hash IS NULL
            AND raw_byte_length IS NULL AND raw_media_type IS NULL AND evidence_json IS NULL)
        OR (state NOT IN ('staged', 'not_started') AND raw_blob_object_id IS NULL AND raw_content_hash IS NULL
            AND raw_byte_length IS NULL AND raw_media_type IS NULL AND evidence_json IS NULL
            AND busy_proof_blob_object_id IS NULL AND busy_proof_content_hash IS NULL
            AND busy_proof_byte_length IS NULL AND busy_proof_media_type IS NULL AND busy_proof_json IS NULL)
    ),
    CHECK (
        (state = 'not_started' AND busy_proof_blob_object_id IS NOT NULL
         AND busy_proof_content_hash IS NOT NULL AND busy_proof_byte_length IS NOT NULL
         AND busy_proof_media_type IS NOT NULL AND busy_proof_json IS NOT NULL
         AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR state <> 'not_started'
    ),
    CHECK (
        (state = 'invoking' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state <> 'invoking' AND lease_token IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE INDEX runtime_shadow_local_measurement_member_recovery_idx
    ON runtime.shadow_local_calibration_measurement_members (attempt_id, state);

CREATE OR REPLACE FUNCTION runtime.guard_shadow_local_calibration_measurement_attempt()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    slot_name text;
    slot_hash text;
    slot_state text;
    profile text;
    predecessor runtime.shadow_local_calibration_measurement_attempts%ROWTYPE;
    predecessor_member_state text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shadow-local measurement attempts are durable and cannot be deleted';
    END IF;
    SELECT slot.command_name, slot.request_hash, slot.state, job.profile
      INTO slot_name, slot_hash, slot_state, profile
      FROM runtime.command_slots AS slot
      JOIN runtime.jobs AS job ON job.job_id = slot.job_id
     WHERE slot.command_slot_id = NEW.command_slot_id AND slot.job_id = NEW.job_id;
    IF NOT FOUND OR slot_name <> 'MeasureShadowLocalCalibrationCommand@1'
       OR slot_hash <> NEW.plan_hash OR slot_state <> 'running' OR profile <> 'shadow' THEN
        RAISE EXCEPTION 'shadow-local attempt requires the exact running shadow command slot';
    END IF;
    IF jsonb_typeof(NEW.plan_json) IS DISTINCT FROM 'object'
       OR NEW.plan_json->>'command' IS DISTINCT FROM 'MeasureShadowLocalCalibrationCommand@1'
       OR NEW.plan_json->>'measurement_protocol' IS DISTINCT FROM 'shadow-local-calibration-measurement-v1'
       OR jsonb_typeof(NEW.plan_json->'shadow_local_inputs') IS DISTINCT FROM 'object'
       OR jsonb_typeof(NEW.plan_json->'corpus_members') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'shadow-local plan must be the closed local measurement protocol shape';
    END IF;
    IF (
        SELECT array_agg(key ORDER BY key)
          FROM jsonb_object_keys(NEW.plan_json) AS object_keys(key)
    ) IS DISTINCT FROM ARRAY[
        'command', 'corpus_members', 'measurement_protocol', 'shadow_local_inputs'
    ]::text[] OR (
        SELECT array_agg(key ORDER BY key)
          FROM jsonb_object_keys(NEW.plan_json->'shadow_local_inputs') AS object_keys(key)
    ) IS DISTINCT FROM ARRAY[
        'limits', 'manifest', 'max_attempt_count', 'service_profile', 'source_bindings'
    ]::text[] OR jsonb_typeof(NEW.plan_json->'shadow_local_inputs'->'service_profile')
           IS DISTINCT FROM 'object'
       OR jsonb_typeof(NEW.plan_json->'shadow_local_inputs'->'manifest')
           IS DISTINCT FROM 'object'
       OR jsonb_typeof(NEW.plan_json->'shadow_local_inputs'->'limits')
           IS DISTINCT FROM 'object'
       OR jsonb_typeof(NEW.plan_json->'shadow_local_inputs'->'source_bindings')
           IS DISTINCT FROM 'array'
       OR jsonb_typeof(NEW.plan_json->'shadow_local_inputs'->'max_attempt_count')
           IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION 'shadow-local plan must have exact closed local input fields';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'prepared' OR NEW.version <> 0 OR NEW.completed_at IS NOT NULL
           OR NEW.recovery_lease_token IS NOT NULL OR NEW.recovery_lease_expires_at IS NOT NULL THEN
            RAISE EXCEPTION 'shadow-local attempt must begin as an unleased prepared attempt';
        END IF;
        IF NEW.attempt_ordinal > 1 THEN
            SELECT * INTO predecessor FROM runtime.shadow_local_calibration_measurement_attempts
             WHERE attempt_id = NEW.previous_attempt_id FOR KEY SHARE;
            IF NOT FOUND OR predecessor.command_slot_id <> NEW.command_slot_id
               OR predecessor.job_id <> NEW.job_id OR predecessor.plan_hash <> NEW.plan_hash
               OR predecessor.plan_json <> NEW.plan_json OR predecessor.state <> 'indeterminate'
               OR predecessor.attempt_ordinal <> NEW.attempt_ordinal - 1
               OR predecessor.version <> NEW.retry_predecessor_version THEN
                RAISE EXCEPTION 'shadow-local successor must preserve its exact indeterminate predecessor';
            END IF;
            SELECT state INTO predecessor_member_state
              FROM runtime.shadow_local_calibration_measurement_members
             WHERE attempt_id = predecessor.attempt_id
               AND case_sha256 = NEW.retry_member_case_sha256
             FOR KEY SHARE;
            IF NOT FOUND
               OR (NEW.retry_reason_code = 'NATIVE_OUTCOME_UNKNOWN'
                   AND predecessor_member_state <> 'indeterminate')
               OR (NEW.retry_reason_code = 'REQUEST_NOT_STARTED'
                   AND predecessor_member_state <> 'not_started') THEN
                RAISE EXCEPTION 'shadow-local successor retry authorization does not match predecessor member';
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.attempt_id, NEW.command_slot_id, NEW.job_id, NEW.plan_hash, NEW.attempt_ordinal,
        NEW.previous_attempt_id, NEW.plan_json, NEW.retry_decision_reference_sha256,
        NEW.retry_member_case_sha256, NEW.retry_predecessor_version, NEW.retry_reason_code)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.command_slot_id, OLD.job_id, OLD.plan_hash, OLD.attempt_ordinal,
        OLD.previous_attempt_id, OLD.plan_json, OLD.retry_decision_reference_sha256,
        OLD.retry_member_case_sha256, OLD.retry_predecessor_version, OLD.retry_reason_code) THEN
        RAISE EXCEPTION 'shadow-local attempt identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'shadow-local attempt transition requires exact version increment';
    END IF;
    IF NOT (
        (OLD.state = 'prepared' AND NEW.state IN ('prepared', 'collecting', 'indeterminate'))
        OR (OLD.state = 'collecting' AND NEW.state IN ('collecting', 'ready', 'indeterminate', 'denied'))
        OR (OLD.state = 'ready' AND NEW.state IN ('ready', 'committed'))
        OR (OLD.state = 'indeterminate' AND NEW.state = 'indeterminate')
    ) THEN
        RAISE EXCEPTION 'invalid shadow-local attempt state transition';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_shadow_local_calibration_measurement_attempt_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.shadow_local_calibration_measurement_attempts
FOR EACH ROW EXECUTE FUNCTION runtime.guard_shadow_local_calibration_measurement_attempt();

CREATE OR REPLACE FUNCTION runtime.guard_shadow_local_calibration_measurement_member()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    attempt_job uuid;
    attempt_number integer;
    predecessor_id uuid;
    predecessor_state text;
    predecessor_raw_id uuid;
    predecessor_raw_hash text;
    predecessor_raw_length bigint;
    predecessor_raw_media_type text;
    predecessor_evidence jsonb;
    authorized_retry_case text;
    claimed boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shadow-local measurement members are durable and cannot be deleted';
    END IF;
    SELECT attempt.job_id, attempt.attempt_ordinal, attempt.previous_attempt_id,
           attempt.retry_member_case_sha256
      INTO attempt_job, attempt_number, predecessor_id, authorized_retry_case
      FROM runtime.shadow_local_calibration_measurement_attempts AS attempt
     WHERE attempt.attempt_id = NEW.attempt_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'shadow-local measurement member has no attempt';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 0 OR NEW.lease_token IS NOT NULL OR NEW.lease_expires_at IS NOT NULL THEN
            RAISE EXCEPTION 'shadow-local measurement member must begin unleased';
        END IF;
        IF attempt_number = 1 THEN
            IF NEW.state <> 'pending' OR NEW.raw_blob_object_id IS NOT NULL
               OR NEW.busy_proof_blob_object_id IS NOT NULL THEN
                RAISE EXCEPTION 'first shadow-local attempt members must begin pending without evidence';
            END IF;
        ELSIF NOT EXISTS (
            SELECT 1 FROM runtime.shadow_local_calibration_measurement_members AS predecessor_member
             WHERE predecessor_member.attempt_id = predecessor_id
               AND (predecessor_member.case_sha256, predecessor_member.request_sha256,
                    predecessor_member.member_ordinal, predecessor_member.case_json,
                    predecessor_member.request_json, predecessor_member.source_job_id,
                    predecessor_member.source_blob_object_id, predecessor_member.source_blob_content_hash,
                    predecessor_member.source_blob_byte_length, predecessor_member.source_blob_media_type,
                    predecessor_member.source_blob_reference_sha256, predecessor_member.binding_sha256,
                    predecessor_member.service_profile_sha256, predecessor_member.max_response_bytes)
                   IS NOT DISTINCT FROM
                   (NEW.case_sha256, NEW.request_sha256, NEW.member_ordinal, NEW.case_json,
                    NEW.request_json, NEW.source_job_id, NEW.source_blob_object_id,
                    NEW.source_blob_content_hash, NEW.source_blob_byte_length,
                    NEW.source_blob_media_type, NEW.source_blob_reference_sha256,
                    NEW.binding_sha256, NEW.service_profile_sha256, NEW.max_response_bytes)
        ) THEN
            RAISE EXCEPTION 'shadow-local successor member must preserve complete immutable identity';
        ELSIF NEW.state = 'staged' THEN
            SELECT state, raw_blob_object_id, raw_content_hash, raw_byte_length, raw_media_type, evidence_json
              INTO predecessor_state, predecessor_raw_id, predecessor_raw_hash, predecessor_raw_length,
                   predecessor_raw_media_type, predecessor_evidence
              FROM runtime.shadow_local_calibration_measurement_members
             WHERE attempt_id = predecessor_id AND case_sha256 = NEW.case_sha256 FOR KEY SHARE;
            IF NOT FOUND OR predecessor_state <> 'staged'
               OR (NEW.raw_blob_object_id, NEW.raw_content_hash, NEW.raw_byte_length,
                   NEW.raw_media_type, NEW.evidence_json)
                  IS DISTINCT FROM
                  (predecessor_raw_id, predecessor_raw_hash, predecessor_raw_length,
                   predecessor_raw_media_type, predecessor_evidence) THEN
                RAISE EXCEPTION 'shadow-local successor staged member must inherit exact prior evidence';
            END IF;
        ELSIF NEW.state = 'pending' THEN
            SELECT state INTO predecessor_state
              FROM runtime.shadow_local_calibration_measurement_members
             WHERE attempt_id = predecessor_id AND case_sha256 = NEW.case_sha256 FOR KEY SHARE;
            IF NOT FOUND OR (
                predecessor_state <> 'pending'
                AND NOT (
                    NEW.case_sha256 = authorized_retry_case
                    AND predecessor_state IN ('indeterminate', 'not_started')
                )
            ) THEN
                RAISE EXCEPTION 'shadow-local successor pending member is not dispatchable from predecessor';
            END IF;
        ELSE
            RAISE EXCEPTION 'shadow-local successor members must inherit staged evidence or begin pending';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.attempt_id, NEW.case_sha256, NEW.request_sha256, NEW.member_ordinal,
        NEW.case_json, NEW.request_json, NEW.source_job_id, NEW.source_blob_object_id,
        NEW.source_blob_content_hash, NEW.source_blob_byte_length, NEW.source_blob_media_type,
        NEW.source_blob_reference_sha256, NEW.binding_sha256, NEW.service_profile_sha256,
        NEW.max_response_bytes)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.case_sha256, OLD.request_sha256, OLD.member_ordinal,
        OLD.case_json, OLD.request_json, OLD.source_job_id, OLD.source_blob_object_id,
        OLD.source_blob_content_hash, OLD.source_blob_byte_length, OLD.source_blob_media_type,
        OLD.source_blob_reference_sha256, OLD.binding_sha256, OLD.service_profile_sha256,
        OLD.max_response_bytes) THEN
        RAISE EXCEPTION 'shadow-local measurement member identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'shadow-local measurement member transition requires exact version increment';
    END IF;
    IF NOT (
        (OLD.state = 'pending' AND NEW.state = 'invoking')
        OR (OLD.state = 'invoking' AND NEW.state IN ('not_started', 'staged', 'indeterminate', 'rejected'))
    ) THEN
        RAISE EXCEPTION 'invalid shadow-local measurement member state transition';
    END IF;
    IF NEW.state = 'staged' THEN
        SELECT EXISTS (
            SELECT 1 FROM storage.blob_objects AS object
            JOIN storage.blob_claims AS claim ON claim.object_id = object.object_id
             WHERE object.object_id = NEW.raw_blob_object_id AND claim.job_id = attempt_job
               AND object.content_hash = NEW.raw_content_hash
               AND object.byte_length = NEW.raw_byte_length AND object.media_type = NEW.raw_media_type
        ) INTO claimed;
        IF NOT claimed THEN
            RAISE EXCEPTION 'staged shadow-local evidence must be exactly claimed by its shadow Job';
        END IF;
    ELSIF NEW.state = 'not_started' THEN
        SELECT EXISTS (
            SELECT 1 FROM storage.blob_objects AS object
            JOIN storage.blob_claims AS claim ON claim.object_id = object.object_id
             WHERE object.object_id = NEW.busy_proof_blob_object_id AND claim.job_id = attempt_job
               AND object.content_hash = NEW.busy_proof_content_hash
               AND object.byte_length = NEW.busy_proof_byte_length
               AND object.media_type = NEW.busy_proof_media_type
        ) INTO claimed;
        IF NOT claimed THEN
            RAISE EXCEPTION 'not-started shadow-local BUSY proof must be exactly claimed by its shadow Job';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_shadow_local_calibration_measurement_member_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.shadow_local_calibration_measurement_members
FOR EACH ROW EXECUTE FUNCTION runtime.guard_shadow_local_calibration_measurement_member();

CREATE OR REPLACE FUNCTION runtime.assert_shadow_local_calibration_measurement_member_set()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    checked_attempt uuid;
    expected_count integer;
    actual_count integer;
    max_ordinal integer;
BEGIN
    checked_attempt := COALESCE(NEW.attempt_id, OLD.attempt_id);
    SELECT jsonb_array_length(plan_json->'corpus_members') INTO expected_count
      FROM runtime.shadow_local_calibration_measurement_attempts WHERE attempt_id = checked_attempt;
    SELECT count(*), max(member_ordinal) INTO actual_count, max_ordinal
      FROM runtime.shadow_local_calibration_measurement_members WHERE attempt_id = checked_attempt;
    IF expected_count IS NULL OR actual_count <> expected_count
       OR actual_count = 0 OR max_ordinal <> actual_count - 1 THEN
        RAISE EXCEPTION 'shadow-local measurement attempt member set is incomplete or non-canonical';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_shadow_local_calibration_measurement_member_set_from_attempt
AFTER INSERT OR UPDATE ON runtime.shadow_local_calibration_measurement_attempts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_shadow_local_calibration_measurement_member_set();
CREATE CONSTRAINT TRIGGER runtime_shadow_local_calibration_measurement_member_set_from_member
AFTER INSERT OR UPDATE OR DELETE ON runtime.shadow_local_calibration_measurement_members
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_shadow_local_calibration_measurement_member_set();

COMMIT;
