# Stage 1 preparation review — 2026-08-26

Scope: explicit Command execution kind and untrusted Stage 1 draft codec.
This review does not approve the inactive Stage 1 compiler or activate HTTP Stage 1.

## Findings fixed during implementation

1. The first migration placed the deferred-constraint flush after re-enabling
   the terminal-slot trigger. ENABLE itself is ALTER TABLE and must not precede
   the flush after the populated-table backfill. The implementation owner moved
   the flush immediately after UPDATE, before ENABLE and NOT NULL alteration.
   Static ordering checks and a written populated-database upgrade regression
   are required. Actual PostgreSQL upgrade execution remains remote.
   PostgreSQL's own explanation of pending trigger events and flushing:
   [upstream discussion](https://www.postgresql.org/message-id/3944.1312239528%40sss.pgh.pa.us).
2. The shadow-calibration successor uses a private raw command-slot INSERT,
   not CommandClaim. It also must explicitly write deterministic; otherwise
   the new no-default NOT NULL column breaks recovery. This producer is included
   in the same migration wave.
3. Existing SQL fixtures and the byte-returning cursor fixture needed the new
   column/query projection. Existing pre-0018 upgrade fixtures remain old-schema
   inputs intentionally; modern fixtures cannot omit the kind.
4. Stage 1 prompt content needs an independent byte budget, not only response
   and collection bounds. The codec rejects oversized input intact and exposes
   a canonical policy hash; no silent context trimming was introduced.
5. The populated-upgrade test initially omitted completed_at on historical
   terminal slots, violating the original schema before reaching 0018.
   The fixture now supplies timestamps only for succeeded/denied/failed.

## Verification boundary

Unit, static and fake-I/O tests are local. No local database, model service or
provider invocation is authorized for this work. PostgreSQL test files are
authored for the remote verification database; collecting them is not executing
or certifying them. The new draft is untrusted content, never a pass result,
coverage declaration or accepted merge.

## Final evidence

- Combined semantic/VLM/Store fake-I/O/media/HTTP/runtime regression:
  413 passed, 6 skipped. Skips do not count as acceptance.
- Import firewall and package/wheel isolation: 15 passed.
- Ruff on changed production/test areas and BasedPyright on changed production
  modules: passed, zero errors/warnings.
- Independent read-only reviewer: ALLOW for this draft and Store lifecycle
  slice after all three migration/fixture findings were corrected.
- Draft committed separately as 05a0cd2f. Store changes and all current callers
  form the coupled migration commit; do not deploy the code without 0018.
- PostgreSQL populated upgrade, constraints, concurrency and restart/replay:
  tests written, NOT EXECUTED. Real VLM/ASR/VAD and complete Pipeline:
  NOT EXECUTED. Both belong to remote desktop acceptance.
- Task 07 remains in progress. Actual Stage 1 compiler/evaluators, generation
  Command, eight-member output reader and Runtime integration are not approved
  or completed by these tests; Stage 2/3 and downstream work remain required.
