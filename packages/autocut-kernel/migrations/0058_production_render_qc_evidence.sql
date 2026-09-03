-- Persist one bounded, canonical full-file QC observation journal.
-- This migration grants no Receipt, ArtifactSet, visibility, release or
-- publication authority.

BEGIN;

ALTER TABLE runtime.production_render_qc_attempts
    DROP CONSTRAINT production_render_qc_attempts_state_check,
    DROP CONSTRAINT production_render_qc_attempts_check,
    ADD COLUMN evidence_report_json text,
    ADD COLUMN evidence_report_sha256 text CHECK (
        evidence_report_sha256 IS NULL
        OR evidence_report_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    ADD COLUMN evidence_ready_at timestamptz,
    ADD CONSTRAINT production_render_qc_attempts_state_check CHECK (
        state IN ('reserved', 'scanning', 'evidence_ready')
    ),
    ADD CONSTRAINT production_render_qc_attempts_evidence_shape CHECK (
        (
            state = 'reserved' AND version = 0
            AND lease_token IS NULL AND lease_expires_at IS NULL
            AND evidence_report_json IS NULL
            AND evidence_report_sha256 IS NULL
            AND evidence_ready_at IS NULL
        )
        OR
        (
            state = 'scanning' AND version >= 1
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
            AND evidence_report_json IS NULL
            AND evidence_report_sha256 IS NULL
            AND evidence_ready_at IS NULL
        )
        OR
        (
            state = 'evidence_ready' AND version >= 2
            AND lease_token IS NULL AND lease_expires_at IS NULL
            AND evidence_report_json IS NOT NULL
            AND octet_length(evidence_report_json) BETWEEN 1 AND 1048576
            AND evidence_report_sha256 IS NOT NULL
            AND evidence_ready_at IS NOT NULL
        )
    );

CREATE TABLE runtime.production_render_qc_evidence_members (
    qc_attempt_id uuid NOT NULL
        REFERENCES runtime.production_render_qc_attempts (qc_attempt_id),
    check_ordinal integer NOT NULL CHECK (check_ordinal BETWEEN 0 AND 63),
    check_id text NOT NULL CHECK (
        octet_length(check_id) BETWEEN 1 AND 128
        AND check_id ~ '^[a-z0-9][a-z0-9._-]*$'
    ),
    evidence_object_id uuid NOT NULL REFERENCES storage.blob_objects (object_id),
    evidence_content_hash text NOT NULL CHECK (
        evidence_content_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    evidence_byte_length bigint NOT NULL CHECK (
        evidence_byte_length BETWEEN 1 AND 2097152
    ),
    evidence_media_type text NOT NULL CHECK (
        evidence_media_type = 'application/json'
    ),
    PRIMARY KEY (qc_attempt_id, check_ordinal),
    UNIQUE (qc_attempt_id, check_id)
);

CREATE OR REPLACE FUNCTION runtime.guard_production_render_qc_attempt_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'production render QC attempts are durable and cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'reserved' OR NEW.version <> 0
           OR NEW.lease_token IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
           OR NEW.evidence_report_json IS NOT NULL
           OR NEW.evidence_report_sha256 IS NOT NULL
           OR NEW.evidence_ready_at IS NOT NULL THEN
            RAISE EXCEPTION
                'production render QC attempts must begin reserved at version zero';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'evidence_ready' THEN
        RAISE EXCEPTION
            'evidence-ready production render QC attempts are immutable';
    END IF;
    IF (NEW.qc_attempt_id, NEW.render_attempt_id, NEW.job_id,
        NEW.command_slot_id, NEW.rendered_version, NEW.output_object_id,
        NEW.render_facts_sha256, NEW.qc_policy_sha256,
        NEW.required_check_set_version, NEW.qc_runner_identity_sha256,
        NEW.reserved_at)
       IS DISTINCT FROM
       (OLD.qc_attempt_id, OLD.render_attempt_id, OLD.job_id,
        OLD.command_slot_id, OLD.rendered_version, OLD.output_object_id,
        OLD.render_facts_sha256, OLD.qc_policy_sha256,
        OLD.required_check_set_version, OLD.qc_runner_identity_sha256,
        OLD.reserved_at) THEN
        RAISE EXCEPTION 'production render QC attempt identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION
            'production render QC attempt transition requires exact version increment';
    END IF;

    IF OLD.state = 'reserved' AND NEW.state = 'scanning' THEN
        IF NEW.lease_expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'production render QC lease must expire in the future';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'scanning' AND NEW.state = 'scanning' THEN
        IF NEW.lease_expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'production render QC lease renewal or takeover is invalid';
        END IF;
        IF NEW.lease_token = OLD.lease_token THEN
            IF OLD.lease_expires_at <= clock_timestamp()
               OR NEW.lease_expires_at <= OLD.lease_expires_at THEN
                RAISE EXCEPTION
                    'production render QC lease renewal requires an active lease and later expiry';
            END IF;
        ELSIF OLD.lease_expires_at > clock_timestamp() THEN
            RAISE EXCEPTION 'active production render QC lease cannot be taken over';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'scanning' AND NEW.state = 'evidence_ready' THEN
        IF OLD.lease_token IS NULL
           OR OLD.lease_expires_at IS NULL
           OR OLD.lease_expires_at <= clock_timestamp()
           OR NEW.lease_token IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL
           OR NEW.evidence_report_json IS NULL
           OR NEW.evidence_report_sha256 IS NULL THEN
            RAISE EXCEPTION
                'production render QC evidence requires the active exact lease';
        END IF;
        NEW.evidence_ready_at := clock_timestamp();
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid production render QC attempt state transition';
END $$;

CREATE OR REPLACE FUNCTION runtime.prevent_production_render_qc_evidence_member_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'production render QC evidence members are immutable and non-deletable';
END $$;

CREATE TRIGGER runtime_production_render_qc_evidence_member_no_mutation
BEFORE UPDATE OR DELETE ON runtime.production_render_qc_evidence_members
FOR EACH ROW
EXECUTE FUNCTION runtime.prevent_production_render_qc_evidence_member_mutation();

CREATE OR REPLACE FUNCTION runtime.production_render_qc_rational_is_canonical(
    value text
)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
    numerator numeric;
    denominator numeric;
    remainder numeric;
BEGIN
    IF octet_length(value) > 512
       OR value !~ '^(0|-?[1-9][0-9]*)/[1-9][0-9]*$' THEN
        RETURN false;
    END IF;
    numerator := abs(split_part(value, '/', 1)::numeric);
    denominator := split_part(value, '/', 2)::numeric;
    WHILE denominator <> 0 LOOP
        remainder := mod(numerator, denominator);
        numerator := denominator;
        denominator := remainder;
    END LOOP;
    RETURN numerator = 1;
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END $$;

CREATE OR REPLACE FUNCTION runtime.validate_production_render_qc_evidence(
    target_qc_attempt_id uuid
)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    qc_attempt runtime.production_render_qc_attempts%ROWTYPE;
    report jsonb;
    output_document jsonb;
    check_document jsonb;
    measurement_document jsonb;
    evidence_document jsonb;
    check_index integer;
    check_ordinal integer;
    expected_check_id text;
    collection_status text;
    coverage text;
    measurement_name text;
    previous_measurement_name text;
    value_kind text;
    measurement_value text;
    measurement_unit text;
    required_checks text[] := ARRAY[
        'exact_object_identity',
        'container_stream_topology',
        'packet_timeline_integrity',
        'decoded_frame_timeline',
        'full_video_decode',
        'full_audio_decode',
        'video_black_intervals',
        'video_freeze_intervals',
        'audio_silence_intervals',
        'audio_sample_health',
        'av_presentation_envelope',
        'edit_junction_continuity'
    ]::text[];
BEGIN
    SELECT * INTO qc_attempt
      FROM runtime.production_render_qc_attempts
     WHERE qc_attempt_id = target_qc_attempt_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'production render QC evidence has no owning attempt';
    END IF;

    -- Revalidate the complete 0057 parent/output authority on every reread path.
    IF NOT EXISTS (
        SELECT 1
          FROM runtime.production_render_attempts AS parent_render
          JOIN runtime.command_slots AS render_slot
            ON render_slot.command_slot_id = parent_render.command_slot_id
          JOIN runtime.jobs AS render_job
            ON render_job.job_id = parent_render.job_id
          JOIN storage.blob_objects AS output_blob
            ON output_blob.object_id = parent_render.output_object_id
          JOIN storage.blob_claims AS output_claim
            ON output_claim.object_id = output_blob.object_id
         WHERE parent_render.attempt_id = qc_attempt.render_attempt_id
           AND parent_render.state = 'rendered'
           AND parent_render.version = qc_attempt.rendered_version
           AND parent_render.output_object_id = qc_attempt.output_object_id
           AND parent_render.render_facts_sha256 = qc_attempt.render_facts_sha256
           AND parent_render.render_facts_json IS NOT NULL
           AND parent_render.job_id = qc_attempt.job_id
           AND parent_render.command_slot_id = qc_attempt.command_slot_id
           AND render_slot.job_id = qc_attempt.job_id
           AND render_slot.state = 'running'
           AND render_slot.command_name = 'RenderProductionRecipeCommand@1'
           AND render_slot.execution_kind = 'deterministic'
           AND render_job.profile IN ('shadow', 'production')
           AND output_blob.object_id = qc_attempt.output_object_id
           AND output_blob.storage_kind = 's3_compatible'
           AND output_blob.byte_length > 0
           AND output_blob.media_type = 'video/mp4'
           AND output_claim.job_id = qc_attempt.job_id
    ) THEN
        RAISE EXCEPTION
            'production render QC attempt must bind one exact rendered output authority';
    END IF;

    IF qc_attempt.state <> 'evidence_ready' THEN
        RETURN;
    END IF;
    IF qc_attempt.required_check_set_version <> 'production-av-qc-v1' THEN
        RAISE EXCEPTION
            'production render QC evidence uses an unregistered required check set';
    END IF;
    BEGIN
        report := qc_attempt.evidence_report_json::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'production render QC evidence report must be strict JSON';
    END;
    IF jsonb_typeof(report) IS DISTINCT FROM 'object'
       OR ARRAY(
            SELECT key
              FROM jsonb_object_keys(report) AS report_key(key)
             ORDER BY key COLLATE "C"
       ) <> ARRAY[
            'checks', 'command_slot_id', 'job_id', 'output_blob',
            'qc_attempt_id', 'qc_policy_sha256',
            'qc_runner_identity_sha256', 'render_attempt_id',
            'render_facts_sha256', 'required_check_set_version',
            'schema_version'
       ]::text[]
       OR jsonb_typeof(report->'checks') IS DISTINCT FROM 'array'
       OR jsonb_typeof(report->'output_blob') IS DISTINCT FROM 'object'
       OR EXISTS (
            SELECT 1
              FROM unnest(ARRAY[
                    'command_slot_id', 'job_id', 'qc_attempt_id',
                    'qc_policy_sha256', 'qc_runner_identity_sha256',
                    'render_attempt_id', 'render_facts_sha256',
                    'required_check_set_version', 'schema_version'
              ]::text[]) AS text_field(key)
             WHERE jsonb_typeof(report->text_field.key) IS DISTINCT FROM 'string'
       ) THEN
        RAISE EXCEPTION
            'production render QC evidence report uses an unsupported closed schema';
    END IF;
    IF qc_attempt.evidence_report_json IS DISTINCT FROM
            runtime.canonical_json_ascii(report) THEN
        RAISE EXCEPTION
            'production render QC evidence report must use canonical JSON serialization';
    END IF;
    IF qc_attempt.evidence_report_sha256 IS DISTINCT FROM (
        'sha256:' || encode(
            sha256(convert_to(qc_attempt.evidence_report_json, 'UTF8')),
            'hex'
        )
    ) THEN
        RAISE EXCEPTION
            'production render QC evidence report hash does not bind the exact persisted JSON';
    END IF;
    IF report->>'schema_version' <> 'production-render-qc-evidence-v1'
       OR report->>'qc_attempt_id' <> qc_attempt.qc_attempt_id::text
       OR report->>'render_attempt_id' <> qc_attempt.render_attempt_id::text
       OR report->>'job_id' <> qc_attempt.job_id::text
       OR report->>'command_slot_id' <> qc_attempt.command_slot_id::text
       OR report->>'render_facts_sha256' <> qc_attempt.render_facts_sha256
       OR report->>'qc_policy_sha256' <> qc_attempt.qc_policy_sha256
       OR report->>'required_check_set_version'
            <> qc_attempt.required_check_set_version
       OR report->>'qc_runner_identity_sha256'
            <> qc_attempt.qc_runner_identity_sha256 THEN
        RAISE EXCEPTION
            'production render QC evidence report disagrees with reserved authority';
    END IF;

    output_document := report->'output_blob';
    IF ARRAY(
            SELECT key
              FROM jsonb_object_keys(output_document) AS output_key(key)
             ORDER BY key COLLATE "C"
       ) <> ARRAY[
            'byte_length', 'content_hash', 'media_type', 'object_id'
       ]::text[]
       OR jsonb_typeof(output_document->'byte_length') IS DISTINCT FROM 'number'
       OR jsonb_typeof(output_document->'content_hash') IS DISTINCT FROM 'string'
       OR jsonb_typeof(output_document->'media_type') IS DISTINCT FROM 'string'
       OR jsonb_typeof(output_document->'object_id') IS DISTINCT FROM 'string'
       OR output_document->>'byte_length' !~ '^[1-9][0-9]*$'
       OR output_document->>'content_hash' !~ '^sha256:[0-9a-f]{64}$'
       OR output_document->>'media_type' <> 'video/mp4'
       OR output_document->>'object_id' <> qc_attempt.output_object_id::text
       OR NOT EXISTS (
            SELECT 1 FROM storage.blob_objects AS output_blob
             WHERE output_blob.object_id = qc_attempt.output_object_id
               AND output_blob.content_hash = output_document->>'content_hash'
               AND output_blob.byte_length::text = output_document->>'byte_length'
               AND output_blob.media_type = output_document->>'media_type'
       ) THEN
        RAISE EXCEPTION
            'production render QC evidence report output BlobRef is not exact';
    END IF;

    IF jsonb_array_length(report->'checks') > 64
       OR jsonb_array_length(report->'checks') <> cardinality(required_checks) THEN
        RAISE EXCEPTION
            'production render QC evidence required check set is incomplete';
    END IF;

    FOR check_document, check_index IN
        SELECT item.value, (item.ordinality - 1)::integer
          FROM jsonb_array_elements(report->'checks')
               WITH ORDINALITY AS item(value, ordinality)
    LOOP
        IF jsonb_typeof(check_document) IS DISTINCT FROM 'object'
           OR ARRAY(
                SELECT key
                  FROM jsonb_object_keys(check_document) AS check_key(key)
                 ORDER BY key COLLATE "C"
           ) <> ARRAY[
                'argv_sha256', 'check_id', 'check_ordinal',
                'collection_status', 'coverage', 'diagnostic_code',
                'evidence_blob', 'measurements', 'parser_schema_version',
                'tool_identity_sha256'
           ]::text[]
           OR jsonb_typeof(check_document->'check_ordinal')
                IS DISTINCT FROM 'number'
           OR jsonb_typeof(check_document->'check_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(check_document->'collection_status')
                IS DISTINCT FROM 'string'
           OR jsonb_typeof(check_document->'coverage') IS DISTINCT FROM 'string'
           OR jsonb_typeof(check_document->'parser_schema_version')
                IS DISTINCT FROM 'string'
           OR jsonb_typeof(check_document->'tool_identity_sha256')
                IS DISTINCT FROM 'string'
           OR jsonb_typeof(check_document->'argv_sha256')
                IS DISTINCT FROM 'string'
           OR jsonb_typeof(check_document->'measurements')
                IS DISTINCT FROM 'array'
           OR jsonb_typeof(check_document->'evidence_blob')
                IS DISTINCT FROM 'object'
           OR jsonb_typeof(check_document->'diagnostic_code')
                NOT IN ('string', 'null') THEN
            RAISE EXCEPTION
                'production render QC check evidence uses an unsupported closed schema';
        END IF;
        -- The closed report cap is 64 checks, so every valid ordinal has at
        -- most two digits. Bound length before the int4 cast.
        IF octet_length(check_document->>'check_ordinal') > 2
           OR check_document->>'check_ordinal' !~ '^(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION 'production render QC check ordinal is not canonical';
        END IF;
        check_ordinal := (check_document->>'check_ordinal')::integer;
        expected_check_id := check_document->>'check_id';
        collection_status := check_document->>'collection_status';
        coverage := check_document->>'coverage';
        IF check_ordinal <> check_index
           OR expected_check_id <> required_checks[check_index + 1] THEN
            RAISE EXCEPTION
                'production render QC evidence checks are missing, extra or reordered';
        END IF;
        IF octet_length(expected_check_id) > 128
           OR expected_check_id !~ '^[a-z0-9][a-z0-9._-]*$'
           OR octet_length(check_document->>'parser_schema_version') NOT BETWEEN 1 AND 128
           OR check_document->>'parser_schema_version'
                !~ '^[a-z0-9][a-z0-9._-]*$'
           OR check_document->>'tool_identity_sha256'
                !~ '^sha256:[0-9a-f]{64}$'
           OR check_document->>'argv_sha256' !~ '^sha256:[0-9a-f]{64}$' THEN
            RAISE EXCEPTION
                'production render QC check identity is invalid or unbounded';
        END IF;
        IF collection_status NOT IN (
                'completed', 'incomplete', 'not_run', 'not_applicable'
           )
           OR coverage NOT IN ('full_file', 'partial', 'none', 'not_applicable')
           OR (collection_status = 'completed'
               AND coverage NOT IN ('full_file', 'not_applicable'))
           OR (collection_status = 'incomplete'
               AND coverage NOT IN ('partial', 'none'))
           OR (collection_status = 'not_run' AND coverage <> 'none')
           OR (collection_status = 'not_applicable'
               AND coverage <> 'not_applicable') THEN
            RAISE EXCEPTION
                'production render QC collection status and coverage disagree';
        END IF;
        IF jsonb_typeof(check_document->'diagnostic_code') = 'string'
           AND (
                octet_length(check_document->>'diagnostic_code') NOT BETWEEN 1 AND 128
                OR check_document->>'diagnostic_code'
                    !~ '^[a-z0-9][a-z0-9._-]*$'
           ) THEN
            RAISE EXCEPTION
                'production render QC diagnostic code is invalid or unbounded';
        END IF;
        IF jsonb_array_length(check_document->'measurements') > 256 THEN
            RAISE EXCEPTION
                'production render QC check exceeds the measurement cap';
        END IF;

        previous_measurement_name := NULL;
        FOR measurement_document IN
            SELECT item.value
              FROM jsonb_array_elements(check_document->'measurements')
                   WITH ORDINALITY AS item(value, ordinality)
             ORDER BY item.ordinality
        LOOP
            IF jsonb_typeof(measurement_document) IS DISTINCT FROM 'object'
               OR ARRAY(
                    SELECT key
                      FROM jsonb_object_keys(measurement_document)
                           AS measurement_key(key)
                     ORDER BY key COLLATE "C"
               )
                    <> ARRAY['name', 'unit', 'value', 'value_kind']::text[]
               OR EXISTS (
                    SELECT 1
                      FROM unnest(ARRAY[
                            'name', 'unit', 'value', 'value_kind'
                      ]::text[]) AS text_field(key)
                     WHERE jsonb_typeof(measurement_document->text_field.key)
                            IS DISTINCT FROM 'string'
               ) THEN
                RAISE EXCEPTION
                    'production render QC measurement uses an unsupported closed schema';
            END IF;
            measurement_name := measurement_document->>'name';
            value_kind := measurement_document->>'value_kind';
            measurement_value := measurement_document->>'value';
            measurement_unit := measurement_document->>'unit';
            IF octet_length(measurement_name) NOT BETWEEN 1 AND 128
               OR measurement_name !~ '^[a-z0-9][a-z0-9._-]*$' THEN
                RAISE EXCEPTION
                    'production render QC measurement name is invalid or unbounded';
            END IF;
            IF previous_measurement_name IS NOT NULL
               AND NOT (
                    previous_measurement_name COLLATE "C"
                    < measurement_name COLLATE "C"
               ) THEN
                RAISE EXCEPTION
                    'production render QC measurement names must be unique and strictly increasing';
            END IF;
            previous_measurement_name := measurement_name;
            IF value_kind NOT IN (
                    'integer', 'decimal', 'rational', 'boolean', 'text', 'sha256'
               )
               OR octet_length(measurement_value) > 512 THEN
                RAISE EXCEPTION
                    'production render QC measurement value is invalid or unbounded';
            END IF;
            IF measurement_unit NOT IN (
                    'none', 'count', 'byte', 'tick', 'second', 'frame',
                    'sample', 'packet', 'stream', 'channel', 'hertz',
                    'decibel', 'lufs', 'percent', 'ratio'
               ) THEN
                RAISE EXCEPTION 'production render QC measurement unit is unregistered';
            END IF;
            IF value_kind = 'integer'
               AND measurement_value !~ '^(0|-?[1-9][0-9]*)$' THEN
                RAISE EXCEPTION 'production render QC integer is not canonical';
            ELSIF value_kind = 'decimal'
               AND measurement_value !~
                    '^(0|-?[1-9][0-9]*|-?(0|[1-9][0-9]*)[.][0-9]*[1-9])$' THEN
                RAISE EXCEPTION 'production render QC decimal is not canonical';
            ELSIF value_kind = 'rational'
               AND NOT runtime.production_render_qc_rational_is_canonical(
                    measurement_value
               ) THEN
                RAISE EXCEPTION 'production render QC rational is not canonical';
            ELSIF value_kind = 'boolean'
               AND measurement_value NOT IN ('true', 'false') THEN
                RAISE EXCEPTION 'production render QC boolean is not canonical';
            ELSIF value_kind = 'sha256'
               AND measurement_value !~ '^sha256:[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'production render QC SHA-256 is not canonical';
            ELSIF value_kind = 'text'
               AND (
                    octet_length(measurement_value) NOT BETWEEN 1 AND 512
                    OR measurement_name ~ '(^|_)(path|locator|uri|url)($|_)'
               ) THEN
                RAISE EXCEPTION
                    'production render QC measurement text cannot carry locator or path semantics';
            END IF;
        END LOOP;

        evidence_document := check_document->'evidence_blob';
        IF ARRAY(
                SELECT key
                  FROM jsonb_object_keys(evidence_document) AS evidence_key(key)
                 ORDER BY key COLLATE "C"
           ) <> ARRAY[
                'byte_length', 'content_hash', 'media_type', 'object_id'
           ]::text[]
           OR jsonb_typeof(evidence_document->'byte_length')
                IS DISTINCT FROM 'number'
           OR jsonb_typeof(evidence_document->'content_hash')
                IS DISTINCT FROM 'string'
           OR jsonb_typeof(evidence_document->'media_type')
                IS DISTINCT FROM 'string'
           OR jsonb_typeof(evidence_document->'object_id')
                IS DISTINCT FROM 'string'
           OR evidence_document->>'byte_length' !~ '^[1-9][0-9]*$'
           OR (evidence_document->>'byte_length')::numeric > 2097152
           OR evidence_document->>'content_hash' !~ '^sha256:[0-9a-f]{64}$'
           OR evidence_document->>'media_type' <> 'application/json' THEN
            RAISE EXCEPTION
                'production render QC evidence BlobRef is invalid or exceeds its cap';
        END IF;
        IF NOT EXISTS (
            SELECT 1
              FROM runtime.production_render_qc_evidence_members AS member
              JOIN storage.blob_objects AS evidence_blob
                ON evidence_blob.object_id = member.evidence_object_id
              JOIN storage.blob_claims AS evidence_claim
                ON evidence_claim.object_id = evidence_blob.object_id
             WHERE member.qc_attempt_id = qc_attempt.qc_attempt_id
               AND member.check_ordinal = check_index
               AND member.check_id = expected_check_id
               AND member.evidence_object_id::text
                    = evidence_document->>'object_id'
               AND member.evidence_content_hash
                    = evidence_document->>'content_hash'
               AND member.evidence_byte_length::text
                    = evidence_document->>'byte_length'
               AND member.evidence_media_type
                    = evidence_document->>'media_type'
               AND evidence_blob.object_id = member.evidence_object_id
               AND evidence_blob.content_hash = member.evidence_content_hash
               AND evidence_blob.byte_length = member.evidence_byte_length
               AND evidence_blob.media_type = member.evidence_media_type
               AND evidence_claim.job_id = qc_attempt.job_id
               AND evidence_claim.object_id = evidence_blob.object_id
        ) THEN
            RAISE EXCEPTION
                'production render QC evidence member or same-Job Blob claim is not exact';
        END IF;
    END LOOP;

    IF (
        SELECT count(*)
          FROM runtime.production_render_qc_evidence_members AS member
         WHERE member.qc_attempt_id = qc_attempt.qc_attempt_id
    ) <> cardinality(required_checks) THEN
        RAISE EXCEPTION
            'production render QC evidence member set is incomplete or has extras';
    END IF;
    IF (
        SELECT COALESCE(sum(member.evidence_byte_length), 0)
          FROM runtime.production_render_qc_evidence_members AS member
         WHERE member.qc_attempt_id = qc_attempt.qc_attempt_id
    ) > 16777216 THEN
        RAISE EXCEPTION
            'production render QC evidence member set exceeds the aggregate cap';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION runtime.assert_production_render_qc_attempt_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM runtime.validate_production_render_qc_evidence(NEW.qc_attempt_id);
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION runtime.assert_production_render_qc_evidence_member_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- This is evaluated by the deferred trigger against the transaction's
    -- final row state. It permits member-first attachment followed by the
    -- fenced attempt transition, but never a committed partial scanning row.
    IF NOT EXISTS (
        SELECT 1
          FROM runtime.production_render_qc_attempts AS owner
         WHERE owner.qc_attempt_id = NEW.qc_attempt_id
           AND owner.state = 'evidence_ready'
    ) THEN
        RAISE EXCEPTION
            'production render QC evidence members require an evidence-ready owner';
    END IF;
    PERFORM runtime.validate_production_render_qc_evidence(NEW.qc_attempt_id);
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER runtime_production_render_qc_evidence_member_integrity_check
AFTER INSERT ON runtime.production_render_qc_evidence_members
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.assert_production_render_qc_evidence_member_integrity();

-- evidence_ready remains an active private journal. Only the later atomic
-- QC/release owner may replace this parent transition function and consume it.
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
        IF EXISTS (
            SELECT 1
              FROM runtime.production_render_qc_attempts AS qc_attempt
             WHERE qc_attempt.render_attempt_id = OLD.attempt_id
               AND qc_attempt.state IN ('reserved', 'scanning', 'evidence_ready')
        ) THEN
            RAISE EXCEPTION
                'production render with an active QC journal cannot become terminal';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid production render attempt state transition';
END $$;

COMMENT ON TABLE runtime.production_render_qc_evidence_members IS
    'Immutable private full-file QC evidence BlobRefs; never release authority.';
COMMENT ON COLUMN runtime.production_render_qc_attempts.evidence_report_json IS
    'Exact canonical path-free production-render-qc-evidence-v1 JSON.';
COMMENT ON COLUMN runtime.production_render_qc_attempts.evidence_ready_at IS
    'Database time metadata excluded from the canonical evidence report hash.';

REVOKE ALL ON runtime.production_render_qc_evidence_members FROM PUBLIC;

COMMIT;
