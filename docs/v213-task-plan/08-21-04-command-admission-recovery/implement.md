# Task04 Implementation Plan

## Entry gate

1. Verify immutable authority baseline
   `baf667f797ac7d4eb34e48caad8047fb07433c9c`, total source hash
   `c34af7451919ad9a895644b40136062834b7ba9e857139f10b61f7dc51be67e9`,
   curated manifests, import firewall and Reuse Ledger. Deny entry on any
   commit/hash/provenance mismatch or accepted F1/later supersession; rebuild
   the task snapshot rather than implementing against stale authority.
2. Verify Task01 exposes the required closed contract/profile schema surface
   and Task03 exposes a reviewed PostgreSQL UoW composition API. Stop and
   return to the owning task if either premise is false.

## Ordered slices

1. Add execution migration: exact paired receipt/run/reservation/result/
   diagnostic refs, slot/receipt constraints, indexes, and deferred triggers.
   Prove migrations against clean PostgreSQL.
2. Implement profile resolver, deterministic request/reference/capability gate,
   derived key and slot claim/reconcile path. Add zero-handler-call rejection
   tests and concurrent idempotency tests.
3. Implement immutable Receipt/Registry transitions and atomic Store/UoW
   composition. Test every effect phase and cross-hash substitution rejection.
4. Implement the pure Admission evaluator boundary and its atomic business
   set/Admission commit profile. Test evidence re-read and handler override
   denial.
5. Implement canonical fingerprint validation and RecoveryLedger controller:
   reserve/finalize, exhausted Attempt→Ledger path, CAS retries and no mutable
   budget projection authority.
6. Implement bounded Provider transport retry as an ordered one-to-many
   GenerationAttempt chain: frozen policy, explicit failure disposition,
   dispatch lease/token, idempotent next-ordinal reservation and an exact
   Receipt-to-Attempt relation. Test `503 → 429 → success`, three-attempt
   exhaustion, active-dispatch contention and indeterminate reconciliation.
   Derive each immutable `not_before_at` from frozen request bytes and the
   policy hash; cover the complete registered Provider duration with the
   dispatch lease; keep Files preparation outside Responses reconciliation.
7. Add PostgreSQL fault-injection/race fixtures for AC4–AC6 and the global
   G-REPAIR-001…006 vectors. Add both-Runtime conformance harness via public
   Dispatcher ports only.

## Checks before each commit

- Run import-firewall/Reuse-Ledger checks, format/lint/type checks and the
  focused unit tests for the changed slice.
- Run the relevant PostgreSQL integration/race/crash tests with an explicit
  disposable test schema.
- Independently inspect contract/profile/reference drift and confirm no legacy
  import or Runtime write path was introduced.
- Commit only the clean Task04 worktree; do not modify either dirty legacy or
  source-authority worktree.

## Stop conditions

- A necessary behavior is absent from the generated Registry/profile/schema.
- The only way to pass is a legacy import, mutable budget state, hidden default
  or a Runtime direct write.
- A cross-table invariant cannot be proven in the same transaction. Return to
  authority/design instead of encoding a best-effort workaround.
