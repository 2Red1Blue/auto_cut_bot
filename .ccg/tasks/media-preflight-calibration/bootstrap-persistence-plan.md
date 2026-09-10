# Durable shadow-bootstrap collection plan

## Goal

Turn the proven `r0-ep01` HTTP call into an idempotent, durable Kernel command
without making untrusted observations available to calibration, MediaPreflight,
Stage 4, render, or release.

## Existing authority reused

No schema migration is required. The command will reuse:

- succeeded `PersistedWholeSeriesSourceManifest` plus an exact episode `BlobRef`;
- `materialize_immutable_blob` for a private bounded source lease;
- `put_immutable_blob` for the raw HTTP response and projected JSON;
- generic deterministic `claim_command`, `commit_command_success`, and terminal
  rejection, which atomically create the ArtifactSet and Receipt.

It must use fresh command/artifact names, never a reserved calibration or
timed-media command name.

## Command boundary

`CollectShadowBootstrapObservationCommand@1` accepts a successful source
manifest reference, episode identity, explicit source clock/range and transfer
limits. It:

1. rereads and verifies the source manifest/episode BlobRef;
2. claims one deterministic command key;
3. materializes the exact sealed source BlobRef;
4. calls only the bootstrap HTTP port once—unknown result is not blindly retried;
5. stores raw response bytes and strict Kernel projection as immutable blobs;
6. commits exactly two observation artifacts plus a success Receipt atomically.

The two payloads contain `trust_status=untrusted`,
`authority_eligible=false`, and `independent_anchor_count=0`.  Neither exposes
a timed-media finalizer type or a Recipe/Render/Release reference.

## Test matrix

- fresh collection, receipt replay, and exact source substitution denial;
- response/request/hash/size drift and malformed response denial;
- source/body materialization limits and no duplicate dispatch on replay;
- PostgreSQL readback proving raw/projection blob integrity and atomic Receipt;
- type-level barrier: bootstrap result cannot be given to calibration/preflight
  or Stage 4 APIs.
