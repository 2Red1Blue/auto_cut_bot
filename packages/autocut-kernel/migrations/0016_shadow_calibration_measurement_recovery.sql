-- Dedicated recovery aggregate for MeasureShadowCalibrationCommand@2.1.3.
--
-- This deliberately does not add a lease/reclaim state to command_slots: a
-- local native result whose process response was lost is evidence-unknown,
-- not safely replayable by a generic command owner.

BEGIN;

CREATE TABLE runtime.shadow_calibration_measurement_attempts (
    attempt_id uuid PRIMARY KEY,
    command_slot_id uuid NOT NULL UNIQUE REFERENCES runtime.command_slots (command_slot_id),
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    plan_hash text NOT NULL CHECK (plan_hash ~ '^sha256:[0-9a-f]{64}$'),
    attempt_ordinal integer NOT NULL CHECK (attempt_ordinal >= 1),
    previous_attempt_id uuid UNIQUE REFERENCES runtime.shadow_calibration_measurement_attempts (attempt_id),
    state text NOT NULL CHECK (state IN ('prepared', 'collecting', 'ready', 'indeterminate', 'committed')),
    version bigint NOT NULL CHECK (version >= 0),
    recovery_lease_token text,
    recovery_lease_expires_at timestamptz,
    plan_json jsonb NOT NULL,
    retry_decision_reference_sha256 text CHECK (
        retry_decision_reference_sha256 IS NULL
        OR retry_decision_reference_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    retry_reason_code text CHECK (
        retry_reason_code IS NULL OR retry_reason_code = 'NATIVE_OUTCOME_UNKNOWN'
    ),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    completed_at timestamptz,
    UNIQUE (job_id, plan_hash, attempt_ordinal),
    CHECK ((recovery_lease_token IS NULL) = (recovery_lease_expires_at IS NULL)),
    CHECK (
        (attempt_ordinal = 1 AND previous_attempt_id IS NULL
         AND retry_decision_reference_sha256 IS NULL AND retry_reason_code IS NULL)
        OR (attempt_ordinal > 1 AND previous_attempt_id IS NOT NULL
            AND retry_decision_reference_sha256 IS NOT NULL
            AND retry_reason_code = 'NATIVE_OUTCOME_UNKNOWN')
    ),
    CHECK ((state = 'committed') = (completed_at IS NOT NULL))
);

CREATE TABLE runtime.shadow_calibration_measurement_members (
    attempt_id uuid NOT NULL REFERENCES runtime.shadow_calibration_measurement_attempts (attempt_id),
    corpus_member_reference_sha256 text NOT NULL CHECK (
        corpus_member_reference_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    member_ordinal integer NOT NULL CHECK (member_ordinal >= 0),
    expected_anchor_reference_sha256 text NOT NULL CHECK (
        expected_anchor_reference_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    invocation_json jsonb NOT NULL,
    context_json jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'invoking', 'staged', 'indeterminate')),
    version bigint NOT NULL CHECK (version >= 0),
    lease_token text,
    lease_expires_at timestamptz,
    raw_blob_object_id uuid REFERENCES storage.blob_objects (object_id),
    raw_content_hash text CHECK (raw_content_hash IS NULL OR raw_content_hash ~ '^sha256:[0-9a-f]{64}$'),
    raw_byte_length bigint CHECK (raw_byte_length IS NULL OR raw_byte_length >= 0),
    raw_media_type text,
    projection_json jsonb,
    PRIMARY KEY (attempt_id, corpus_member_reference_sha256),
    UNIQUE (attempt_id, member_ordinal),
    CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL)),
    CHECK (
        (state = 'staged' AND raw_blob_object_id IS NOT NULL AND raw_content_hash IS NOT NULL
         AND raw_byte_length IS NOT NULL AND raw_media_type IS NOT NULL AND projection_json IS NOT NULL
         AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR (state <> 'staged' AND raw_blob_object_id IS NULL AND raw_content_hash IS NULL
            AND raw_byte_length IS NULL AND raw_media_type IS NULL AND projection_json IS NULL)
    ),
    CHECK (
        (state = 'invoking' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state <> 'invoking' AND lease_token IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE OR REPLACE FUNCTION runtime.guard_shadow_calibration_measurement_attempt()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    slot_name text;
    slot_hash text;
    slot_state text;
    profile text;
    predecessor runtime.shadow_calibration_measurement_attempts%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shadow measurement attempts are durable and cannot be deleted';
    END IF;
    SELECT slot.command_name, slot.request_hash, slot.state, job.profile
      INTO slot_name, slot_hash, slot_state, profile
      FROM runtime.command_slots AS slot
      JOIN runtime.jobs AS job ON job.job_id = slot.job_id
     WHERE slot.command_slot_id = NEW.command_slot_id AND slot.job_id = NEW.job_id;
    IF NOT FOUND OR slot_name <> 'MeasureShadowCalibrationCommand@2.1.3'
       OR slot_hash <> NEW.plan_hash OR slot_state <> 'running' OR profile <> 'shadow' THEN
        RAISE EXCEPTION 'shadow measurement attempt requires the exact running shadow command slot';
    END IF;
    IF jsonb_typeof(NEW.plan_json) <> 'object'
       OR NEW.plan_json->>'command' <> 'MeasureShadowCalibrationCommand@2.1.3'
       OR NEW.plan_json->>'measurement_protocol' <> 'shadow-calibration-measurement-v1'
       OR NOT (NEW.plan_json ? 'shadow_inputs')
       OR jsonb_typeof(NEW.plan_json->'corpus_members') <> 'array' THEN
        RAISE EXCEPTION 'shadow measurement plan must be the closed measurement protocol shape';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'prepared' OR NEW.version <> 0 OR NEW.completed_at IS NOT NULL
           OR NEW.recovery_lease_token IS NOT NULL OR NEW.recovery_lease_expires_at IS NOT NULL THEN
            RAISE EXCEPTION 'shadow measurement attempt must begin as an unleased prepared attempt';
        END IF;
        IF NEW.attempt_ordinal > 1 THEN
            SELECT * INTO predecessor FROM runtime.shadow_calibration_measurement_attempts
             WHERE attempt_id = NEW.previous_attempt_id FOR KEY SHARE;
            IF NOT FOUND OR predecessor.job_id <> NEW.job_id OR predecessor.plan_hash <> NEW.plan_hash
               OR predecessor.plan_json <> NEW.plan_json OR predecessor.state <> 'indeterminate'
               OR predecessor.attempt_ordinal <> NEW.attempt_ordinal - 1 THEN
                RAISE EXCEPTION 'shadow successor must preserve its immediately prior indeterminate plan';
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.attempt_id, NEW.command_slot_id, NEW.job_id, NEW.plan_hash, NEW.attempt_ordinal,
        NEW.previous_attempt_id, NEW.plan_json, NEW.retry_decision_reference_sha256, NEW.retry_reason_code)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.command_slot_id, OLD.job_id, OLD.plan_hash, OLD.attempt_ordinal,
        OLD.previous_attempt_id, OLD.plan_json, OLD.retry_decision_reference_sha256, OLD.retry_reason_code) THEN
        RAISE EXCEPTION 'shadow measurement attempt identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'shadow measurement attempt transition requires exact version increment';
    END IF;
    IF NOT (
        (OLD.state = 'prepared' AND NEW.state IN ('prepared', 'collecting', 'indeterminate'))
        OR (OLD.state = 'collecting' AND NEW.state IN ('collecting', 'ready', 'indeterminate'))
        OR (OLD.state = 'ready' AND NEW.state IN ('ready', 'committed'))
        OR (OLD.state = 'indeterminate' AND NEW.state = 'indeterminate')
    ) THEN
        RAISE EXCEPTION 'invalid shadow measurement attempt state transition';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_shadow_calibration_measurement_attempt_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.shadow_calibration_measurement_attempts
FOR EACH ROW EXECUTE FUNCTION runtime.guard_shadow_calibration_measurement_attempt();

CREATE OR REPLACE FUNCTION runtime.guard_shadow_calibration_measurement_member()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    attempt_job uuid;
    claimed boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shadow measurement members are durable and cannot be deleted';
    END IF;
    SELECT job_id INTO attempt_job FROM runtime.shadow_calibration_measurement_attempts
     WHERE attempt_id = NEW.attempt_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'shadow measurement member has no attempt';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'pending' OR NEW.version <> 0 OR NEW.lease_token IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL OR NEW.raw_blob_object_id IS NOT NULL THEN
            RAISE EXCEPTION 'shadow measurement member must begin pending without evidence';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.attempt_id, NEW.corpus_member_reference_sha256, NEW.member_ordinal,
        NEW.expected_anchor_reference_sha256, NEW.invocation_json, NEW.context_json)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.corpus_member_reference_sha256, OLD.member_ordinal,
        OLD.expected_anchor_reference_sha256, OLD.invocation_json, OLD.context_json) THEN
        RAISE EXCEPTION 'shadow measurement member identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'shadow measurement member transition requires exact version increment';
    END IF;
    IF NOT (
        (OLD.state = 'pending' AND NEW.state = 'invoking')
        OR (OLD.state = 'invoking' AND NEW.state IN ('staged', 'indeterminate'))
    ) THEN
        RAISE EXCEPTION 'invalid shadow measurement member state transition';
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
            RAISE EXCEPTION 'staged shadow evidence must be exactly claimed by its shadow Job';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_shadow_calibration_measurement_member_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.shadow_calibration_measurement_members
FOR EACH ROW EXECUTE FUNCTION runtime.guard_shadow_calibration_measurement_member();

CREATE OR REPLACE FUNCTION runtime.assert_shadow_calibration_measurement_member_set()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    checked_attempt uuid;
    expected_count integer;
    actual_count integer;
    max_ordinal integer;
BEGIN
    checked_attempt := COALESCE(NEW.attempt_id, OLD.attempt_id);
    SELECT jsonb_array_length(plan_json->'corpus_members') INTO expected_count
      FROM runtime.shadow_calibration_measurement_attempts WHERE attempt_id = checked_attempt;
    SELECT count(*), max(member_ordinal) INTO actual_count, max_ordinal
      FROM runtime.shadow_calibration_measurement_members WHERE attempt_id = checked_attempt;
    IF expected_count IS NULL OR actual_count <> expected_count
       OR actual_count = 0 OR max_ordinal <> actual_count - 1 THEN
        RAISE EXCEPTION 'shadow measurement attempt member set is incomplete or non-canonical';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_shadow_calibration_measurement_member_set_from_attempt
AFTER INSERT OR UPDATE ON runtime.shadow_calibration_measurement_attempts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_shadow_calibration_measurement_member_set();
CREATE CONSTRAINT TRIGGER runtime_shadow_calibration_measurement_member_set_from_member
AFTER INSERT OR UPDATE OR DELETE ON runtime.shadow_calibration_measurement_members
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime.assert_shadow_calibration_measurement_member_set();

COMMIT;
