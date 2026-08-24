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

## Layer 3 review — Durable VLM command and semantic adapter

### Scope and guarantees

- Added a provider-neutral `VlmProviderPort`; adapters receive the exact immutable proxy bytes and canonical request payload, but cannot parse observations, assign ownership, persist Artifacts, or select physical edit endpoints.
- `GenerateVlmEvidenceCommand` now owns the durable reserve/dispatch/respond/reconcile/commit state machine. An ambiguous dispatch is persisted as `indeterminate`; replay reconciles the same provider idempotency key and never dispatches a second request.
- Request payload, proxy bytes, raw provider response, provider identity, idempotency key, and provider request ID are durably bound to the same Job and generation attempt.
- A successful transaction commits exactly one ArtifactSet containing request record, response record, and `vlm_observation_set`. Invalid provider output preserves the raw response Blob but cannot create semantic evidence.
- The one-way semantic adapter admits only globally `core_owned` observations from the exact committed Artifact payload. Provider summary text remains explicitly untrusted, and all VLM intervals retain `semantic_precision=coarse_only`.
- Production semantic admission remains closed until its independent evaluators are connected. This checkpoint does not approve a real provider adapter or claim a real video/VLM end-to-end run.

### Adversarial checks and repairs

- Moved immutable proxy loading before the durable dispatch transition so a local Blob failure cannot falsely record that an external call may have happened.
- Matched ArtifactSet hashing to the Store's exact UTF-8 canonicalization (`ensure_ascii=False`); a Chinese-summary integration fixture prevents encoding drift.
- Persisted `provider_request_id` for explicit terminal provider failure instead of losing the external correlation identity.
- Verified successful command replay reparses the persisted raw response without invoking the provider.

### Verification

- Default VLM/semantic/Store suite: `53 passed, 45 skipped` (PostgreSQL tests intentionally skipped without a DSN).
- Disposable PostgreSQL 16 integration suite: `45 passed`.
- Ruff: passed.
- BasedPyright: `0 errors, 0 warnings, 0 notes`.
- `git diff --check`: passed.

### Decision

**Go for the Layer 3 Kernel checkpoint.** The next gate is a typed adapter for the active non-legacy VLM implementation plus a real source-window/proxy/model run. Fake-provider success is not accepted as completion of the VLM stage.

## Layer 4 review — Real Qwen video adapter and live smoke

### Implementation boundary

- Added the cut_bot-side `QwenVlmProvider`; the shared Kernel remains provider-neutral.
- The adapter submits Base64 MP4 through Qwen Chat Completions with SDK retries disabled, a 20 MiB pre-network cap, closed request parameters, sanitized terminal errors, and no reconciliation redispatch.
- Added a versioned prompt pack containing the complete response Schema and exact Kernel frame anchors.
- Added `IdentityProxyWindowBuilder` for the narrow case where the submitted MP4 is itself the Source. It collects real decoded PTS and sampled-frame hashes; it cannot be used for a transcoded proxy.

### Adversarial live sequence

1. Live request v1 reached Qwen and returned meaningful semantic content, but used legacy-like flat fields. Kernel rejected it with `MISSING_RESPONSE_FIELD`; no observation Artifact was created.
2. Live request v2 tried strict provider-side `json_schema`. Qwen's multimodal endpoint rejected the request with HTTP 400. Kernel recorded a terminal provider failure and created no ArtifactSet.
3. The adapter was corrected to the officially supported multimodal `json_object` path, with the complete Schema included in the versioned prompt. Live request v3 succeeded and committed four coarse observations.

The two failed Attempts remain durable audit records; neither was overwritten or converted into success.

### Live acceptance evidence

- Source: existing authorized `w001-480p.mp4` test window (4.65 MB).
- Provider/model: `qwen-openai-chat` / `qwen3.7-plus`.
- Durable result: Attempt `committed`, provider request ID retained, exactly one `vlm_request_record`, one `vlm_response_record`, and one `vlm_observation_set` in the committed ArtifactSet.
- Parsed result: four observations, all `core_owned=true` and `semantic_precision=coarse_only`.
- Replay used a Provider implementation that raises on both `dispatch` and `reconcile`; replay still succeeded from the immutable raw response and produced four candidates/four Narrative nodes. This proves no hidden second provider call.

### Automated verification

- Runtime adapter tests (including real ffmpeg/ffprobe identity-window construction): `6 passed`.
- VLM/semantic targeted suite: `59 passed, 4 skipped` without a PostgreSQL DSN.
- Disposable PostgreSQL Store/VLM suite: `45 passed`.
- Ruff: passed.
- BasedPyright: `0 errors, 0 warnings, 0 notes`.

### Decision

**Go for the real Qwen VLM test slice.** This is not production VLM completion: HTTP Pipeline composition, transcoded proxy timeline proof, production semantic Admission, local ASR/VAD conjunction, and Ark provider file-id lifecycle remain open.
