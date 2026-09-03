-- Persist the exact path-free facts produced by one fenced production render.
-- renderer_identity_sha256 is the canonical hash of the facts.ffmpeg mapping.

BEGIN;

-- Pre-0056 attempts cannot be assigned truthful execution-limit or render-facts
-- identities retroactively. v2.1.3 has not produced releasable persistent
-- attempts, so require an empty private journal instead of manufacturing facts.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM runtime.production_render_attempts LIMIT 1) THEN
        RAISE EXCEPTION
            '0056 requires an empty production render attempt journal; pre-facts attempts must be quarantined or reset';
    END IF;
END $$;

ALTER TABLE runtime.production_render_attempts
    ADD COLUMN execution_limits_sha256 text NOT NULL CHECK (
        execution_limits_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    ADD COLUMN render_facts_json text,
    ADD COLUMN render_facts_sha256 text CHECK (
        render_facts_sha256 IS NULL
        OR render_facts_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT production_render_attempt_facts_shape CHECK (
        (output_object_id IS NULL
            AND render_facts_json IS NULL
            AND render_facts_sha256 IS NULL)
        OR
        (output_object_id IS NOT NULL
            AND render_facts_json IS NOT NULL
            AND render_facts_sha256 IS NOT NULL)
    );

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
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid production render attempt state transition';
END $$;

-- Match Python json.dumps(..., ensure_ascii=True, separators=(',', ':'),
-- sort_keys=True) without trusting caller-provided lexical JSON.  jsonb alone
-- is insufficient because its text representation uses a different key order
-- and whitespace convention.
CREATE OR REPLACE FUNCTION runtime.json_ascii_quote(value text)
RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
    result text := '"';
    character_value text;
    codepoint integer;
    adjusted integer;
    high_surrogate integer;
    low_surrogate integer;
BEGIN
    FOR character_index IN 1..char_length(value) LOOP
        character_value := substr(value, character_index, 1);
        codepoint := ascii(character_value);
        IF character_value = '"' THEN
            result := result || chr(92) || '"';
        ELSIF character_value = chr(92) THEN
            result := result || chr(92) || chr(92);
        ELSIF codepoint = 8 THEN
            result := result || chr(92) || 'b';
        ELSIF codepoint = 9 THEN
            result := result || chr(92) || 't';
        ELSIF codepoint = 10 THEN
            result := result || chr(92) || 'n';
        ELSIF codepoint = 12 THEN
            result := result || chr(92) || 'f';
        ELSIF codepoint = 13 THEN
            result := result || chr(92) || 'r';
        ELSIF codepoint < 32 OR codepoint > 126 THEN
            IF codepoint <= 65535 THEN
                result := result || chr(92) || 'u'
                    || lpad(to_hex(codepoint), 4, '0');
            ELSE
                adjusted := codepoint - 65536;
                high_surrogate := 55296 + adjusted / 1024;
                low_surrogate := 56320 + adjusted % 1024;
                result := result || chr(92) || 'u'
                    || lpad(to_hex(high_surrogate), 4, '0')
                    || chr(92) || 'u'
                    || lpad(to_hex(low_surrogate), 4, '0');
            END IF;
        ELSE
            result := result || character_value;
        END IF;
    END LOOP;
    RETURN result || '"';
END $$;

CREATE OR REPLACE FUNCTION runtime.canonical_json_ascii(document jsonb)
RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
    document_type text := jsonb_typeof(document);
    result text;
BEGIN
    IF document_type = 'object' THEN
        SELECT '{' || COALESCE(
            string_agg(
                runtime.json_ascii_quote(member.key) || ':'
                    || runtime.canonical_json_ascii(member.value),
                ',' ORDER BY member.key COLLATE "C"
            ),
            ''
        ) || '}'
          INTO result
          FROM jsonb_each(document) AS member(key, value);
        RETURN result;
    ELSIF document_type = 'array' THEN
        SELECT '[' || COALESCE(
            string_agg(
                runtime.canonical_json_ascii(element.value),
                ',' ORDER BY element.ordinal
            ),
            ''
        ) || ']'
          INTO result
          FROM jsonb_array_elements(document)
               WITH ORDINALITY AS element(value, ordinal);
        RETURN result;
    ELSIF document_type = 'string' THEN
        RETURN runtime.json_ascii_quote(document #>> '{}');
    ELSIF document_type IN ('number', 'boolean', 'null') THEN
        RETURN document::text;
    END IF;
    RAISE EXCEPTION 'unsupported JSON type for canonical serialization';
END $$;

CREATE OR REPLACE FUNCTION runtime.assert_production_render_facts_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    facts jsonb;
    ffmpeg_identity_json text;
BEGIN
    IF NEW.render_facts_json IS NULL THEN
        RETURN NULL;
    END IF;
    BEGIN
        facts := NEW.render_facts_json::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'production render facts must be strict JSON';
    END;
    IF jsonb_typeof(facts) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'production render facts use an unsupported closed schema';
    END IF;
    IF ARRAY(SELECT jsonb_object_keys(facts) ORDER BY 1) <> ARRAY[
            'attempt_id', 'execution_limits_sha256', 'execution_schema_version',
            'ffmpeg', 'input_authority_sha256', 'input_count', 'job', 'output',
            'plan_sha256', 'profile_sha256', 'recipe_sha256', 'schema_version',
            'segment_count', 'stderr_sha256', 'story_id'
       ]::text[]
       OR jsonb_typeof(facts->'job') IS DISTINCT FROM 'object'
       OR jsonb_typeof(facts->'ffmpeg') IS DISTINCT FROM 'object'
       OR jsonb_typeof(facts->'output') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'production render facts use an unsupported closed schema';
    END IF;
    IF ARRAY(SELECT jsonb_object_keys(facts->'job') ORDER BY 1)
            <> ARRAY['job_key', 'profile']::text[]
       OR ARRAY(SELECT jsonb_object_keys(facts->'ffmpeg') ORDER BY 1) <> ARRAY[
            'executable_byte_length', 'executable_sha256', 'version_output_sha256'
       ]::text[]
       OR ARRAY(SELECT jsonb_object_keys(facts->'output') ORDER BY 1)
            <> ARRAY['byte_length', 'content_hash', 'media_type']::text[]
       OR EXISTS (
            SELECT 1
              FROM unnest(ARRAY[
                    'attempt_id', 'execution_limits_sha256',
                    'execution_schema_version', 'input_authority_sha256',
                    'plan_sha256', 'profile_sha256', 'recipe_sha256',
                    'schema_version', 'stderr_sha256', 'story_id'
              ]::text[]) AS text_field(key)
             WHERE jsonb_typeof(facts->text_field.key) IS DISTINCT FROM 'string'
       )
       OR jsonb_typeof(facts->'input_count') IS DISTINCT FROM 'number'
       OR jsonb_typeof(facts->'segment_count') IS DISTINCT FROM 'number'
       OR jsonb_typeof(facts->'job'->'job_key') IS DISTINCT FROM 'string'
       OR jsonb_typeof(facts->'job'->'profile') IS DISTINCT FROM 'string'
       OR jsonb_typeof(facts->'ffmpeg'->'executable_sha256')
            IS DISTINCT FROM 'string'
       OR jsonb_typeof(facts->'ffmpeg'->'executable_byte_length')
            IS DISTINCT FROM 'number'
       OR jsonb_typeof(facts->'ffmpeg'->'version_output_sha256')
            IS DISTINCT FROM 'string'
       OR jsonb_typeof(facts->'output'->'content_hash') IS DISTINCT FROM 'string'
       OR jsonb_typeof(facts->'output'->'byte_length') IS DISTINCT FROM 'number'
       OR jsonb_typeof(facts->'output'->'media_type') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'production render facts use an unsupported closed schema';
    END IF;
    IF NEW.render_facts_json IS DISTINCT FROM runtime.canonical_json_ascii(facts) THEN
        RAISE EXCEPTION
            'production render facts must use canonical JSON serialization';
    END IF;
    IF NEW.render_facts_sha256 IS DISTINCT FROM (
        'sha256:' || encode(
            sha256(convert_to(NEW.render_facts_json, 'UTF8')),
            'hex'
        )
    ) THEN
        RAISE EXCEPTION
            'production render facts hash does not bind the exact persisted JSON';
    END IF;
    ffmpeg_identity_json := runtime.canonical_json_ascii(facts->'ffmpeg');
    IF NEW.renderer_identity_sha256 IS DISTINCT FROM (
        'sha256:' || encode(
            sha256(convert_to(ffmpeg_identity_json, 'UTF8')),
            'hex'
        )
    ) THEN
        RAISE EXCEPTION
            'production renderer identity does not bind the persisted FFmpeg facts';
    END IF;
    IF facts->>'schema_version' <> 'production-render-attempt-v1'
       OR facts->>'execution_schema_version' <> 'production-ffmpeg-execution-v1'
       OR facts->>'attempt_id' <> NEW.attempt_id::text
       OR facts->'job'->>'job_key' IS DISTINCT FROM (
            SELECT job_key FROM runtime.jobs WHERE job_id = NEW.job_id
       )
       OR facts->'job'->>'profile' IS DISTINCT FROM (
            SELECT profile FROM runtime.jobs WHERE job_id = NEW.job_id
       )
       OR facts->>'story_id' <> substring(
            NEW.recipe_logical_id FROM length('production_recipe@') + 1
       )
       OR facts->>'recipe_sha256' <> NEW.recipe_content_hash
       OR facts->>'plan_sha256' <> NEW.render_plan_sha256
       OR facts->>'profile_sha256' <> NEW.render_profile_sha256
       OR facts->>'execution_limits_sha256' <> NEW.execution_limits_sha256
       OR facts->'output'->>'content_hash' IS DISTINCT FROM (
            SELECT content_hash FROM storage.blob_objects
             WHERE object_id = NEW.output_object_id
       )
       OR facts->'output'->>'byte_length' IS DISTINCT FROM (
            SELECT byte_length::text FROM storage.blob_objects
             WHERE object_id = NEW.output_object_id
       )
       OR facts->'output'->>'media_type' IS DISTINCT FROM (
            SELECT media_type FROM storage.blob_objects
             WHERE object_id = NEW.output_object_id
       )
       OR facts->'output'->>'media_type' <> 'video/mp4'
       OR facts->'ffmpeg'->>'executable_byte_length' !~ '^[1-9][0-9]*$'
       OR facts->>'input_count' !~ '^[1-9][0-9]*$'
       OR facts->>'segment_count' !~ '^[1-9][0-9]*$'
       OR facts->'output'->>'byte_length' !~ '^[1-9][0-9]*$'
       OR facts->>'story_id' = ''
       OR facts->>'recipe_sha256' !~ '^sha256:[0-9a-f]{64}$'
       OR facts->>'plan_sha256' !~ '^sha256:[0-9a-f]{64}$'
       OR facts->>'profile_sha256' !~ '^sha256:[0-9a-f]{64}$'
       OR facts->>'execution_limits_sha256' !~ '^sha256:[0-9a-f]{64}$'
       OR facts->>'input_authority_sha256' !~ '^sha256:[0-9a-f]{64}$'
       OR facts->>'stderr_sha256' !~ '^sha256:[0-9a-f]{64}$'
       OR facts->'output'->>'content_hash' !~ '^sha256:[0-9a-f]{64}$'
       OR facts->'ffmpeg'->>'executable_sha256' !~ '^sha256:[0-9a-f]{64}$'
       OR facts->'ffmpeg'->>'version_output_sha256' !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'production render facts disagree with reserved authority';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_production_render_facts_integrity_check
AFTER INSERT OR UPDATE ON runtime.production_render_attempts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.assert_production_render_facts_integrity();

COMMENT ON COLUMN runtime.production_render_attempts.renderer_identity_sha256 IS
    'Canonical SHA-256 identity of the ProductionRenderAttemptFacts ffmpeg mapping.';
COMMENT ON COLUMN runtime.production_render_attempts.render_facts_json IS
    'Exact canonical JSON for the path-free ProductionRenderAttemptFacts closure.';

COMMIT;
