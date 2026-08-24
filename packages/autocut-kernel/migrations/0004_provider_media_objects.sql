-- Durable provider-side media identities used by VLM adapters.
-- A provider file_id is never treated as globally valid: it is bound to the
-- provider, exact proxy bytes and exact preprocessing policy.

BEGIN;

CREATE TABLE runtime.provider_media_objects (
    media_object_id uuid PRIMARY KEY,
    provider_id text NOT NULL CHECK (length(btrim(provider_id)) > 0),
    content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    byte_length bigint NOT NULL CHECK (byte_length > 0),
    media_type text NOT NULL CHECK (length(btrim(media_type)) > 0),
    preprocess_policy_hash text NOT NULL CHECK (
        preprocess_policy_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    generation bigint NOT NULL CHECK (generation >= 1),
    state text NOT NULL CHECK (
        state IN ('reserved', 'processing', 'available', 'indeterminate', 'failed', 'expired')
    ),
    version bigint NOT NULL CHECK (version >= 0),
    provider_file_id text CHECK (
        provider_file_id IS NULL OR length(btrim(provider_file_id)) > 0
    ),
    provider_status text CHECK (
        provider_status IS NULL OR length(btrim(provider_status)) > 0
    ),
    failure_code text CHECK (
        failure_code IS NULL OR length(btrim(failure_code)) > 0
    ),
    reserved_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    uploaded_at timestamptz,
    available_at timestamptz,
    expires_at timestamptz,
    completed_at timestamptz,
    UNIQUE (provider_id, content_hash, preprocess_policy_hash, generation),
    UNIQUE (provider_id, provider_file_id),
    CHECK (
        (state IN ('processing', 'available', 'expired') AND provider_file_id IS NOT NULL)
        OR state IN ('reserved', 'indeterminate', 'failed')
    ),
    CHECK (
        (state = 'available' AND available_at IS NOT NULL AND expires_at IS NOT NULL
                             AND expires_at > available_at)
        OR state <> 'available'
    ),
    CHECK (
        (state = 'failed' AND failure_code IS NOT NULL AND completed_at IS NOT NULL)
        OR (state <> 'failed' AND failure_code IS NULL)
    ),
    CHECK (
        (state IN ('indeterminate', 'expired') AND completed_at IS NOT NULL)
        OR state NOT IN ('indeterminate', 'expired')
    )
);

-- An expired generation remains immutable audit evidence, while a later claim
-- may create the next generation. Every other state blocks duplicate upload.
CREATE UNIQUE INDEX runtime_one_live_provider_media_identity
    ON runtime.provider_media_objects
       (provider_id, content_hash, preprocess_policy_hash)
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
           OR NEW.completed_at IS NOT NULL THEN
            RAISE EXCEPTION 'provider media objects must begin as a clean reservation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.media_object_id, NEW.provider_id, NEW.content_hash, NEW.byte_length,
        NEW.media_type, NEW.preprocess_policy_hash, NEW.generation, NEW.reserved_at)
       IS DISTINCT FROM
       (OLD.media_object_id, OLD.provider_id, OLD.content_hash, OLD.byte_length,
        OLD.media_type, OLD.preprocess_policy_hash, OLD.generation, OLD.reserved_at) THEN
        RAISE EXCEPTION 'provider media object identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'provider media transition requires exact version increment';
    END IF;
    IF OLD.provider_file_id IS NOT NULL
       AND NEW.provider_file_id IS DISTINCT FROM OLD.provider_file_id THEN
        RAISE EXCEPTION 'provider file identity is immutable once known';
    END IF;
    IF NOT (
        (OLD.state = 'reserved' AND NEW.state IN ('processing', 'indeterminate', 'failed'))
        OR (OLD.state = 'processing' AND NEW.state IN ('available', 'indeterminate', 'failed'))
        OR (OLD.state = 'available' AND NEW.state = 'expired')
    ) THEN
        RAISE EXCEPTION 'invalid provider media object state transition';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_provider_media_object_transition_guard
BEFORE INSERT OR UPDATE OR DELETE ON runtime.provider_media_objects
FOR EACH ROW EXECUTE FUNCTION runtime.guard_provider_media_object_transition();

COMMIT;
