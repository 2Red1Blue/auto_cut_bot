# Local speech foundations and integration

Target: task05's local-window-command-implementation-plan.md, sections 3–7.
The existing physical prelude, measured audio facts, mapped extraction and
BUSY wire are implemented. The new exact terminal reader is independently
reviewed separately. No Claude calls: the user explicitly requested native
subagents after exhausting Claude quota. Reuse existing workers, no spawning.

## Ownership

- calibration_contract: local lifecycle/request contract and its pure tests;
  carry exact measured audio facts through the Source resolver without changing
  old canonical payloads. Do not modify Store/Postgres or existing HTTP adapter
  until the root assigns a specific integration change.
- calibration_migration: after terminal-reader review, mapped local-window
  assessment and tests only. No changes to old v1 assessment/codec semantics.
- review_calibration_migration: new shadow_local_calibration.py,
  shadow_local_calibration_projection.py and their two pure test files.
- root: shared Kernel producer port and the existing HTTP adapter's result/BUSY
  type imports, integration tests, documentation, scoped commits. Move existing
  result content rather than invent another wire grammar. Preserve the adapter's
  LocalMediaToolError compatibility; Commands catch a Kernel-owned BUSY base and
  must still independently replay its raw proof against the expected request.

Every worker is sharing the worktree. Do not revert another owner's changes;
do not touch the protected dirty config, legacy packages, service deployment,
models, PostgreSQL, native codecs or other owners' files. Use pure/synthetic
tests on Mac. Read complete files before editing. No commits by workers.

## Required behavior

1. Local lifecycle keys bind the full parent, physical predecessor, candidate,
   policies, installed-profile expectations, decoder and explicit budgets.
   Expansion starts at zero and attempt at one; no caller idempotency key.
   A content DTO cannot authorize a retry or become an accepted profile.
2. Preserve the old resolved Source payload/hash. New local requests separately
   bind measured AudioStreamFacts; absent facts remain absent, never default
   to mono/48 kHz or inverse time_base.
3. Assessment uses a verified continuous presentation map and real raw-bound
   local speech coverage. It cannot compare native video/audio tick integers,
   invent a complete sentence or accept a caller-provided expansion decision.
   The explicit adaptive video-clock guard is mapped before adding separate
   ASR/VAD calibrated audio-tick bounds and the certificate's audio snap bound.
   Snap calibration alone does not describe speech timestamp error. Suppress
   source-edge touch only when both the video and the audio extraction endpoint
   are their own source endpoints; a remaining audio tail cannot become closed
   merely because the video cannot expand any further.
4. Shadow-local case identity includes extraction, independent window anchors,
   model/service/policy identities and Source provenance, but no future Record,
   accepted bound, Receipt or derived request hash. A request's binding is the
   case hash. Service profile hash and native port identity are distinct.
5. Shadow projection reuses the existing raw window decoder/projector, checks
   exact request/report/profile identity and deterministic one-to-one ordered
   anchor alignment. Never clip whole-source anchors or fill fake nonzero error.
   Real zero error is valid measurement; no claim of accepted calibration.
6. Existing full-source and normal-window wire behavior must remain unchanged.
   Full-source calibration does not authorize the new extraction path. Runtime
   activation waits for explicit local profile identity and independent
   persisted local calibration acceptance.

## Verification and completion boundary

Each slice needs negative mutation tests, scoped Ruff/type checks and review by
a native agent that did not author it. Root independently runs combined pure
regressions and commits accepted slices promptly. These foundational slices do
not finish task05: the durable child, exact chain reader, parent-owned admissions,
shadow-local service/persistence/acceptance, Runtime and real desktop acceptance
remain tracked until implemented and exercised.
