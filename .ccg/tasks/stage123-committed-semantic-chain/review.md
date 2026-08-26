# Latest review: Stage 2 durable Command (2026-08-26)

Scope ALLOW after independent request, lifecycle and exact reader reviews.
Commits: ff5fef1b (request), a8dcdf53 (shared lifecycle), 9e924be9 (Command/reader).
Stage2 now adds the two actual input checks to seventeen business checks,
atomically commits five members and replays stored identities. No provider
execution or replacement compilation on successful replay.

Closed findings: actual Store retry parsing requires top-level retry policy/hash;
all earlier attempts' existing raw Blobs must be checked on replay; semantic
denial details preserve original canonical UTF-16/number constraints. The private
provider inspection in a composition test now follows the lifecycle owner.
Rehashed raw/request/Admission and a later feasible self-asserted pass are rejected.

Evidence: 2085 semantic/architecture tests plus 104 Runtime/adapter tests passed;
Ruff/production types/diff checks clean. No DB/provider/full Pipeline execution.
Stage2 HTTP/profile registration, Stage3 and downstream/remote acceptance remain
open. Task07/this CCG task stay in progress, not archived.

## Earlier review: Stage 2 pure business chain (2026-08-26)

Task 07 remains in progress. Runtime registration, durable Stage 2 generation
Command/exact output reader, Stage 3 and remote execution are not completed.

Independent review accepted candidate projection/context, acyclic portfolio
values, four-business-member composition, strict five-member structural decoder,
closed nineteen-check Admission values and seventeen-rule business evaluation.
The final material-value delta was independently checked ALLOW before commit.

Findings addressed in this wave:

- Exact Candidate cannot claim different Sources in different assignments.
- Decoded Source grant/payload hash/window set/proxy/clock/episode and VLM child
  request identities must close; output scope cannot differ from predecessors.
- Structurally valid low-confidence/missing-measurement candidates remain
  diagnosed alternatives, not a whole-Catalog structural failure.
- Fact witnesses must name a candidate's direct anchor/supporting/payoff Event,
  not merely the same Card owner or a context-only Event.
- Material proofs retain full original proposals, required-fact witnesses and
  taint exclusions. Primary taint counts must not exceed retained tainted refs.
- A later feasible tuple and rehashed fake unsupported earlier proposal both
  fail independent canonical/material checks. No compiler result or supplied
  check map is accepted as evaluation truth.

Evidence: 1995 semantic-chain/architecture pure regression tests passed after
the final taint-count delta; changed-file Ruff and production BasedPyright are
clean. Candidate, material, compiler, decoder and evaluator use real algorithms
over synthetic media/MemoryStore; no DB/provider/whole-Pipeline execution.
Final delta evidence is recorded in stage2-production-wave.md. Material evidence
is committed in 261488d5; composition/independent business evaluation in 53cda7bc.

## Earlier review: Stage 1 HTTP integration (2026-08-26)

Decision: ALLOW for this local implementation slice. Task 07 stays in progress.

Source v2 and execution profile v6 freeze the complete Stage 1 policy. The thin
adapter reads actual Source/VLM predecessors and delegates the same durable
Kernel Command on execute/reconcile. The four-stage plan cannot claim full-run
success. Historical pre-v6 rows are not silently rewritten.

The cross-release scope is explicit: Stage 1 checks its own installed policy;
Store verifies prior VLM raw bytes under the original frozen policy. This is not
an assertion that every old run field matches the currently installed release.

An isolated-wheel regression exposed an execution-facade dependency while
loading pure configuration. The policy owner moved to semantic_chain, preserving
request bytes/hashes. No dependency was installed to hide the boundary error.
Independent review confirmed the move and fixture corrections.

Evidence: 2322 selected pure/regression tests passed; another 2 media pure tests
passed with 4 PG cases excluded. 63 DB/media cases collected only. Changed-file
Ruff and production BasedPyright passed. No database migration, real VLM/ASR/VAD,
service startup or complete Pipeline was executed on this workstation.

See docs/v213-task-plan/08-21-07-stage1-3-semantic-chain/stage1-runtime-wave.md.

## Historical review checkpoints (superseded implementation status)

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
