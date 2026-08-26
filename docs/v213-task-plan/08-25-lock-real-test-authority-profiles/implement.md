# Implementation Plan

## Phase 1 — profile grammar only

Freeze the closed source grammar before authoring any production profile bytes.
This phase owns only:

- `registry/authority_profiles.py` and its public exports;
- the three governance JSON Schema mirrors plus `schema-index.yaml`;
- focused authority tests for closed fields, duplicate keys, fixed identities,
  non-zero hashes, exact capability matrices and shadow-to-run closure.

It must not create `governance/registry-sources/profiles/*.json`, update the
authority inventory/lock, touch runtime composition, or invent model/corpus
hashes.  Passing this phase proves only that the grammar is executable.

## Phase 2 — CalibrationRecord and independent validation

Add one aggregate immutable CalibrationRecord with distinct ASR and VAD child
record hashes.  Existing timed-speech registry entries continue to receive the
two child hashes; the run profile references the aggregate record and the
independent validation Receipt.  The validator must re-read immutable raw
evidence, re-decode it, recompute integer observations and bounds, and commit
accepted record/Receipt/anchor atomically through a protected Store owner.

Measurement and validation use different run identities.  Denied or
indeterminate validation never produces a committed record and cannot be
referenced by a run profile.

As a Phase-2 entry repair, upgrade the committed measurement manifest from v2
to v3 so every ordered member persists its complete canonical `raw_context`,
expected-anchor reference and raw BlobRef.  Validation must reject v2 instead
of silently trusting its projection.  Persisted candidate construction and
accepted authority assembly are separate APIs; only the validator command may
assemble the accepted receipt after raw-byte recomputation.

## Phase 3 — measured sources and authority publication

Only after Phases 1–2 pass may the local machine produce real, non-zero model,
service, policy, corpus and calibration identities.  Author and publish the
shadow source through source A, inventory-only B and generated-lock-only C.
After the accepted record exists, author the successor local-run and Stage-1
narrative sources and publish a second A→B→C chain.

## Phase 4 — packaged bootstrap, HTTP injection and real local run

Compile the verified Git blobs into an installed authority-context resource,
bootstrap it through the authority-only Store command, replay the durable
anchor, then inject the verified snapshot into standard HTTP composition.
The runtime cannot read Git, `tools/`, the checkout, caller profile data or
ordinary environment policy.  Run the current drama locally and retain
Receipts/ArtifactSets/Render/QC evidence while external publication remains
unreachable.

## Detailed ordered work

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
