-- Persist one closed diagnostic for a deterministic historical VLM batch
-- incompatibility. Existing Receipts remain valid and immutable with NULLs.
BEGIN;

LOCK TABLE runtime.pipeline_run_receipts IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE runtime.pipeline_run_receipts
    ADD COLUMN failure_code text,
    ADD COLUMN failure_detail jsonb,
    ADD CONSTRAINT pipeline_run_receipts_failure_pair_check CHECK (
        (failure_code IS NULL AND failure_detail IS NULL)
        OR (failure_code IS NOT NULL AND failure_detail IS NOT NULL)
    ),
    ADD CONSTRAINT pipeline_run_receipts_isolation_detail_check CHECK (
        failure_code IS NULL
        OR (
            outcome = 'failed'
            AND failure_code = 'VLM_BATCH_CHILD_REQUEST_POLICY_MISMATCH'
            AND jsonb_typeof(failure_detail) = 'object'
            AND failure_detail ?& ARRAY[
                'declared_episode_count',
                'distinct_policy_count',
                'ordered_policy_hashes_sha256',
                'schema_version'
            ]::text[]
            AND failure_detail - ARRAY[
                'declared_episode_count',
                'distinct_policy_count',
                'ordered_policy_hashes_sha256',
                'schema_version'
            ]::text[] = '{}'::jsonb
            AND jsonb_typeof(failure_detail -> 'declared_episode_count') = 'number'
            AND (failure_detail ->> 'declared_episode_count')::bigint > 0
            AND jsonb_typeof(failure_detail -> 'distinct_policy_count') = 'number'
            AND (failure_detail ->> 'distinct_policy_count')::bigint > 1
            AND (failure_detail ->> 'distinct_policy_count')::bigint
                <= (failure_detail ->> 'declared_episode_count')::bigint
            AND failure_detail ->> 'ordered_policy_hashes_sha256'
                ~ '^sha256:[0-9a-f]{64}$'
            AND failure_detail ->> 'schema_version'
                = 'vlm-batch-policy-mismatch-v1'
        )
    );

COMMENT ON COLUMN runtime.pipeline_run_receipts.failure_code IS
    'Closed runtime isolation code; NULL for ordinary Kernel-backed outcomes.';
COMMENT ON COLUMN runtime.pipeline_run_receipts.failure_detail IS
    'Immutable bounded diagnostic for the exact isolated persisted-input mismatch.';

COMMIT;
