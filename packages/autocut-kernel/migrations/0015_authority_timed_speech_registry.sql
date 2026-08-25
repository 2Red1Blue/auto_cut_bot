-- Authority-only immutable TimedSpeech profile registry bootstrap.
--
-- The runtime can read the anchor but cannot write this scope.  The database
-- gate is intentional: application-level validation alone would leave the
-- generic ArtifactMember writer able to forge an authority-looking row.

BEGIN;

LOCK TABLE runtime.jobs, runtime.command_slots, runtime.artifact_sets,
           runtime.artifacts, runtime.artifact_set_members IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE runtime.jobs
    DROP CONSTRAINT jobs_profile_check,
    ADD CONSTRAINT jobs_profile_check
        CHECK (profile IN ('test', 'shadow', 'production', 'authority'));

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runtime.artifacts
         WHERE namespace = 'autocut_authority'
           AND scope_kind = 'registry'
           AND scope_key = 'timed_speech_profiles'
    ) THEN
        RAISE EXCEPTION
            '0015 refuses pre-existing timed speech authority artifacts; bootstrap provenance is required';
    END IF;
END $$;

CREATE TABLE runtime.timed_speech_profile_anchors (
    profile_key text PRIMARY KEY,
    registry_set_sha256 text NOT NULL CHECK (registry_set_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    receipt_id uuid NOT NULL UNIQUE REFERENCES runtime.command_receipts (receipt_id),
    artifact_set_id uuid NOT NULL UNIQUE REFERENCES runtime.artifact_sets (artifact_set_id),
    member_ordinal integer NOT NULL CHECK (member_ordinal = 0),
    content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    command_slot_id uuid NOT NULL UNIQUE REFERENCES runtime.command_slots (command_slot_id),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (profile_key ~ '^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$')
);

CREATE OR REPLACE FUNCTION runtime.guard_timed_speech_authority_artifact()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    slot_name text;
    writer_key text;
    writer_profile text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'timed speech authority artifacts are immutable';
    END IF;
    IF NEW.namespace <> 'autocut_authority'
       OR NEW.scope_kind <> 'registry'
       OR NEW.scope_key <> 'timed_speech_profiles' THEN
        RETURN NEW;
    END IF;
    SELECT slot.command_name, job.job_key, job.profile
      INTO slot_name, writer_key, writer_profile
      FROM runtime.artifact_sets AS artifact_set
      JOIN runtime.command_slots AS slot ON slot.command_slot_id = artifact_set.command_slot_id
      JOIN runtime.jobs AS job ON job.job_id = artifact_set.job_id
     WHERE artifact_set.artifact_set_id = NEW.artifact_set_id;
    IF NOT FOUND
       OR slot_name <> 'BootstrapTimedSpeechProfileRegistry@2.1.3'
       OR writer_key <> 'autocut_authority'
       OR writer_profile <> 'authority'
       OR NEW.artifact_type <> 'timed_speech_profile_registry_entry'
       OR NEW.revision <> 1
       OR NEW.logical_id !~ '^timed-speech/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' THEN
        RAISE EXCEPTION 'timed speech authority registry write requires the dedicated bootstrap writer';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER runtime_guard_timed_speech_authority_artifact
BEFORE INSERT OR UPDATE OR DELETE ON runtime.artifacts
FOR EACH ROW EXECUTE FUNCTION runtime.guard_timed_speech_authority_artifact();

CREATE OR REPLACE FUNCTION runtime.guard_timed_speech_profile_anchor()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    expected_profile_key text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'timed speech authority anchors are immutable';
    END IF;
    SELECT regexp_replace(artifact.logical_id, '^timed-speech/([^/]+)/([^/]+)$', '\\1@\\2')
      INTO expected_profile_key
      FROM runtime.command_receipts AS receipt
      JOIN runtime.command_slots AS slot ON slot.command_slot_id = receipt.command_slot_id
      JOIN runtime.jobs AS job ON job.job_id = slot.job_id
      JOIN runtime.artifact_sets AS artifact_set
        ON artifact_set.artifact_set_id = receipt.result_artifact_set_id
       AND artifact_set.command_slot_id = slot.command_slot_id
      JOIN runtime.artifact_set_members AS member
        ON member.artifact_set_id = artifact_set.artifact_set_id
       AND member.ordinal = NEW.member_ordinal
      JOIN runtime.artifacts AS artifact
        ON artifact.artifact_id = member.artifact_id
       AND artifact.artifact_set_id = artifact_set.artifact_set_id
     WHERE receipt.receipt_id = NEW.receipt_id
       AND receipt.result_artifact_set_id = NEW.artifact_set_id
       AND receipt.outcome = 'succeeded'
       AND slot.command_slot_id = NEW.command_slot_id
       AND slot.command_name = 'BootstrapTimedSpeechProfileRegistry@2.1.3'
       AND slot.state = 'succeeded'
       AND job.job_key = 'autocut_authority'
       AND job.profile = 'authority'
       AND artifact.namespace = 'autocut_authority'
       AND artifact.scope_kind = 'registry'
       AND artifact.scope_key = 'timed_speech_profiles'
       AND artifact.artifact_type = 'timed_speech_profile_registry_entry'
       AND artifact.revision = 1
       AND artifact.content_hash = NEW.content_hash;
    IF NOT FOUND OR expected_profile_key <> NEW.profile_key THEN
        RAISE EXCEPTION 'timed speech authority anchor does not close over its immutable bootstrap member';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER runtime_guard_timed_speech_profile_anchor
BEFORE INSERT OR UPDATE OR DELETE ON runtime.timed_speech_profile_anchors
FOR EACH ROW EXECUTE FUNCTION runtime.guard_timed_speech_profile_anchor();

COMMIT;
