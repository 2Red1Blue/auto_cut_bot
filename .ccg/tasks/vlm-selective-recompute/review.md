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
- 81 focused pipeline tests pass (8 environment-gated skips).
- A real PostgreSQL integration test proves that the target cannot read the
  origin Blob before binding, can read the exact same immutable object after
  binding, survives origin-path deletion and Store re-instantiation, and emits
  exactly one origin plus one target Receipt.
- Wheel build contains the new Kernel modules.

## Remaining work

The HTTP `POST /v1/pipeline/recompute` control-plane Run, selected-episode VLM
dispatch and lineage budget/hold controls remain unimplemented. This binding is
only the secure input-reuse primitive those steps will call.
