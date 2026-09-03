-- Add immutable metadata for large S3-compatible objects without rewriting
-- existing bounded PostgreSQL bytea evidence.

BEGIN;

ALTER TABLE storage.blob_objects
    ALTER COLUMN content_bytes DROP NOT NULL,
    ADD COLUMN storage_kind text NOT NULL DEFAULT 'postgres_inline'
        CHECK (storage_kind IN ('postgres_inline', 's3_compatible')),
    ADD COLUMN storage_backend_id text,
    ADD COLUMN storage_region text,
    ADD COLUMN storage_locator text,
    ADD COLUMN storage_etag text,
    ADD COLUMN storage_version_id text,
    ADD COLUMN write_strategy text,
    ADD COLUMN verified_at timestamptz;

ALTER TABLE storage.blob_objects
    ADD CONSTRAINT blob_objects_storage_shape CHECK (
        (
            storage_kind = 'postgres_inline'
            AND content_bytes IS NOT NULL
            AND storage_backend_id IS NULL
            AND storage_region IS NULL
            AND storage_locator IS NULL
            AND storage_etag IS NULL
            AND storage_version_id IS NULL
            AND write_strategy IS NULL
            AND verified_at IS NULL
        )
        OR
        (
            storage_kind = 's3_compatible'
            AND content_bytes IS NULL
            AND byte_length > 0
            AND storage_backend_id ~ '^[a-z][a-z0-9._-]{0,63}$'
            AND storage_region ~ '^[a-z][a-z0-9._-]{0,63}$'
            AND length(storage_locator) BETWEEN 1 AND 1024
            AND left(storage_locator, 1) <> '/'
            AND storage_locator !~ '(^|/)\.\.(/|$)'
            AND length(btrim(storage_etag)) BETWEEN 1 AND 512
            AND (
                storage_version_id IS NULL
                OR length(btrim(storage_version_id)) BETWEEN 1 AND 512
            )
            AND write_strategy = 's3-single-put-v1'
            AND verified_at IS NOT NULL
        )
    );

CREATE UNIQUE INDEX storage_blob_objects_external_locator_unique
    ON storage.blob_objects (
        storage_backend_id,
        storage_region,
        storage_locator
    )
    WHERE storage_kind = 's3_compatible';

CREATE TABLE storage.object_write_intents (
    object_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    byte_length bigint NOT NULL CHECK (byte_length > 0),
    media_type text NOT NULL CHECK (length(btrim(media_type)) > 0),
    storage_backend_id text NOT NULL CHECK (
        storage_backend_id ~ '^[a-z][a-z0-9._-]{0,63}$'
    ),
    storage_region text NOT NULL CHECK (
        storage_region ~ '^[a-z][a-z0-9._-]{0,63}$'
    ),
    storage_locator text NOT NULL CHECK (
        length(storage_locator) BETWEEN 1 AND 1024
        AND left(storage_locator, 1) <> '/'
        AND storage_locator !~ '(^|/)\.\.(/|$)'
    ),
    write_strategy text NOT NULL CHECK (write_strategy = 's3-single-put-v1'),
    reservation_token uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('reserved', 'resolved')),
    version bigint NOT NULL CHECK (version >= 0),
    resolved_object_id uuid REFERENCES storage.blob_objects (object_id),
    reserved_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    resolved_at timestamptz,
    UNIQUE (storage_backend_id, storage_region, storage_locator),
    CHECK (
        (state = 'reserved' AND version = 0
            AND resolved_object_id IS NULL AND resolved_at IS NULL)
        OR
        (state = 'resolved' AND version = 1
            AND resolved_object_id IS NOT NULL AND resolved_at IS NOT NULL)
    )
);

CREATE OR REPLACE FUNCTION storage.prevent_object_write_intent_rewrite()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'reserved'
           OR NEW.version <> 0
           OR NEW.resolved_object_id IS NOT NULL
           OR NEW.resolved_at IS NOT NULL THEN
            RAISE EXCEPTION 'object write intents must begin reserved at version zero';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'object write intents cannot be deleted';
    END IF;
    IF OLD.state = 'resolved' THEN
        RAISE EXCEPTION 'resolved object write intents are immutable';
    END IF;
    IF (NEW.object_id, NEW.job_id, NEW.content_hash, NEW.byte_length,
        NEW.media_type, NEW.storage_backend_id, NEW.storage_region,
        NEW.storage_locator, NEW.write_strategy, NEW.reservation_token,
        NEW.reserved_at)
       IS DISTINCT FROM
       (OLD.object_id, OLD.job_id, OLD.content_hash, OLD.byte_length,
        OLD.media_type, OLD.storage_backend_id, OLD.storage_region,
        OLD.storage_locator, OLD.write_strategy, OLD.reservation_token,
        OLD.reserved_at)
       OR NEW.state <> 'resolved'
       OR NEW.version <> OLD.version + 1
       OR NEW.resolved_object_id IS NULL
       OR NEW.resolved_at IS NULL THEN
        RAISE EXCEPTION 'object write intent transition is invalid';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER storage_object_write_intent_no_rewrite
BEFORE INSERT OR UPDATE OR DELETE ON storage.object_write_intents
FOR EACH ROW EXECUTE FUNCTION storage.prevent_object_write_intent_rewrite();

COMMENT ON COLUMN storage.blob_objects.storage_locator IS
    'Opaque backend-relative locator. Never exposed through Kernel BlobRef or public DTOs.';
COMMENT ON COLUMN storage.blob_objects.verified_at IS
    'Database time at which exact remote HEAD metadata was accepted; not publication authority.';
COMMENT ON TABLE storage.object_write_intents IS
    'Durable pre-write expectations and one-way CAS resolution for external objects.';

COMMIT;
