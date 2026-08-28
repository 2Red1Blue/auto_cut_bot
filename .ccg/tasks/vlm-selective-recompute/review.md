# SourcePrep cross-Job binding review

Date: 2026-08-28

## Scope reviewed

- The new binding never exposes a generic cross-Job Blob read operation.
- Only `BindWholeSeriesSourcesCommand` may create target Blob claims.
- The protected PostgreSQL writer independently resolves the exact successful,
  singleton origin `PrepareWholeSeriesSourcesCommand` Receipt before writing the
  target's two-member artifact set.
- A target read accepts only the strict native singleton or the strict binding
  pair; it verifies the target scope, binding target, policy and payload hashes.

## Findings

No release-blocking defect found in this slice. One stale portability fixture
assumed the VLM command had ordinal 1; `context_prepare` now precedes VLM, so it
was changed to select by the stable stage name.

## Evidence

- Ruff and BasedPyright pass for changed code.
- 76 focused run-service/API/SourcePrep stage tests pass; the SourcePrep command
  suite passes 54 tests with 4 PostgreSQL-gated skips while the local Podman VM
  is stopped.
- A real PostgreSQL integration test proves that the target cannot read the
  origin Blob before binding, can read the exact same immutable object after
  binding, survives origin-path deletion and Store re-instantiation, and emits
  exactly one origin plus one target Receipt.
- The Kernel wheel build contains the source-reuse and PostgreSQL store modules.

## Current environment limitation

The local Podman VM is currently stopped, so the disposable PostgreSQL database
at `127.0.0.1:5433` cannot be reset or queried. The full real-PostgreSQL
recompute regression was already passed before the VM stopped, but it must be
rerun after the VM is deliberately restarted; a stopped database is never
reported as a passing portability check.

## Follow-up scope

`POST /v1/pipeline/recompute` now implements the deliberately narrow,
full-stage path: it creates a distinct semantic-only Run, binds the exact
successful SourcePrep evidence before enqueueing it, and then uses the normal
Context/VLM stages. It has focused API/service tests and a real PostgreSQL test
that deletes the origin host path before the target SourcePrep projection.

Selected-episode VLM dispatch, policy-changing plans, partial-result
Aggregates, durable inspection hold and lineage budget controls remain
unimplemented. They must not be inferred from the full-stage endpoint.
