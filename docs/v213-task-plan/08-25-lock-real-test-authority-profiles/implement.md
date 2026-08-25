# Implementation Plan

1. Define the Kernel-owned closed calibration-only raw-response envelope,
   invocation identity, CalibrationRecord member, independent validation
   receipt and deterministic measurement-bound algorithm.  The first record
   must be built from direct FunASR/FSMN output, never the ordinary response
   whose identity already contains a calibration record/bound. Add the shadow
   profile source grammar with selected model identities and
   source/identity/secret/zero-negative tests.
   Add the shadow-only staged measurement/recovery aggregate before a real
   invocation: expiring member/recovery leases, durable exact plan and staged
   raw evidence, indeterminate unknown outcomes, bounded recorded successor
   attempts, and transactional ArtifactSet finalization. Do not add generic
   command-slot reclaim or blindly repeat an unknown native invocation.
2. Obtain the tracked authority-child authorization, then publish the shadow
   sources through the A -> B -> C Git-lock chain using immutable Git blobs.
3. Implement/verify bounded shadow calibration, producing a source-bound,
   non-zero independently validated CalibrationRecord without enabling
   Pipeline HTTP.
4. Add successor `local_run_v1` and locked Stage-1 narrative sources only
   after they bind that exact record/receipt; publish their revision through a
   second A -> B -> C lock chain and reject stale/substituted/dirty sources.
5. Complete the authority build step that verifies A/B/C Git blobs and emits a
   closed packaged authority-context resource. Runtime reads only that resource,
   then checks the durable anchor; add terminal bootstrap conflict and
   retryable-failure receipts plus server snapshot injection. Keep Pipeline HTTP
   bootstrap-free.
6. Run tests and destructive PostgreSQL acceptance only against
   `ac_autocut_verify`, never `ac_db`.
7. Independently review source provenance, bootstrap replay/conflict and HTTP
   reachability; commit each coherent phase immediately.
8. Configure the real local environment, run one current-drama shadow job and
   inspect durable receipts plus semantic highlights. Stop before publication.

## Evidence and rollback

Collect lock/Git-chain hashes, CalibrationRecord identity/non-zero bounds,
verification-DB receipts and real-run ArtifactSet/highlighter evidence. Before
lock commit discard only unaccepted source work; after deployment stop the
worker and restore a prior verified snapshot without deleting authority data.
