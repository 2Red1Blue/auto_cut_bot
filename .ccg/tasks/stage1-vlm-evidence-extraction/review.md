# Layer 1 review — Root Media Evidence and VLM contracts

## Scope

- Root media evidence contract: frame PTS, shot/scene boundaries, audio sample boundaries, transcript, VAD, visual validity, subtitle timing.
- Provider-neutral VLM window, proxy timeline mapping, request identity, strict response parser, and global semantic-core ownership.
- No provider invocation, persistence migration, semantic-chain adapter, physical endpoint compiler, render, or publication behavior is approved by this checkpoint.

## Independent review round 1 — No-Go

The first independent review found two P1 gaps:

1. `WindowManifest` frame samples were not required to be members of the root `FramePtsIndexSet`.
2. `parse_vlm_response` derived ownership from one window and did not require the complete `WindowManifestSet`.

Both gaps could have made provenance and unique ownership conventions rather than enforced contracts.

## Repairs

- `WindowManifest` now requires the exact `FramePtsIndexSet`, checks source/hash/clock/time-base identity, and rejects every sampled source PTS not present in that index.
- The frame-index-set hash is transitively bound by the manifest and `VlmRequestIdentity`.
- `VlmRequestIdentity` binds the complete `WindowManifestSet` hash.
- Identity construction, identity verification, and parsing all require membership in the exact manifest set.
- `core_owned` is derived only through global `select_core_owner`; a single window cannot self-assign ownership.
- Added negative VFR-membership, forged-hash, and overlapping-context ownership tests.

## Independent review round 2 — Go

The second read-only review confirmed both P1 findings are closed and found no new P0/P1 issue.

## Verification

- Combined targeted suite: `118 passed`.
- VLM suite: `35 passed`.
- Ruff: passed.
- BasedPyright: `0 errors, 0 warnings, 0 notes`.
- `git diff --check`: passed.
- Known unrelated warning: the repository pytest configuration declares an `asyncio_mode` option not recognized in this environment.

## Decision

**Go for the Layer 1 checkpoint only.** The generated values remain coarse semantic evidence and cannot be consumed as physical edit endpoints. Store/attempt atomicity, real provider invocation, A/V exact pairing, and end-to-end admission require later checkpoints and independent review.

## Layer 2 review — Store lifecycle and exact A/V compiler

### Findings and repairs

- Normal command completion previously terminalized the whole Job. It now closes only its slot and Receipt; only an exact `FinalizeRunOutcome` command can atomically terminalize the Job.
- The first generation-attempt implementation bound only an opaque request hash. Main-agent adversarial review required and added durable `provider_id`, `provider_idempotency_key`, and exact request-payload `BlobRef` identity. These fields are immutable and the payload must be claimed by the same Job.
- Request and response blobs are content-addressed, byte/hash/length checked, immutable, and locator-free at the Kernel API.
- An ambiguous provider timeout leaves the command slot running and the attempt `indeterminate`; the same attempt may only reconcile and cannot dispatch again.
- The exact compiler now enumerates four endpoints from authoritative frame/sample evidence. VLM types are rejected at this boundary.
- A zero subtitle-clearance floor is rejected for the production A/V policy; detector timing error and the positive policy floor are conjunctive.

### Verification

- Store/unit/migration and A/V targeted suite: `73 passed`.
- Real PostgreSQL 16 Store integration suite after the main-agent repairs: `41 passed`.
- Ruff: passed.
- BasedPyright: `0 errors, 0 warnings, 0 notes`.
- Podman database `autocut` was created in `ac_postgres`, owned by the existing `ac_user`, and migrations `0001` through `0003` were applied. The legacy `ac_db` database was not modified.

### Decision

**Go for the Layer 2 checkpoint.** This does not yet approve a real provider adapter, VLM command orchestration, semantic-chain consumption, rendering, or publication.
