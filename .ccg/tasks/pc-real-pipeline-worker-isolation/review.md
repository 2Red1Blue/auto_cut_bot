# Review checkpoint

## Decision

The change is narrowly scoped to one persisted VLM batch invariant. It does
not weaken the Kernel finalizer, retry arbitrary `ValueError`, or mark invalid
content successful.

## Verified properties

- Only `VLM_BATCH_CHILD_REQUEST_POLICY_MISMATCH` is translated into run-local
  isolation; all unrelated finalizer errors remain fatal.
- The Kernel creates no aggregate claim, Artifact, or Receipt for the invalid
  batch.
- The Postgres store mints the isolation Receipt and atomically fails the exact
  indeterminate VLM command while blocking only its successors.
- Committed child outcomes are read during reconciliation, so no paid provider
  request is repeated.
- Historical Receipt rows remain valid with NULL diagnostic fields.

## Local verification

- Ruff: passed on all changed Python files.
- BasedPyright: passed on the changed production modules.
- Pytest: 99 relevant tests passed, including zero provider redispatch during
  replay; PostgreSQL integration cases are collected and skipped locally until
  run against the disposable PC database.

## Remaining checkpoint

Apply migration 0050 on the PC database, run the PostgreSQL integration test,
restart the HTTP pipeline worker, and verify that the historical incompatible
run becomes failed with a diagnostic Receipt while another run remains
schedulable.
