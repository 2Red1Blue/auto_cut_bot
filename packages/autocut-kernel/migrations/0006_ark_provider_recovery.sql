-- Close Ark Files/Responses crash windows without importing legacy provider state.

BEGIN;

ALTER TABLE runtime.provider_media_objects
    ADD COLUMN provider_scope_fingerprint text,
    ADD COLUMN lease_token text,
    ADD COLUMN lease_expires_at timestamptz,
    ADD COLUMN audit_expires_at timestamptz;

ALTER TABLE runtime.provider_media_objects
    DISABLE TRIGGER runtime_provider_media_object_transition_guard;

-- Existing rows deliberately receive an unreachable legacy scope. They remain
-- audit evidence but can never be reused by a newly configured Ark tenant.
UPDATE runtime.provider_media_objects
   SET provider_scope_fingerprint = 'sha256:' || repeat('0', 64);
UPDATE runtime.provider_media_objects
   SET lease_token = 'migration-unowned-' || media_object_id::text,
       lease_expires_at = transaction_timestamp()
 WHERE state IN ('reserved', 'processing');
UPDATE runtime.provider_media_objects
   SET audit_expires_at = COALESCE(completed_at, reserved_at) + interval '30 days'
 WHERE state = 'indeterminate';

ALTER TABLE runtime.provider_media_objects
    ALTER COLUMN provider_scope_fingerprint SET NOT NULL;

DO $$
DECLARE scoped_constraint record;
BEGIN
    FOR scoped_constraint IN
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'runtime.provider_media_objects'::regclass
           AND contype = 'u'
           AND (
               pg_get_constraintdef(oid) LIKE '%provider_id, content_hash, preprocess_policy_hash, generation%'
               OR pg_get_constraintdef(oid) LIKE '%provider_id, provider_file_id%'
           )
    LOOP
        EXECUTE format(
            'ALTER TABLE runtime.provider_media_objects DROP CONSTRAINT %I',
            scoped_constraint.conname
        );
    END LOOP;
END $$;

ALTER TABLE runtime.provider_media_objects
    ADD CONSTRAINT provider_media_scoped_generation_unique UNIQUE
        (provider_id, provider_scope_fingerprint, content_hash,
         preprocess_policy_hash, generation),
    ADD CONSTRAINT provider_media_scoped_file_id_unique UNIQUE
        (provider_id, provider_scope_fingerprint, provider_file_id);

-- Replace the v0004 checks as one closed set because audit expiry permits an
-- unknown-file quarantine (no file_id) to become expired after retention.
DO $$
DECLARE checked_constraint record;
BEGIN
    FOR checked_constraint IN
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'runtime.provider_media_objects'::regclass
           AND contype = 'c'
    LOOP
        EXECUTE format(
            'ALTER TABLE runtime.provider_media_objects DROP CONSTRAINT %I',
            checked_constraint.conname
        );
    END LOOP;
END $$;

ALTER TABLE runtime.provider_media_objects
    ADD CONSTRAINT provider_media_provider_id_nonempty
        CHECK (length(btrim(provider_id)) > 0),
    ADD CONSTRAINT provider_media_scope_sha256
        CHECK (provider_scope_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    ADD CONSTRAINT provider_media_content_sha256
        CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    ADD CONSTRAINT provider_media_byte_length_positive CHECK (byte_length > 0),
    ADD CONSTRAINT provider_media_type_nonempty CHECK (length(btrim(media_type)) > 0),
    ADD CONSTRAINT provider_media_policy_sha256
        CHECK (preprocess_policy_hash ~ '^sha256:[0-9a-f]{64}$'),
    ADD CONSTRAINT provider_media_generation_positive CHECK (generation >= 1),
    ADD CONSTRAINT provider_media_state_closed CHECK (
        state IN ('reserved', 'processing', 'available', 'indeterminate', 'failed', 'expired')
    ),
    ADD CONSTRAINT provider_media_version_nonnegative CHECK (version >= 0),
    ADD CONSTRAINT provider_media_file_id_nonempty CHECK (
        provider_file_id IS NULL OR length(btrim(provider_file_id)) > 0
    ),
    ADD CONSTRAINT provider_media_status_nonempty CHECK (
        provider_status IS NULL OR length(btrim(provider_status)) > 0
    ),
    ADD CONSTRAINT provider_media_failure_nonempty CHECK (
        failure_code IS NULL OR length(btrim(failure_code)) > 0
    ),
    ADD CONSTRAINT provider_media_lease_token_nonempty CHECK (
        lease_token IS NULL OR length(btrim(lease_token)) > 0
    ),
    ADD CONSTRAINT provider_media_file_binding CHECK (
        (state IN ('processing', 'available') AND provider_file_id IS NOT NULL)
        OR state IN ('reserved', 'indeterminate', 'failed', 'expired')
    ),
    ADD CONSTRAINT provider_media_active_lease CHECK (
        (state IN ('reserved', 'processing')
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
            AND completed_at IS NULL AND audit_expires_at IS NULL)
        OR (state NOT IN ('reserved', 'processing')
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    ADD CONSTRAINT provider_media_available_ttl CHECK (
        (state = 'available' AND available_at IS NOT NULL AND expires_at IS NOT NULL
                             AND expires_at > available_at)
        OR state <> 'available'
    ),
    ADD CONSTRAINT provider_media_failed_terminal CHECK (
        (state = 'failed' AND failure_code IS NOT NULL AND completed_at IS NOT NULL)
        OR (state <> 'failed' AND failure_code IS NULL)
    ),
    ADD CONSTRAINT provider_media_audit_terminal CHECK (
        (state = 'indeterminate' AND completed_at IS NOT NULL
                                  AND audit_expires_at IS NOT NULL)
        OR (state <> 'indeterminate' AND audit_expires_at IS NULL)
    ),
    ADD CONSTRAINT provider_media_expired_terminal CHECK (
        (state = 'expired' AND completed_at IS NOT NULL) OR state <> 'expired'
    );

DROP INDEX runtime.runtime_one_live_provider_media_identity;
CREATE UNIQUE INDEX runtime_one_live_provider_media_identity
    ON runtime.provider_media_objects
       (provider_id, provider_scope_fingerprint, content_hash, preprocess_policy_hash)
    WHERE state <> 'expired';

CREATE OR REPLACE FUNCTION runtime.guard_provider_media_object_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'provider media objects are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'reserved' OR NEW.version <> 0
           OR NEW.provider_file_id IS NOT NULL OR NEW.provider_status IS NOT NULL
           OR NEW.failure_code IS NOT NULL OR NEW.uploaded_at IS NOT NULL
           OR NEW.available_at IS NOT NULL OR NEW.expires_at IS NOT NULL
           OR NEW.completed_at IS NOT NULL OR NEW.audit_expires_at IS NOT NULL
           OR NEW.lease_token IS NULL OR NEW.lease_expires_at IS NULL
           OR NEW.lease_expires_at <= NEW.reserved_at THEN
            RAISE EXCEPTION 'provider media objects must begin as a leased clean reservation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.media_object_id, NEW.provider_id, NEW.provider_scope_fingerprint,
        NEW.content_hash, NEW.byte_length, NEW.media_type,
        NEW.preprocess_policy_hash, NEW.generation, NEW.reserved_at)
       IS DISTINCT FROM
       (OLD.media_object_id, OLD.provider_id, OLD.provider_scope_fingerprint,
        OLD.content_hash, OLD.byte_length, OLD.media_type,
        OLD.preprocess_policy_hash, OLD.generation, OLD.reserved_at) THEN
        RAISE EXCEPTION 'provider media object identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'provider media transition requires exact version increment';
    END IF;
    IF OLD.provider_file_id IS NOT NULL
       AND NEW.provider_file_id IS DISTINCT FROM OLD.provider_file_id THEN
        RAISE EXCEPTION 'provider file identity is immutable once known';
    END IF;
    IF OLD.state = 'processing' AND NEW.state = 'processing' THEN
        IF (NEW.provider_file_id, NEW.failure_code, NEW.uploaded_at,
            NEW.available_at, NEW.expires_at, NEW.completed_at, NEW.audit_expires_at)
           IS DISTINCT FROM
           (OLD.provider_file_id, OLD.failure_code, OLD.uploaded_at,
            OLD.available_at, OLD.expires_at, OLD.completed_at, OLD.audit_expires_at)
           OR (NEW.lease_token, NEW.lease_expires_at)
              IS NOT DISTINCT FROM (OLD.lease_token, OLD.lease_expires_at) THEN
            RAISE EXCEPTION 'processing recovery may only rotate or release its lease';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT (
        (OLD.state = 'reserved' AND NEW.state IN ('processing', 'indeterminate', 'failed'))
        OR (OLD.state = 'processing' AND NEW.state IN ('available', 'failed'))
        OR (OLD.state = 'available' AND NEW.state = 'expired')
        OR (OLD.state = 'indeterminate' AND NEW.state = 'expired')
    ) THEN
        RAISE EXCEPTION 'invalid provider media object state transition';
    END IF;
    RETURN NEW;
END $$;

ALTER TABLE runtime.provider_media_objects
    ENABLE TRIGGER runtime_provider_media_object_transition_guard;

CREATE OR REPLACE FUNCTION runtime.guard_generation_attempt_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'generation attempts are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'reserved' OR NEW.version <> 0
           OR NEW.provider_request_id IS NOT NULL OR NEW.raw_response_object_id IS NOT NULL
           OR NEW.receipt_id IS NOT NULL OR NEW.artifact_set_id IS NOT NULL THEN
            RAISE EXCEPTION 'generation attempts must begin as a clean reservation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.attempt_id, NEW.job_id, NEW.command_slot_id, NEW.request_hash,
        NEW.provider_id, NEW.provider_idempotency_key,
        NEW.request_payload_object_id, NEW.reserved_at)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.job_id, OLD.command_slot_id, OLD.request_hash,
        OLD.provider_id, OLD.provider_idempotency_key,
        OLD.request_payload_object_id, OLD.reserved_at) THEN
        RAISE EXCEPTION 'generation attempt identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'generation attempt transition requires exact version increment';
    END IF;
    IF OLD.provider_request_id IS NOT NULL
       AND NEW.provider_request_id IS DISTINCT FROM OLD.provider_request_id THEN
        RAISE EXCEPTION 'generation provider request identity is immutable once known';
    END IF;
    IF OLD.raw_response_object_id IS NOT NULL
       AND NEW.raw_response_object_id IS DISTINCT FROM OLD.raw_response_object_id THEN
        RAISE EXCEPTION 'generation raw-response identity is immutable once known';
    END IF;
    IF OLD.state = 'dispatched' AND NEW.state = 'dispatched' THEN
        IF OLD.provider_request_id IS NOT NULL OR NEW.provider_request_id IS NULL
           OR (NEW.raw_response_object_id, NEW.receipt_id, NEW.artifact_set_id,
               NEW.failure_code, NEW.failure_detail, NEW.dispatched_at,
               NEW.responded_at, NEW.completed_at)
              IS DISTINCT FROM
              (OLD.raw_response_object_id, OLD.receipt_id, OLD.artifact_set_id,
               OLD.failure_code, OLD.failure_detail, OLD.dispatched_at,
               OLD.responded_at, OLD.completed_at) THEN
            RAISE EXCEPTION 'dispatched request-id CAS may only bind the first provider identity';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT (
        (OLD.state = 'reserved' AND NEW.state IN ('dispatched', 'failed'))
        OR (OLD.state = 'dispatched' AND NEW.state IN ('responded', 'indeterminate', 'failed'))
        OR (OLD.state = 'responded' AND NEW.state IN ('committed', 'failed'))
        OR (OLD.state = 'indeterminate' AND NEW.state IN ('reconciled', 'failed'))
        OR (OLD.state = 'reconciled' AND NEW.state IN ('committed', 'failed'))
    ) THEN
        RAISE EXCEPTION 'invalid generation attempt state transition';
    END IF;
    RETURN NEW;
END $$;

COMMIT;
