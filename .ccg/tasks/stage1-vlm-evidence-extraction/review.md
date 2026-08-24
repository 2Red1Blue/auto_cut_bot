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
