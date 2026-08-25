# Implementation Plan

> **Supersession (2026-08-23):** execute the file scope, migration order,
> semantic Store API and acceptance matrix in
> `research/product-first-minimal-persistence-design.md`. Do not implement the
> broad Blob/Outbox/Recovery scope described below in the first runnable
> persistence slice.

1. Before changing implementation, verify authority baseline
   `baf667f797ac7d4eb34e48caad8047fb07433c9c`, total-contract raw SHA-256
   `c34af7451919ad9a895644b40136062834b7ba9e857139f10b61f7dc51be67e9`, and
   physical-schema raw SHA-256
   `6adbb95e531184d5229351d0cfec6250b415db0b9d4ef26be077a87cda0cd674`; then
   complete the Task 03 source map.  Deny entry on any mismatch, missing
   provenance, or accepted F1/later supersession; re-run authority/context and
   predecessor admission rather than implementing against a stale plan.
2. Add migration tooling and schemas `authority`/`storage` with only the first
   vertical-slice relations; include exact composite keys/FKs and no business
   SQL defaults.
3. Implement dependency-free Store request/value types in `autocut_kernel` and
   a PostgreSQL adapter behind that interface.
4. Implement one-transaction ArtifactSet commit, canonical lock order and
   exact head CAS/fencing.
5. Add container-backed integration tests for empty migration, rollback,
   cross-namespace identities, parent/head attacks, concurrent CAS and
   commit-response-lost readback.
6. Verify role grants deny Runtime direct writes; run migration/rollback,
   focused integration tests, lint/type checks and independent review.
7. Commit this vertical slice separately. Do not start Blob/Outbox/Recovery
   implementation until its dependencies and schema subsets are planned.

## Stop conditions

- If PostgreSQL is unavailable, retain unit tests and mark integration work
  pending; do not substitute SQLite as proof.
- If an Envelope field or Registry scope rule is unresolved, represent it as a
  verified opaque canonical payload only where the physical schema permits;
  do not invent business defaults.
- If a required FK cannot express the documented exact identity, stop and
  revise the physical design before writing a weaker constraint.
