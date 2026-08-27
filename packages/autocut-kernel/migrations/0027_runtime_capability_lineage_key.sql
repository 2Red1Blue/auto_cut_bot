-- Runtime calibration capabilities are immutable per measured identity *and*
-- authority lineage.  A protected profile/Registry upgrade may retain the same
-- physical timing identity but must be able to persist a new accepted record.

BEGIN;

LOCK TABLE runtime.runtime_calibration_capabilities IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE runtime.runtime_calibration_capabilities
    DROP CONSTRAINT runtime_calibration_capabilities_pkey,
    ADD PRIMARY KEY (
        runtime_capability_id,
        timing_compatibility_sha256,
        runtime_measurement_identity_sha256,
        profile_source_sha256,
        registry_snapshot_sha256
    );

COMMIT;
