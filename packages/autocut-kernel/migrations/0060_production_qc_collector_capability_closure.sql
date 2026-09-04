-- Close the write-time integrity gap in 0059 without changing its immutable
-- guards, privileges, identities, or ordinary/calibration finalization rules.
BEGIN;

LOCK TABLE runtime.jobs, runtime.command_slots, runtime.command_receipts,
           runtime.artifact_sets, runtime.artifacts, runtime.artifact_set_members,
           runtime.production_qc_collector_capabilities
    IN SHARE ROW EXCLUSIVE MODE;

-- All strings in the closed capability documents are ASCII identifiers. The
-- existing sorted, compact serializer therefore also matches ensure_ascii=False
-- in the Store member/set writer; jsonb::text does not match that wire format.
CREATE FUNCTION runtime.production_qc_capability_json_sha256(document jsonb)
RETURNS text LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT 'sha256:' || encode(sha256(convert_to(
        runtime.canonical_json_ascii(document), 'UTF8'
    )), 'hex')
$$;

CREATE FUNCTION runtime.assert_production_qc_capability_row(
    capability runtime.production_qc_collector_capabilities
)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    policy jsonb;
    live jsonb;
    request_document jsonb;
    measurement jsonb;
    decision_document jsonb;
    expected_scope text;
    expected_job text;
    expected_key text;
    expected_request_hash text;
    expected_manifest jsonb;
    actual_manifest jsonb;
    actual_set_hash text;
BEGIN
    -- Construct closed projections from typed columns, rather than trusting
    -- selected keys in caller JSON (which would admit unknown/duplicate keys).
    policy := jsonb_build_object(
        'schema_version', 'production-qc-collector-policy-source-v1',
        'profile_id', capability.profile_id,
        'policy_source_sha256', capability.policy_source_sha256,
        'registry_snapshot_sha256', capability.registry_snapshot_sha256,
        'collector_registry_sha256', capability.collector_registry_sha256,
        'required_check_set_version', capability.required_check_set_version,
        'runner_schema_version', capability.runner_schema_version,
        'fixed_environment_sha256', capability.fixed_environment_sha256
    );
    live := policy || jsonb_build_object(
        'schema_version', 'production-qc-collector-live-profile-v1',
        'ffmpeg_identity', jsonb_build_object(
            'executable_sha256', capability.ffmpeg_executable_sha256,
            'executable_byte_length', capability.ffmpeg_executable_byte_length,
            'version_output_sha256', capability.ffmpeg_version_output_sha256
        ),
        'ffprobe_identity', jsonb_build_object(
            'executable_sha256', capability.ffprobe_executable_sha256,
            'executable_byte_length', capability.ffprobe_executable_byte_length,
            'version_output_sha256', capability.ffprobe_version_output_sha256
        )
    );
    request_document := jsonb_build_object(
        'schema_version', 'production-qc-collector-capability-request-v1',
        'authority_state', 'store_acceptance_required',
        'policy_source', policy, 'live_profile', live
    );
    expected_scope := 'production_qc_collector_capability:'
        || capability.profile_id || ':'
        || substring(capability.policy_source_sha256 from 8) || ':'
        || substring(capability.registry_snapshot_sha256 from 8) || ':'
        || substring(capability.qc_runner_identity_sha256 from 8);
    expected_job := 'autocut_production_qc_collector_validator:'
        || substring(expected_scope from length('production_qc_collector_capability:') + 1);
    expected_key := 'production-qc-collector-capability:'
        || capability.profile_id || ':'
        || substring(capability.policy_source_sha256 from 8) || ':'
        || substring(capability.registry_snapshot_sha256 from 8) || ':'
        || substring(capability.capability_request_sha256 from 8);
    expected_request_hash := runtime.production_qc_capability_json_sha256(
        jsonb_build_object(
            'command', 'AcceptProductionRenderQcCollectorCapability@1',
            'request', request_document
        )
    );
    IF capability.scope_key IS DISTINCT FROM expected_scope
       OR capability.capability_request_json IS DISTINCT FROM
            runtime.canonical_json_ascii(request_document)
       OR capability.capability_request_sha256 IS DISTINCT FROM
            runtime.production_qc_capability_json_sha256(request_document)
       -- 0059 stores the *full live profile* digest here, not the runner's
       -- separately defined compact evaluator identity.
       OR capability.qc_runner_identity_sha256 IS DISTINCT FROM
            runtime.production_qc_capability_json_sha256(live) THEN
        RAISE EXCEPTION 'production QC capability request/static/live closure mismatch';
    END IF;

    measurement := jsonb_build_object(
        'schema_version', 'production-qc-collector-measurement-v1',
        'capability_request_sha256', capability.capability_request_sha256,
        'live_profile', live
    );
    decision_document := jsonb_build_object(
        'schema_version', 'production-qc-collector-decision-v1',
        'capability_request_sha256', capability.capability_request_sha256,
        'decision', 'accepted',
        'measurement_member_sha256', capability.measurement_member_sha256,
        'policy_source', policy,
        'authority_provenance', jsonb_build_object(
            'authority_revision', capability.authority_revision,
            'authority_bundle_sha256', capability.authority_bundle_sha256,
            'source_commit', capability.source_commit,
            'inventory_commit', capability.inventory_commit,
            'lock_commit', capability.lock_commit
        )
    );
    IF capability.measurement_member_sha256 IS DISTINCT FROM
            runtime.production_qc_capability_json_sha256(measurement)
       OR capability.capability_member_sha256 IS DISTINCT FROM
            runtime.production_qc_capability_json_sha256(decision_document) THEN
        RAISE EXCEPTION 'production QC capability member payload/hash closure mismatch';
    END IF;

    SELECT artifact_set.set_hash INTO actual_set_hash
      FROM runtime.jobs AS job
      JOIN runtime.command_slots AS slot ON slot.job_id = job.job_id
      JOIN runtime.command_receipts AS receipt
        ON receipt.command_slot_id = slot.command_slot_id
      JOIN runtime.artifact_sets AS artifact_set
        ON artifact_set.command_slot_id = slot.command_slot_id
       AND artifact_set.job_id = job.job_id
       AND artifact_set.artifact_set_id = receipt.result_artifact_set_id
     WHERE job.job_key = expected_job AND job.profile = 'authority'
       AND job.state = 'succeeded'
       AND slot.command_slot_id = capability.command_slot_id
       AND slot.command_name = 'AcceptProductionRenderQcCollectorCapability@1'
       AND slot.execution_kind = 'deterministic'
       AND slot.idempotency_key = expected_key
       AND slot.request_hash = expected_request_hash
       AND slot.state = 'succeeded'
       AND receipt.receipt_id = capability.receipt_id
       AND receipt.outcome = 'succeeded'
       AND artifact_set.artifact_set_id = capability.artifact_set_id
       AND artifact_set.member_count = 2;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'production QC capability requires its exact succeeded Job/slot/Receipt/set closure';
    END IF;

    SELECT jsonb_agg(jsonb_build_object(
        'artifact_type', expected.artifact_type,
        'logical_id', expected.logical_id,
        'revision', 1,
        'content_hash', expected.content_hash,
        'payload_json', expected.payload,
        'scope', jsonb_build_object(
            'namespace', capability.namespace,
            'kind', capability.scope_kind, 'key', capability.scope_key
        )
    ) ORDER BY expected.ordinal) INTO expected_manifest
      FROM (VALUES
        (0, 'production_qc_collector_measurement', 'measurement',
         capability.measurement_member_sha256, measurement),
        (1, 'production_qc_collector_capability', 'decision',
         capability.capability_member_sha256, decision_document)
      ) AS expected(ordinal, artifact_type, logical_id, content_hash, payload);

    SELECT jsonb_agg(jsonb_build_object(
        'artifact_type', artifact.artifact_type,
        'logical_id', artifact.logical_id,
        'revision', artifact.revision,
        'content_hash', artifact.content_hash,
        'payload_json', artifact.payload_json,
        'scope', jsonb_build_object(
            'namespace', artifact.namespace,
            'kind', artifact.scope_kind, 'key', artifact.scope_key
        )
    ) ORDER BY member.ordinal) INTO actual_manifest
      FROM runtime.artifact_set_members AS member
      JOIN runtime.artifacts AS artifact
        ON artifact.artifact_id = member.artifact_id
       AND artifact.artifact_set_id = member.artifact_set_id
      JOIN runtime.artifact_sets AS artifact_set
        ON artifact_set.artifact_set_id = artifact.artifact_set_id
       AND artifact_set.job_id = artifact.job_id
     WHERE member.artifact_set_id = capability.artifact_set_id
       AND member.ordinal IN (0, 1);

    IF runtime.canonical_json_ascii(actual_manifest) IS DISTINCT FROM
            runtime.canonical_json_ascii(expected_manifest)
       OR actual_set_hash IS DISTINCT FROM
            runtime.production_qc_capability_json_sha256(expected_manifest)
       OR (SELECT count(*) FROM runtime.artifact_set_members
            WHERE artifact_set_id = capability.artifact_set_id) <> 2
       -- Count artifacts too: an unlisted third member must not survive.
       OR (SELECT count(*) FROM runtime.artifacts
            WHERE artifact_set_id = capability.artifact_set_id) <> 2 THEN
        RAISE EXCEPTION 'production QC capability requires exactly two ordered members with exact payload/hash/set closure';
    END IF;
END $$;

CREATE FUNCTION runtime.assert_production_qc_capability_job(checked_job uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    job runtime.jobs%ROWTYPE;
    slot runtime.command_slots%ROWTYPE;
    capability runtime.production_qc_collector_capabilities%ROWTYPE;
    slot_count integer;
    receipt_count integer;
    output_count integer;
    capability_count integer;
    lineage text;
    key_prefix text;
BEGIN
    SELECT * INTO job FROM runtime.jobs WHERE job_id = checked_job;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF job.job_key NOT LIKE 'autocut_production_qc_collector_validator:%'
       AND NOT EXISTS (
            SELECT 1 FROM runtime.command_slots
             WHERE job_id = checked_job
               AND (command_name = 'AcceptProductionRenderQcCollectorCapability@1'
                    OR idempotency_key LIKE 'production-qc-collector-capability:%')
       ) AND NOT EXISTS (
            SELECT 1 FROM runtime.artifacts
             WHERE job_id = checked_job
               AND (scope_kind = 'production_qc_collector_capability'
                    OR artifact_type IN ('production_qc_collector_measurement',
                                         'production_qc_collector_capability'))
    ) THEN
        RETURN;
    END IF;
    -- Serialize competing closures for this authority Job. In READ COMMITTED
    -- the queries below then see a concurrent inserter's committed rows too.
    SELECT * INTO job FROM runtime.jobs WHERE job_id = checked_job FOR NO KEY UPDATE;
    IF job.profile <> 'authority' OR job.job_key !~
        '^autocut_production_qc_collector_validator:[a-z][a-z0-9._-]{0,127}:[0-9a-f]{64}:[0-9a-f]{64}:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'production QC capability validator Job identity is not exact';
    END IF;
    lineage := substring(job.job_key from length('autocut_production_qc_collector_validator:') + 1);
    key_prefix := 'production-qc-collector-capability:'
        || split_part(lineage, ':', 1) || ':'
        || split_part(lineage, ':', 2) || ':'
        || split_part(lineage, ':', 3) || ':';
    SELECT count(*) INTO slot_count FROM runtime.command_slots WHERE job_id = checked_job;
    IF slot_count > 1 OR (job.state IN ('succeeded', 'denied', 'failed') AND slot_count <> 1) THEN
        RAISE EXCEPTION 'production QC capability validator requires exactly one terminal command';
    END IF;
    SELECT * INTO slot FROM runtime.command_slots WHERE job_id = checked_job;
    IF FOUND AND (
        slot.command_name <> 'AcceptProductionRenderQcCollectorCapability@1'
        OR slot.execution_kind <> 'deterministic'
        OR left(slot.idempotency_key, length(key_prefix)) <> key_prefix
        OR substring(slot.idempotency_key from length(key_prefix) + 1) !~ '^[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'production QC capability validator command identity is not exact';
    END IF;
    SELECT count(*) INTO receipt_count FROM runtime.command_receipts
     WHERE command_slot_id = slot.command_slot_id AND outcome = slot.state;
    IF slot.state IN ('succeeded', 'denied', 'failed') AND receipt_count <> 1 THEN
        RAISE EXCEPTION 'production QC capability terminal slot requires its matching Receipt';
    END IF;
    IF job.state IN ('succeeded', 'denied', 'failed') AND slot.state IS DISTINCT FROM job.state THEN
        RAISE EXCEPTION 'production QC capability terminal Job/slot outcome mismatch';
    END IF;
    SELECT count(*) INTO output_count FROM runtime.artifact_sets WHERE job_id = checked_job;
    SELECT count(*) INTO capability_count FROM runtime.production_qc_collector_capabilities
     WHERE command_slot_id = slot.command_slot_id;
    IF job.state = 'succeeded' THEN
        IF output_count <> 1 OR capability_count <> 1 THEN
            RAISE EXCEPTION 'production QC capability succeeded validator requires exactly one accepted row/set';
        END IF;
        SELECT * INTO capability FROM runtime.production_qc_collector_capabilities
         WHERE command_slot_id = slot.command_slot_id;
        PERFORM runtime.assert_production_qc_capability_row(capability);
    ELSIF slot.state = 'succeeded' OR output_count <> 0 OR capability_count <> 0
       OR EXISTS (SELECT 1 FROM runtime.artifacts WHERE job_id = checked_job) THEN
        RAISE EXCEPTION 'production QC capability non-success validator cannot own accepted output';
    END IF;
    -- A running Job with a rejected slot and no outputs is intentionally legal:
    -- the existing rejection writer terminalizes the slot, not the authority Job.
END $$;

-- Pending identities must not be moved out of the protected namespace before
-- the deferred checker runs. Existing terminal/record mutation guards remain.
CREATE FUNCTION runtime.guard_production_qc_capability_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'jobs' THEN
        IF OLD.job_key LIKE 'autocut_production_qc_collector_validator:%'
           AND (OLD.job_id, OLD.job_key, OLD.profile) IS DISTINCT FROM
               (NEW.job_id, NEW.job_key, NEW.profile) THEN
            RAISE EXCEPTION 'production QC capability validator Job identity is immutable';
        END IF;
    ELSE
        IF (OLD.command_name = 'AcceptProductionRenderQcCollectorCapability@1'
            OR OLD.idempotency_key LIKE 'production-qc-collector-capability:%')
           AND (OLD.command_slot_id, OLD.job_id, OLD.command_name,
                OLD.idempotency_key, OLD.request_hash, OLD.execution_kind)
               IS DISTINCT FROM
               (NEW.command_slot_id, NEW.job_id, NEW.command_name,
                NEW.idempotency_key, NEW.request_hash, NEW.execution_kind) THEN
            RAISE EXCEPTION 'production QC capability validator slot identity is immutable';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER production_qc_capability_job_identity_guard
BEFORE UPDATE ON runtime.jobs FOR EACH ROW
EXECUTE FUNCTION runtime.guard_production_qc_capability_identity();
CREATE TRIGGER production_qc_capability_slot_identity_guard
BEFORE UPDATE ON runtime.command_slots FOR EACH ROW
EXECUTE FUNCTION runtime.guard_production_qc_capability_identity();

-- Route every participating surface to the same final-state checks. Resolve
-- both old and new references so moving a mutable slot cannot strand outputs.
CREATE FUNCTION runtime.check_production_qc_capability_closure()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    reference jsonb;
    checked_job uuid;
    capability runtime.production_qc_collector_capabilities%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME = 'production_qc_collector_capabilities' AND TG_OP <> 'DELETE' THEN
        SELECT * INTO capability FROM runtime.production_qc_collector_capabilities
         WHERE scope_key = NEW.scope_key;
        IF FOUND THEN
            PERFORM runtime.assert_production_qc_capability_row(capability);
        END IF;
    END IF;
    FOR reference IN
        SELECT value FROM jsonb_array_elements(jsonb_build_array(
            CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) ELSE NULL END,
            CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) ELSE NULL END
        ))
    LOOP
        FOR checked_job IN
            SELECT (reference->>'job_id')::uuid
            UNION
            SELECT job_id FROM runtime.command_slots
             WHERE command_slot_id = (reference->>'command_slot_id')::uuid
            UNION
            SELECT job_id FROM runtime.artifact_sets
             WHERE artifact_set_id IN (
                (reference->>'artifact_set_id')::uuid,
                (reference->>'result_artifact_set_id')::uuid
             )
            UNION
            SELECT slot.job_id FROM runtime.command_receipts AS receipt
              JOIN runtime.command_slots AS slot USING (command_slot_id)
             WHERE receipt.receipt_id = (reference->>'receipt_id')::uuid
        LOOP
            PERFORM runtime.assert_production_qc_capability_job(checked_job);
        END LOOP;
    END LOOP;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER production_qc_capability_closure_from_job
AFTER INSERT OR UPDATE OR DELETE ON runtime.jobs
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_production_qc_capability_closure();
CREATE CONSTRAINT TRIGGER production_qc_capability_closure_from_slot
AFTER INSERT OR UPDATE OR DELETE ON runtime.command_slots
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_production_qc_capability_closure();
CREATE CONSTRAINT TRIGGER production_qc_capability_closure_from_receipt
AFTER INSERT OR UPDATE OR DELETE ON runtime.command_receipts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_production_qc_capability_closure();
CREATE CONSTRAINT TRIGGER production_qc_capability_closure_from_set
AFTER INSERT OR UPDATE OR DELETE ON runtime.artifact_sets
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_production_qc_capability_closure();
CREATE CONSTRAINT TRIGGER production_qc_capability_closure_from_artifact
AFTER INSERT OR UPDATE OR DELETE ON runtime.artifacts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_production_qc_capability_closure();
CREATE CONSTRAINT TRIGGER production_qc_capability_closure_from_member
AFTER INSERT OR UPDATE OR DELETE ON runtime.artifact_set_members
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_production_qc_capability_closure();
CREATE CONSTRAINT TRIGGER production_qc_capability_closure_from_row
AFTER INSERT OR UPDATE OR DELETE ON runtime.production_qc_collector_capabilities
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION runtime.check_production_qc_capability_closure();

-- Validate all existing rows and validator Jobs under the same write lock as
-- trigger installation. Invalid 0059 history aborts the entire migration; no
-- records are rewritten or grandfathered in.
DO $$
DECLARE
    capability runtime.production_qc_collector_capabilities%ROWTYPE;
    checked_job uuid;
BEGIN
    FOR capability IN SELECT * FROM runtime.production_qc_collector_capabilities LOOP
        PERFORM runtime.assert_production_qc_capability_row(capability);
    END LOOP;
    FOR checked_job IN SELECT job_id FROM runtime.jobs LOOP
        PERFORM runtime.assert_production_qc_capability_job(checked_job);
    END LOOP;
EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION '0060 refuses invalid production QC capability history: %', SQLERRM;
END $$;

-- Also prevent concurrent distinct claims under the same authority Job when
-- callers use a repeatable-read snapshot. The deferred count alone cannot see
-- a competing transaction's newly inserted slot in that isolation level.
CREATE UNIQUE INDEX production_qc_capability_one_validator_slot_per_job
    ON runtime.command_slots (job_id)
    WHERE command_name = 'AcceptProductionRenderQcCollectorCapability@1';

COMMIT;
