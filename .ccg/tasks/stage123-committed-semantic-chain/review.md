# Review checkpoints

## VLM Semantic Pack v3 and Source authorization wave

Decision: GO for commit as the incompatible upstream replacement wave. The
overall Stage 1-3 task remains in progress.

Closed findings:

- All provider-controlled semantic DTO construction is behind one rejection
  boundary; invariant failures persist a terminal denial Receipt.
- Store exact readers re-read immutable raw Doubao bytes, rebuild Source/Window
  ownership and reject forged semantic-pack content even when member/set hashes
  are recomputed.
- SourceOperationPolicy and the content-bound SourceOperationGrant replace the
  former caller-chosen authorization ID. VLM requires `semantic_analysis` and
  MediaPreflight requires `render_source` before side effects.
- Execution profile v4 is the only executable Semantic Pack profile. v1-v3 are
  terminal read-only history, migration checks use fail-closed SQL boolean
  semantics, and bootstrap stages cannot falsely complete a semantic run.
- Old v2 observation authority, fixture semantic commands, inactive Stage 1-3
  facade, old scenario/Agent runtime prototypes and semantic-resolution proof
  projection were removed without aliases or dual writes.
- Kernel imports no application runtime; Source/Window decoding has one Kernel
  owner shared by Store and source preparation.

Evidence:

- Related unit/integration/PostgreSQL suite: 412 passed.
- Architecture boundary suite: 4 passed.
- Ruff on every changed Python file: passed.
- BasedPyright on every changed Python file: 0 errors, 0 warnings.
- `git diff --check`: passed.
- Independent final semantic review: GO after continuity rejection regression.

Known deliberate state:

- Stage 1-3 replacement commands do not exist yet; after source/VLM/media
  bootstrap the HTTP run fails closed instead of claiming semantic success.
- Agent Runtime will be rebuilt only after the shared Stage 1-3 commands exist.
