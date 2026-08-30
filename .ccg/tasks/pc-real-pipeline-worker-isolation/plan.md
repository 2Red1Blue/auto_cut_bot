# Implementation plan

1. Introduce one typed, closed stage-isolation failure for deterministic persisted-input incompatibility. Arbitrary runtime/programmer errors remain fatal.
2. Make the VLM batch finalizer distinguish deterministic batch validation failures from infrastructure/store failures and map only those failures to the typed runtime isolation boundary.
3. Extend runtime Pipeline Receipts with immutable optional failure code/detail fields and add a CAS store operation that terminally fails the exact indeterminate command, blocks later commands, and fails only that Run.
4. Add worker/reconciler, migration, PostgreSQL and VLM regressions proving the incompatible Run is isolated, its cause is durable, no provider call is repeated, and a later Run remains schedulable.
5. Run focused tests, Ruff, type checking and an independent read-only review; fix any blocking finding.
6. Commit and push to `feat/v213-contract-codegen`, update the clean PC runtime worktree through Git, apply only the new migration, and restart the existing Pipeline service.
7. Verify the historical Run reaches terminal failed with its exact Receipt, then submit or resume one authorized real single-episode run and inspect stage debug plus database Receipts.

## Non-goals

- Do not weaken the VLM finalizer or accept mixed policies.
- Do not delete or rewrite historical child Artifacts/Receipts.
- Do not reset the real PostgreSQL database.
- Do not touch the dirty primary PC checkout, private configuration, media, or debug files through Git.
