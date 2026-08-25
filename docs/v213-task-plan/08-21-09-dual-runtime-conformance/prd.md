# Dual Runtime Conformance

## Goal

Deliver the v2.1.3 dual-runtime boundary in which the Pipeline HTTP Runtime
and the cut_bot/nanobot Agent-Native Runtime are only two callers of the same
`autocut_kernel` Command, Store and Admission authority. For identical
committed inputs and frozen policy/strategy, both runtimes must produce the
same business Artifact hashes and equivalent Receipt, Admission, Recipe and QC
authority facts. Runtime-specific transport and observability data may differ
only through a closed, reviewed exclusion list.

External publication remains disabled. This task proves local/test/shadow
runtime parity; it does not grant publication eligibility.

## Current Planning Facts

- The current integration branch contains the Pipeline HTTP run-control
  surface, but its concrete worker composition is still an unfinished
  predecessor.
- The previously accepted Agent Runtime and cut_bot adapter commits are
  historical evidence only: their files are absent from the current
  integration head after a later deletion. Task status or an old commit is not
  accepted as proof that the current candidate contains the runtime seam.
- The protected `RuntimeConformanceReceipt` 1.0 schema currently represents a
  task-gate predicate envelope and does not bind the two runtime builds,
  committed input set or canonical business projections required by the v2.1.3
  dual-runtime contract.
- The tracked Stage 5 source still records dual-runtime-conformance bindings as
  unresolved. An accepted Authority Change must close that source/schema gap
  before this ordinary implementation task starts.

### Context source and snapshot boundary

- `implement.jsonl` and `check.jsonl` are context-source declarations only.
  Their presence, file paths and prose reasons do not constitute the doc12
  `TaskSnapshot`, task admission, check evidence or a pass oracle.
- Before Task09 can leave `planning`, the approved machine-side Context Loader
  must read every directly declared source to UTF-8 EOF and generate a
  `TaskSnapshot.context_manifest` entry containing its full-file byte length
  and hash, EOF result, each injected section/byte range and slice hash, plus
  the hash-bound reference closure for definitions, preconditions, error
  rules, exceptions and directly referenced Rule/Command/Artifact contracts.
- The same generated snapshot binds the exact gate bundle, toolchain lock and
  Supervisor contract hashes. A missing, truncated, unstable or stale member
  denies entry; neither an implementer summary nor text printed by a test may
  fill it in.
- If a required reference closure exceeds the approved context budget, split
  the task or narrow its declared source/section scope and regenerate the
  snapshot. Do not inject the full corpus, truncate it or replace it with an
  automatic summary.

## Requirements

### R1. One business authority path

- Both runtimes submit closed typed intents to the same generated/public
  `CommandGateway`/Dispatcher surface and read through the same Query Gateway.
- Both runtimes use the same Command profiles, Store implementation,
  Admission evaluator, Recovery catalog and projector for a given conformance
  case.
- A runtime may persist transport cursor, queue lease, HTTP status or agent
  session correlation as non-business control-plane state. It must not persist
  or derive an Artifact, Receipt, Admission, Recipe, QC decision, recovery
  debit or publication fact.
- No runtime may call a Store write API, construct a physical-edit/Recipe/QC
  authority object, invoke FFmpeg/Renderer/Provider/Platform ports, or call an
  external publication endpoint. Those effects remain reachable only from a
  registered Kernel Command handler with the proper principal.

### R2. Closed Agent-Native capability surface

- The v2.1.3 nanobot execution profile exposes only query, registered recovery
  listing, typed command proposal and non-authoritative explanation tools.
- It does not expose generic shell/filesystem execution, legacy pipeline
  tools, database write/query tools, raw HTTP/provider clients, render tools,
  publish/prepare/commit/abort tools or credential-bearing internals.
- Agent intent contains semantic identifiers and committed refs only. Paths,
  PTS/ticks, FFmpeg arguments, Recipe/QC objects and untyped dictionaries are
  rejected before the Gateway is called.
- Prompt text, chat history, natural-language plans and explanations are not
  business inputs unless separately committed as an audited root Artifact and
  listed in the ModelInputManifest.

### R3. Pipeline adapter boundary

- HTTP handlers only decode/authenticate requests and invoke the durable
  Pipeline Runtime service. The Pipeline scheduler chooses a registered
  command intent from committed snapshot, Admission next action and frozen
  completion policy.
- Resume/restart re-reads committed heads and existing Command Receipts.
  `running` or `indeterminate` work is reconciled under the same invocation;
  it never creates a replacement invocation or silently advances a milestone.
- The Pipeline run-control Store is explicitly non-authoritative for business
  success. A transport status cannot override the Kernel projector.

### R4. Conformance trace and canonical projection

- Every paired case records a closed observation trace for each runtime and a
  canonical comparison projection reconstructed by an independent, read-only
  Store projector.
- The comparison binds authority lock, Schema bundle, RegistrySet, Kernel
  build, runtime builds, committed input ArtifactSet, frozen policy/strategy,
  fixture/scenario identity, business trace hashes and exclusion-policy hash.
- Canonical command entries include registered logical step/ordinal, command
  profile, canonical request hash, Gateway decision, terminal Receipt business
  projection, result Artifact refs/hashes, Admission/RuleResult projection,
  RecoveryLedger accounting and projected milestone.
- Canonical final state includes the exact committed business Artifact graph,
  Recipe and all QC/release-local decisions required by the profile. Missing,
  duplicate, uncommitted or unreachable facts are failures, not empty values.
- The comparison uses RFC 8785/JCS bytes. Equality is byte/hash equality after
  applying the one closed projection, never ad-hoc field deletion by a test.

### R5. Narrow runtime-specific exclusions

- The only excludable data is transport/observability data with no business
  authority: runtime kind/build headers, HTTP request and agent tool-call IDs,
  session/message IDs, trace/span/log IDs, worker/process/host identity,
  queue-delivery attempts, transport timestamps/latency, natural-language
  explanations and protocol status text.
- Exclusion paths are versioned and hash-bound. Unknown exclusions, wildcard
  paths, fields carrying policy/input/output identity, or removal of an
  Artifact/Receipt/Admission/Recipe/QC/Recovery value make the receipt deny.
- IDs and timestamps that participate in a business content hash are not
  runtime-specific. Paired fixtures must use the same committed deterministic
  identities and frozen clock where literal business hash equality requires
  them.

### R6. Replay, restart, indeterminate and recovery parity

- Same request/key replay returns the existing Command/Receipt and produces no
  second business write, budget debit, provider call, render or output.
- Process restart at every durable boundary reconstructs from Store facts and
  yields the same canonical projection.
- Provider/platform acceptance uncertainty remains `indeterminate` and only
  reconciles the original external identity. It does not create a new attempt
  or report success.
- Registered recovery uses the same fingerprint, reservation, budget epoch,
  attempt ordinal, exhaustion action and final Admission in both runtimes.
- Runtime switch preserves lineage, recovery epoch, receipts and external
  effect ownership. It neither receives a second root identity nor replenishes
  budget.

### R7. Tests use fixture and real PostgreSQL oracles

- Pure fixture tests cover closed decoding, canonicalization, exclusion-policy
  enforcement, trace comparison and one-bit business mismatches.
- Integration tests run two isolated schemas/databases seeded from the same
  committed snapshot in disposable real PostgreSQL. SQLite and private
  test-only authority tables are not accepted as parity evidence.
- Fault tests cover crash/ack loss, replay, stale lease, `indeterminate`,
  recovery reserve/finalize/exhaustion and Pipeline-to-Agent handoff.
- At least one local render/QC case exercises the registered Kernel path while
  a deny-on-call canary proves neither Runtime can invoke FFmpeg, Provider or
  Platform ports directly.

### R8. Fail closed and keep publication off

- Authority/schema/registry/build/input/policy mismatch, incomplete trace,
  illegal exclusion, business projection mismatch or missing test evidence
  produces `deny`; it is never downgraded to warning or not-applicable.
- A conformance pass is evidence for the local/test/shadow runtime predicate
  only. It cannot mint `publication_eligible`, `publish_decision=allow`, a
  platform transaction or an external visibility result.
- Any call to an external publication adapter during this task is a blocking
  failure even if the target is non-production.

## Entry Prerequisites

1. An independently accepted Authority Change closes the unresolved
   dual-runtime binding and supplies generated closed schemas for the detailed
   business trace/projection/receipt. Task09 must not edit protected governance
   source or its own oracle.
2. The Context Loader has converted the two JSONL source lists into a valid
   machine-generated `TaskSnapshot` with full-file hash/EOF evidence, selected
   section/range and slice hashes, complete reference closure, and bound
   gate/toolchain/Supervisor hashes. JSONL entries alone never satisfy this
   prerequisite; an over-budget closure requires task decomposition.
3. The exact integration predecessor has accepted shared Command/Admission/
   Recovery, Store/CAS, Stage 1-4, render/QC/local-release and durable Pipeline
   worker slices. Their commits and authority/schema/registry hashes are bound
   into the TaskSnapshot.
4. The current predecessor tree, not archived task metadata, contains or
   explicitly authorizes reimplementation of the clean Agent-Native adapter
   seam. Historical deleted code may be inspected as evidence but may not be
   restored wholesale or used as authority.
5. Both runtimes can run the same Kernel build/profile, a real disposable
   PostgreSQL service is available, deterministic conformance fixtures are
   committed, and direct runtime DB roles have no business DML grants.
6. The execution profile is `test` or `shadow`; every publication capability,
   credential and route is disabled or replaced by a deny-on-call canary.

## Acceptance Criteria

- [ ] AC1: Static architecture and runtime permission tests prove Pipeline and
  Agent reach business writes only through the same public Kernel Command
  Gateway; direct Store/Admission/Recipe/QC/Recovery writes are denied.
- [ ] AC2: The v2.1.3 nanobot profile exposes only the closed query/proposal/
  explanation surface and rejects physical edit values, generic execution,
  legacy pipeline tools, FFmpeg/Provider/Platform/publication access.
- [ ] AC3: For every golden fixture, both runtimes produce byte-identical
  canonical business projections and equal business Artifact, Receipt,
  Admission, Recipe and QC hashes under the same inputs/policy/strategy.
- [ ] AC4: Randomizing every permitted trace/transport field does not change
  the comparison hash; changing one business field or adding an unknown
  exclusion deterministically denies conformance.
- [ ] AC5: Real PostgreSQL happy, denial, replay, restart, commit-ack-loss,
  stale-lease, indeterminate, recovery and exhaustion cases pass without
  duplicate effects or divergent milestones.
- [ ] AC6: Pipeline-to-Agent and Agent-to-Pipeline takeover of the same lineage
  returns/reconciles existing Receipts, preserves the RecoveryLedger and never
  creates a second root, invocation, budget or output.
- [ ] AC7: Each case emits a schema-valid, hash-closed conformance receipt
  bound to both runtime builds, Kernel/authority/schema/registry, committed
  inputs, policies, raw traces, canonical projections and exclusion policy.
- [ ] AC8: Missing/invalid/stale evidence and every parity mismatch produce
  `deny`; no runtime self-report can make the independent Store projection
  pass.
- [ ] AC9: External publication remains unreachable and disabled; the task
  creates no publication eligibility, platform intent/effect or external
  visible object.

## Out of Scope

- Changing v2.1.3 Authority, Registry, protected schemas or blocking fixtures
  inside this ordinary implementation task.
- Implementing missing Store, Command, Stage, render/QC or Pipeline-worker
  predecessor behavior locally to make the harness pass.
- Production enablement, external publication certification, migration or
  cutover.
- Reusing legacy `ArtifactBus`, Stage, file-state, direct database, provider or
  story-render Agent tools.
