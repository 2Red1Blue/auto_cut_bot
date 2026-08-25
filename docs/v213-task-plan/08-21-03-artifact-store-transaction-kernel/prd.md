# Minimal Persistence Core — Artifact / Command / Receipt

## Goal

Deliver the first runnable Pipeline persistence boundary: an idempotent
Command claim and a single PostgreSQL transaction that durably records its
immutable ArtifactSet, Artifact revisions/members/heads and linked immutable
Receipt. This is the prerequisite for Media Preflight, not a complete
Recovery/Outbox/Registry implementation.

## Requirements

- The implementation target is `feat/v213-contract-codegen`. The unmerged
  Store branch is reference material only; it must not be merged unchanged.
- Empty-database migrations create the `storage`, `authority` and `execution`
  relations actually used by the public Store API. Tests must not create a
  private simplified `execution` table to make a broken migration appear to
  work.
- An immutable Artifact chain is scope-aware. Different namespace/scope
  chains may use the same business `logical_id` and revision; a duplicate
  revision in the same chain is rejected.
- A `command_slot`, `artifact_set`, `command_receipt` and every exact
  Artifact/Set reference are linked by PostgreSQL foreign keys and deferred
  commit-time consistency checks. A Runtime has no raw table-write API.
- The only public write surface is semantic: claim a command, commit success,
  commit rejection, or read an exact outcome. Generic execution-row writes
  and raw cursor escapes are forbidden.
- Initial and subsequent head contention have stable typed outcomes; a normal
  initial-head race must not leak an unclassified database unique violation.
- Blob identity is retained only as an Artifact prerequisite. Blob GC,
  Outbox, Admission evaluation, RecoveryLedger, Registry interpretation and
  projections are deferred to later slices and must not be faked here.

## Acceptance Criteria

- [ ] An empty disposable PostgreSQL applies the runtime-core migration and
  contains every relation used by the API; an intentional migration failure
  rolls back cleanly.
- [ ] A command claim is idempotent for the same canonical intent and reports
  a typed conflict for the same key with changed command, request or scope;
  a conflicting request does not invoke a handler.
- [ ] A success transaction atomically persists its committed Set, members,
  exact heads, receipt and slot current-receipt pointer. A rejection records
  an immutable diagnostic receipt without manufacturing a successful result.
- [ ] Database constraints reject a missing/wrong Set–Slot–Receipt relation,
  a wrong Artifact/Set hash pair, cross-chain parent/head attachment, scope
  bytes/hash mismatch and incomplete success/rejection reference pairs.
- [ ] Two first writers of one chain yield exactly one success and one
  `StaleHeadError`; two writers of different chains with the same alias can
  both succeed when their namespaces/scopes differ.
- [ ] Fault injection at any point before commit leaves no partial committed
  Set/Artifact/Receipt/Slot state. A commit-ack loss is reconciled by exact
  idempotency-key/readback, never by a changed replay.
- [ ] A disposable PostgreSQL role representing Pipeline/Agent Runtime is
  denied DML on `authority` and `execution`; only the kernel command API owns
  writes.

## Explicitly Deferred

- Recovery budgets, retries and exhaustion transitions.
- Transactional Outbox, external effects, blob retain/GC and projections.
- Registry/Profile interpretation and Admission rule evaluation.
- Stage semantics, media probing, rendering, quality control and publication.

## Planning Record

The concrete schema, transaction API, migration ordering and test matrix are
in `research/product-first-minimal-persistence-design.md`. That document is
the implementation handoff for this task; it supersedes the previous broad
first-slice plan where they differ.
