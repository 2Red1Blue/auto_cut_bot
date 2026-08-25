# v2.1.3 Authority Implementation Plan

## Execution Rules

- Parent remains planning/integration owner; start one independently verifiable child at a time.
- Parent is cross-repository: `auto_cut_bot` owns governance/kernel/Agent runtime; `ac_auto_cut` owns Pipeline runtime and consumes a pinned kernel artifact.
- Do not start any implementation until the latest planning summary is explicitly approved.
- Phase -1 is sequential: `00` freezes authority/consumer-lock contracts, then `02` builds the exact wheel and materializes the first lock; both must complete before `01`.
- Each child loads v2.1.3 authority specs plus relevant Trellis code-spec; old code may be read only for inventory/fixtures until ledger admission.
- Every implementation child ends with contract tests, architecture lint, Trellis check, spec update and a scoped Git commit.
- Every child follows implementation design 12: freeze `TaskSnapshot`, run deterministic checks into `CheckReport`, then obtain a read-only `SupervisorDecision` for the exact candidate tree; gate/toolchain/contract hashes and stable finding fingerprints prevent stale cache and repair-budget reset.
- Local `allow` permits task completion/commit only. Push/release remains a separate optional profile and is denied unless its deployment-specific checks are configured and pass.
- High-risk children use GPT-5.6 Sol high/xhigh as owner; bounded frozen-contract implementation may use GPT-5.6 Terra high with Sol review; Spark is restricted to machine-verifiable leaf work.

## Product-first Delivery Order (approved 2026-08-23)

The program is no longer scheduled as a long, fully-complete contract ladder.
It is scheduled around the first executable unattended-publication path.  A
later producer may not delay a runnable downstream path when a bounded test
input can supply the same validated artifact shape.

1. **Minimal persistence core — Artifact / Command / Receipt.**  Complete the
   smallest PostgreSQL slice that can durably create a Job lineage, immutable
   Artifact revisions and sets, an idempotent Command, and its linked Receipt.
   It includes exact CAS, rollback and runtime write-permission denial.  It
   does *not* wait for the full Recovery, Blob lifecycle, Outbox, Registry or
   authority-amendment machinery.  Every foreign-key and scope relation used
   by this slice must exist in the migration; a test-only private schema is
   not an implementation.
2. **Media Preflight and exact Span compiler.**  Given a declared test Job and
   source media, persist MediaEvidence, compile valid spans with integer ticks
   and deterministic boundary evidence, and reject missing/unsafe sources.
   Stage 1--3 may be replaced only by an explicitly labelled test input
   adapter; it cannot silently manufacture semantic facts.
3. **Recipe → Render → Publication QC.**  Compile a complete Recipe, render
   it deterministically, and apply structural, media, editorial and release
   QC.  A per-video failure denies that video's publication; no partial Recipe
   or silently skipped Beat may render.
4. **Real test-environment end-to-end.**  Run the preceding path against a
   disposable PostgreSQL and a real non-production platform/test target.
   Prove retry/idempotency, render/QC rejection, durable reconciliation and
   external batch prepare/commit/query behavior before accepting it as a
   release candidate.  No production publish is enabled in this step.
5. **Stage 1--3 semantic chain.**  Replace the bounded test-input adapter with
   Narrative Graph, Story Design and Scripting.  Coverage, evidence,
   completion and failure propagation become upstream producers for the
   already-tested physical-edit path.
6. **Agent Runtime last.**  Add the nanobot-based Agent Runtime as another
   caller of the same Command boundary.  It receives neither a Store nor a
   publication bypass.  Conformance compares its command/artifact trace to
   the Pipeline path before it can participate in unattended publication.

## Supporting work and sequencing

- `00`, `02` and the useful generated contract/kernel work already integrated
  on `feat/v213-contract-codegen` remain prerequisites and are maintained in
  place; they are not a reason to postpone Step 1.
- Task `03` and the necessary bounded portion of `04` form Step 1 and are
  planned/repaired together.  The existing unmerged Store branch is evidence,
  not a merge candidate: its migration and linkage defects must be corrected
  on the integration branch before it is accepted.
- Tasks `05` and `06` form Step 2; Task `08` plus the rendering subset form
  Step 3; platform certification (`10`) supplies the external part of Step 4.
- Task `07` is deliberately after Step 4.  Task `09` is narrowed to the
  command/artifact conformance needed by Step 6 instead of blocking the
  Pipeline MVP.
- Work may run in parallel only when file ownership and inputs are independent.
  Integration is always into `feat/v213-contract-codegen`, with small local
  commits after each verified slice.  Branches are short-lived review
  candidates, never competing integration lines.

## Global Validation Commands/Checks

- Trellis task validation for active child and parent/child link integrity.
- Authority lock hash verification, protected-path policy and task diff allowlist.
- Contract source → generated Schema/Pydantic/Registry hash diff.
- AST import firewall good/base/bad corpus.
- Isolated `autocut_kernel` wheel import with legacy distributions absent.
- Reuse Ledger schema, source hash, allowed importer/symbol and contract test validation.
- PostgreSQL migration-from-empty/from-previous, CAS concurrency, crash-point and permission tests.
- Pipeline/Agent conformance trace comparison.
- G-CUT-CONF fixtures, four-layer QC and external batch atomic visibility certification.
- Upstream file/capability parity and branding/protocol allowlist checks.
- Candidate-tree audit for every task; `remote_base..candidate` history audit only when the push/release profile is requested.
- Baseline-failure attribution proof for every inherited failure; changed-scope failures remain blocking.

## Stop Conditions

- Authority spec and implementation design disagree.
- Any new import references legacy code without a prior permitted ledger entry.
- Root Trellis spec still grants old ArtifactBus/file-state implementation authority.
- Store/Admission/Publication write path is reachable from a Runtime adapter.
- External effect outcome is unknown and no durable reconcile owner exists.
- A child changes cross-child contracts without returning parent planning to review.

## Risky Boundaries

- `ac_auto_cut` has an empty private remote and `auto_cut_bot` has a public fork remote without protection. This does not block isolated local implementation, but both require clean-history/secret review and protected rules before first push/release.
- Both local repositories contain unrelated dirty user changes; no bootstrap task may stage, reset, move or rewrite them outside its exact allowlist.
- The current worktree contains extensive unrelated user changes; every child must stage and commit only owned files.
- Existing VLM IDs are provider/account/preprocess scoped and cannot be trusted from a bare ID or debug log.
- Python import isolation requires static, packaging and runtime-environment gates together.
