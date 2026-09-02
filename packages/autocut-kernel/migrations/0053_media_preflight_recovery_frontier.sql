-- Minimal persistent episode-success frontier for selective Media Preflight recovery.

BEGIN;

CREATE TABLE runtime.media_preflight_recovery_frontiers (
    frontier_id uuid PRIMARY KEY,
    plan_sha256 text NOT NULL UNIQUE CHECK (plan_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    base_job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    plan_json jsonb NOT NULL CHECK (jsonb_typeof(plan_json) = 'object'),
    episode_count integer NOT NULL CHECK (episode_count > 0),
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'complete', 'finalized')),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    finalizer_job_id uuid REFERENCES runtime.jobs (job_id),
    final_receipt_id uuid REFERENCES runtime.command_receipts (receipt_id),
    final_artifact_set_id uuid REFERENCES runtime.artifact_sets (artifact_set_id),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (
        (state = 'open' AND finalizer_job_id IS NULL
             AND final_receipt_id IS NULL AND final_artifact_set_id IS NULL)
        OR (state = 'complete' AND finalizer_job_id IS NOT NULL
             AND final_receipt_id IS NULL AND final_artifact_set_id IS NULL)
        OR (state = 'finalized' AND finalizer_job_id IS NOT NULL
             AND final_receipt_id IS NOT NULL AND final_artifact_set_id IS NOT NULL)
    )
);

CREATE TABLE runtime.media_preflight_recovery_entries (
    frontier_id uuid NOT NULL
        REFERENCES runtime.media_preflight_recovery_frontiers (frontier_id),
    episode_index integer NOT NULL CHECK (episode_index >= 0),
    requirement_sha256 text NOT NULL CHECK (requirement_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    origin_job_id uuid NOT NULL REFERENCES runtime.jobs (job_id),
    idempotency_key text NOT NULL,
    request_hash text NOT NULL CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),
    transient_retry_budget integer NOT NULL CHECK (transient_retry_budget BETWEEN 0 AND 3),
    command_slot_id uuid NOT NULL REFERENCES runtime.command_slots (command_slot_id),
    receipt_id uuid NOT NULL REFERENCES runtime.command_receipts (receipt_id),
    artifact_set_id uuid NOT NULL REFERENCES runtime.artifact_sets (artifact_set_id),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (frontier_id, episode_index),
    UNIQUE (frontier_id, command_slot_id),
    UNIQUE (frontier_id, receipt_id),
    UNIQUE (frontier_id, artifact_set_id)
);

CREATE OR REPLACE FUNCTION runtime.assert_media_recovery_entry_exact_success()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    expected_count integer;
    expected_requirement text;
    expected_producer_kind text;
    expected_layout text[];
    actual_layout text[];
BEGIN
    SELECT episode_count,
           plan_json->'requirement_sha256s'->>NEW.episode_index,
           plan_json->>'producer_kind'
      INTO expected_count, expected_requirement, expected_producer_kind
      FROM runtime.media_preflight_recovery_frontiers
     WHERE frontier_id = NEW.frontier_id;
    IF expected_count IS NULL OR NEW.episode_index >= expected_count
       OR expected_requirement IS NULL OR expected_requirement <> NEW.requirement_sha256 THEN
        RAISE EXCEPTION 'media recovery entry does not satisfy its exact census slot';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM runtime.command_slots slot
          JOIN runtime.command_receipts receipt
            ON receipt.command_slot_id = slot.command_slot_id
          JOIN runtime.artifact_sets artifact_set
            ON artifact_set.command_slot_id = slot.command_slot_id
           AND artifact_set.job_id = slot.job_id
         WHERE slot.command_slot_id = NEW.command_slot_id
           AND slot.job_id = NEW.origin_job_id
           AND slot.idempotency_key = NEW.idempotency_key
           AND slot.request_hash = NEW.request_hash
           AND slot.state = 'succeeded'
           AND slot.command_name = CASE expected_producer_kind
               WHEN 'local_cpu' THEN 'PrepareTimedMediaEvidence@2.1.3'
               WHEN 'pc_cuda' THEN 'PrepareRuntimeTimedMediaEvidence@1.0.0'
               ELSE '__invalid__'
           END
           AND receipt.receipt_id = NEW.receipt_id
           AND receipt.outcome = 'succeeded'
           AND receipt.result_artifact_set_id = NEW.artifact_set_id
           AND artifact_set.artifact_set_id = NEW.artifact_set_id
           AND artifact_set.member_count = 5
    ) THEN
        RAISE EXCEPTION 'media recovery entry is not an exact succeeded command closure';
    END IF;
    expected_layout := CASE expected_producer_kind
        WHEN 'local_cpu' THEN ARRAY[
            'root_media_evidence_bundle',
            'candidate_timed_evidence_index',
            'timed_speech_profile_admission',
            'presentation_timeline_probe',
            'committed_video_to_audio_clock_map_certificate'
        ]::text[]
        WHEN 'pc_cuda' THEN ARRAY[
            'root_media_evidence_bundle',
            'candidate_timed_evidence_index',
            'runtime_timed_speech_capability_admission',
            'presentation_timeline_probe',
            'committed_video_to_audio_clock_map_certificate'
        ]::text[]
        ELSE ARRAY[]::text[]
    END;
    SELECT array_agg(artifact.artifact_type ORDER BY member.ordinal)
      INTO actual_layout
      FROM runtime.artifact_set_members member
      JOIN runtime.artifacts artifact ON artifact.artifact_id = member.artifact_id
     WHERE member.artifact_set_id = NEW.artifact_set_id;
    IF actual_layout IS DISTINCT FROM expected_layout THEN
        RAISE EXCEPTION 'media recovery entry has the wrong producer member layout';
    END IF;
    RETURN NEW;
END $$;

CREATE CONSTRAINT TRIGGER runtime_media_recovery_entry_exact_success
AFTER INSERT OR UPDATE ON runtime.media_preflight_recovery_entries
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.assert_media_recovery_entry_exact_success();

CREATE OR REPLACE FUNCTION runtime.protect_media_recovery_entry()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'media recovery entries are append-only';
END $$;

CREATE TRIGGER runtime_media_recovery_entry_no_update
BEFORE UPDATE OR DELETE ON runtime.media_preflight_recovery_entries
FOR EACH ROW EXECUTE FUNCTION runtime.protect_media_recovery_entry();

CREATE OR REPLACE FUNCTION runtime.protect_media_recovery_frontier()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    covered_count integer;
    expected_producer_kind text;
    expected_finalizer_command text;
    expected_final_layout text[];
    actual_final_layout text[];
BEGIN
    IF ROW(
        NEW.frontier_id, NEW.plan_sha256, NEW.base_job_id, NEW.plan_json,
        NEW.episode_count, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.frontier_id, OLD.plan_sha256, OLD.base_job_id, OLD.plan_json,
        OLD.episode_count, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'media recovery plan identity is immutable';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'media recovery frontier version must advance exactly once';
    END IF;
    IF NOT (
        (OLD.state = 'open' AND NEW.state = 'open'
             AND NEW.finalizer_job_id IS NULL)
        OR (OLD.state = 'open' AND NEW.state = 'complete'
             AND NEW.finalizer_job_id IS NOT NULL)
        OR (OLD.state = 'complete' AND NEW.state = 'finalized'
             AND NEW.finalizer_job_id = OLD.finalizer_job_id)
    ) THEN
        RAISE EXCEPTION 'media recovery frontier transition is invalid';
    END IF;
    IF NEW.state IN ('complete', 'finalized') THEN
        SELECT count(*) INTO covered_count
          FROM runtime.media_preflight_recovery_entries
         WHERE frontier_id = NEW.frontier_id;
        IF covered_count <> NEW.episode_count THEN
            RAISE EXCEPTION 'media recovery frontier cannot close with partial coverage';
        END IF;
    END IF;
    SELECT plan_json->>'producer_kind'
      INTO expected_producer_kind
      FROM runtime.media_preflight_recovery_frontiers
     WHERE frontier_id = NEW.frontier_id;
    expected_finalizer_command := CASE expected_producer_kind
        WHEN 'local_cpu' THEN 'FinalizeTimedMediaEvidenceBatch@2.1.3'
        WHEN 'pc_cuda' THEN 'FinalizeRuntimeTimedMediaEvidenceBatch@1.0.0'
        ELSE '__invalid__'
    END;
    expected_final_layout := CASE expected_producer_kind
        WHEN 'local_cpu' THEN ARRAY['timed_media_evidence_batch']::text[]
        WHEN 'pc_cuda' THEN ARRAY['runtime_timed_media_evidence_batch']::text[]
        ELSE ARRAY[]::text[]
    END;
    IF NEW.state = 'finalized' AND NOT EXISTS (
        SELECT 1
          FROM runtime.command_receipts receipt
          JOIN runtime.command_slots slot
            ON slot.command_slot_id = receipt.command_slot_id
           JOIN runtime.artifact_sets artifact_set
             ON artifact_set.artifact_set_id = receipt.result_artifact_set_id
         WHERE receipt.receipt_id = NEW.final_receipt_id
           AND receipt.outcome = 'succeeded'
           AND artifact_set.artifact_set_id = NEW.final_artifact_set_id
           AND slot.job_id = NEW.finalizer_job_id
           AND slot.command_name = expected_finalizer_command
           AND artifact_set.member_count = 1
    ) THEN
        RAISE EXCEPTION 'media recovery final batch is not an exact succeeded closure';
    END IF;
    IF NEW.state = 'finalized' THEN
        SELECT array_agg(artifact.artifact_type ORDER BY member.ordinal)
          INTO actual_final_layout
          FROM runtime.artifact_set_members member
          JOIN runtime.artifacts artifact ON artifact.artifact_id = member.artifact_id
         WHERE member.artifact_set_id = NEW.final_artifact_set_id;
        IF actual_final_layout IS DISTINCT FROM expected_final_layout THEN
            RAISE EXCEPTION 'media recovery final batch has the wrong producer layout';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER runtime_media_recovery_frontier_update_guard
BEFORE UPDATE ON runtime.media_preflight_recovery_frontiers
FOR EACH ROW EXECUTE FUNCTION runtime.protect_media_recovery_frontier();

CREATE TRIGGER runtime_media_recovery_frontier_no_delete
BEFORE DELETE ON runtime.media_preflight_recovery_frontiers
FOR EACH ROW EXECUTE FUNCTION runtime.protect_media_recovery_entry();

REVOKE ALL ON runtime.media_preflight_recovery_frontiers FROM PUBLIC;
REVOKE ALL ON runtime.media_preflight_recovery_entries FROM PUBLIC;

COMMIT;
