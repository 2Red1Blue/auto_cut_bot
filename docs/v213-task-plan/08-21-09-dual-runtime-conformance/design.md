# Dual Runtime Conformance Design

## 1. Decision Summary

This task does not build a second pipeline inside nanobot and does not make the
Pipeline service an authority owner. It supplies two constrained adapters over
one Kernel application boundary and an independent comparison oracle over
committed Store facts.

```text
Pipeline HTTP                                   cut_bot / nanobot
request -> durable control plane                closed tool request
       -> Pipeline router                       -> restricted tool profile
                         \                     /
                          typed Command intent
                                  |
                 autocut_kernel Command Gateway
                     / Query Gateway / Catalog
                                  |
        Dispatcher -> Handler -> Admission -> shared Store/UoW
                                  |
               committed business Artifact graph
                                  |
                read-only conformance projector
```

The Pipeline control-plane database may remember HTTP idempotency, queue
delivery and scheduling cursor. Nanobot may remember session/tool correlation.
Neither is a business system of record. A conformance oracle ignores those
records and reconstructs both outcomes through the same Kernel Query/Store
reader.

### Existing authority gap

The tracked `RuntimeConformanceReceipt` 1.0 schema is a generic gate-predicate
receipt. It lacks the hashes and projections required by implementation design
07, and the tracked Stage 5 contribution still declares dual-runtime bindings
unresolved. Because those are protected authority sources, Task09 cannot
extend the schema or alter the Registry. Its entry receipt must instead bind an
independently accepted Authority Change that provides the closed detailed
receipt/projection source. Until then the task remains `planning`.

The earlier Agent Runtime commits are also not an implementation base. Their
files were deleted from the current integration branch. They can inform a
census, but only the current frozen contract and current predecessor APIs may
authorize reimplementation.

### Context loading is not an oracle

`implement.jsonl` and `check.jsonl` are closed lists of candidate context
sources. They neither are nor authorize the doc12 `TaskSnapshot`, and no
reason string, file existence check or implementer-authored expected result can
be interpreted as task admission or conformance pass.

At task start, an approved machine-side Context Loader reads every direct
source to UTF-8 EOF and emits a generated context manifest with:

- normalized source identity, byte length, full-file SHA-256 and explicit EOF
  completion;
- every section anchor and exact byte/line range actually selected for model
  context, with an independent slice SHA-256;
- a hash-bound reference closure covering referenced definitions,
  preconditions, failure rules, exceptions and direct Rule/Command/Artifact
  contracts, with the same full-file and slice evidence for every closure
  member;
- authority/context/base/candidate identities and the exact gate-bundle,
  toolchain-lock and Supervisor-contract hashes.

Only this generated and validated object can populate
`TaskSnapshot.context_manifest`. The planner may inject the recorded slices,
not an unrecorded summary or the JSONL prose. If the closure cannot be located
without truncation or exceeds the approved budget, admission denies and the
task/references are split before retry; injecting all referenced documents is
not a fallback.

## 2. Runtime Ports and Capability Separation

Both runtime compositions receive exactly these business capabilities:

```text
CommandGateway.execute(typed_intent) -> accepted | existing | rejected | indeterminate
QueryGateway.get_snapshot/get_diagnostics/get_milestone(committed_ref)
RecoveryCatalog.list_allowed(admission_ref)
```

The concrete names must come from the accepted generated/public predecessor;
Task09 must not introduce a parallel Gateway because the predecessor chose a
different Python symbol name. Runtime code cannot receive `UnitOfWork`, Store
write ports, Admission evaluator, domain compiler, Renderer/FFmpeg, Provider,
Platform or publication-controller objects.

The Kernel composition root owns the business database principal and effect
ports. Runtime principals have read-only Query capability plus their own
non-business control-plane tables. Database grants deny DML on business
`authority`, `storage` and `execution` relations from both runtime roles.

### Pipeline HTTP Runtime

The HTTP adapter performs authentication, closed decoding and transport
idempotency only. The router derives a command proposal from:

```text
committed snapshot + Admission.next_action + frozen completion policy
```

It re-queries before every proposal. A Pipeline row may say `running` because a
message is leased, but it may say `succeeded` only after the Kernel projector
reports the corresponding committed milestone. On restart, `running` and
`indeterminate` commands are looked up by their existing canonical invocation
identity. A new invocation is forbidden until the old one has a deterministic
terminal state authorizing the next command.

### cut_bot/nanobot Agent-Native Runtime

The v2.1.3 Agent profile uses a dedicated restricted `ToolRegistry`, not the
default general-purpose nanobot registry. Its complete business tool taxonomy
is:

- query: snapshot, diagnostics, milestone;
- recovery query: list strategies already allowed by one Admission;
- proposal: submit one closed Command intent;
- explanation: format already-committed facts without writing them.

The profile does not include shell, filesystem mutation, generic HTTP/MCP,
legacy `agent/tools/pipeline`, `database_write`, `db_query`, story render,
provider or publication tools. The tool composition receives a prebuilt public
Gateway, not a Store or internal service locator. A dedicated profile is
required because merely adding a safe tool to a registry that still exposes
shell or legacy render/database tools would not prevent bypass.

Agent proposal DTOs carry an opaque scenario/intent identity, exact committed
refs and registered strategy identity. They cannot carry paths, ticks/PTS,
span candidates, Recipe members, QC results, FFmpeg arguments, provider model
parameters, platform target or an arbitrary dictionary. The Gateway derives
the command key/slot and rejects unregistered or stale refs.

Natural-language planning is transport context. If it ever influences a model
call, the Kernel requires it to exist first as an audited committed root
Artifact in the exact ModelInputManifest; otherwise it is rejected as a hidden
input.

## 3. Conformance Evidence Model

There are three different objects. Keeping them separate prevents a runtime
from declaring itself conformant.

### 3.1 Runtime observation trace

Each adapter emits a closed append-only observation trace for diagnostics. An
event contains:

```text
runtime_kind/runtime_build
transport_sequence
observed_at/trace correlation
proposed command type and canonical request hash
Gateway response category and returned exact refs
```

Observation traces are evidence of what the adapter attempted, not authority
for what committed. They are stored outside the business Artifact graph or as
non-authoritative diagnostic blobs.

### 3.2 Canonical business projection

After the case stops, an independent read-only projector starts from the
frozen committed input set and traverses exact Store relations. It never reads
the Pipeline run projection or Agent result DTO as proof of success.

The projection header contains:

```text
projection_schema_version
case_id / fixture_hash
authority_lock_hash / schema_bundle_hash / registry_set_hash
kernel_source_commit / kernel_build_hash
committed_input_artifact_set_ref+hash
policy_refs+hashes / strategy_refs+hashes / frozen_clock_ref
```

Each command projection contains:

```text
logical_step_key / registered ordinal / causal predecessors
command profile+version / canonical request hash / invocation semantic hash
Gateway decision category
Receipt state, effect phase, business request/result refs and business hash
ordered result Artifact refs+content hashes / ArtifactSet ref+set hash
ordered RuleResult projections / Admission ref+content hash+next action
Recovery fingerprint/reservation/attempt/ledger refs and exact debit/balance
projected milestone after the command
```

The final projection contains the exact reachable committed Artifact graph and
closed typed summaries for Recipe, structural/media/editorial/local-release QC
and Run outcome. Member ordering comes from registered ordinal/role and causal
relations, not row insertion time. Duplicate delivery observations collapse
only by resolving to the same committed Command slot; distinct committed
attempts or recovery revisions are never collapsed.

`Receipt business hash` is the JCS hash of the authority-bearing Receipt
fields defined by the accepted schema. If the generated Receipt content hash
already excludes operational metadata, its literal content hash must match as
well. A Timestamp/UUID is excludable only if the accepted authority schema
classifies it as transport metadata. The harness cannot locally decide that a
hash mismatch is harmless.

Paired PostgreSQL cases seed the same deterministic Artifact IDs, logical
identities, refs and frozen clock into isolated schemas. This permits literal
Admission/Recipe/QC/Receipt and business Artifact hash equality rather than
weak semantic matching between unrelated lineages.

### 3.3 Runtime conformance receipt

The detailed receipt binds:

- both runtime kind/build identities and the shared Kernel build;
- authority lock, Schema bundle, RegistrySet and execution eligibility;
- committed input set, policy/strategy set, fixture and frozen clock;
- both raw observation-trace hashes;
- both canonical projection hashes;
- exclusion-policy version/hash;
- per-section comparison results and final `allow|deny` decision;
- test runner/toolchain identity and evidence refs.

The receipt is generated only by the independent comparator. It is then
referenced by the generic Trellis `runtime_conformance` predicate receipt; the
generic task-gate envelope is not used as a substitute for detailed business
evidence.

## 4. Canonical Comparison and Exclusion Policy

Canonical encoding is RFC 8785/JCS UTF-8. The algorithm is fixed:

1. validate both observation traces and both Store snapshots against their
   closed schemas and bound authority hashes;
2. require identical committed input, policy, strategy, authority, Schema,
   Registry and Kernel build identities;
3. reconstruct each business projection independently from committed Store
   facts;
4. verify every projected ref/hash/parent/member relation and profile-required
   member is present and committed;
5. apply the versioned exclusion projection only to observation envelopes;
6. JCS-encode and hash canonical command and final-business projections;
7. compare command decisions, Receipt/Admission/Recovery facts, Artifact graph,
   Recipe/QC and milestones; emit `allow` only when all required sections are
   equal.

The closed exclusion set may cover:

- `runtime_kind` and the two runtime-build identities in receipt headers;
- HTTP request/correlation/Idempotency-Key presentation and agent
  session/message/tool-call IDs;
- trace/span/log IDs, host/process/worker identity;
- queue delivery count, lease owner and protocol retry timing when they do not
  participate in a business fence or effect decision;
- observation timestamps, latency and HTTP/tool response formatting;
- natural-language plan/explanation text.

It may not cover input/policy/strategy hashes, command/request semantic hashes,
business fencing/lease facts used for a committed transition, Artifact/Set
refs or hashes, Receipt state/effect/result, Admission/RuleResult, Recipe/QC,
Recovery or milestone. No wildcard path is accepted. An unknown field or
exclusion path is a schema error. Randomizing permitted excluded fields must
leave the comparison hash unchanged; mutating any business leaf must deny.

## 5. Replay, Restart and Recovery State Machines

### Replay

Same key + same canonical request/profile resolves the existing slot and
Receipt. Same key + changed request/profile is conflict. Observation traces may
have another transport event, but canonical projection remains one command
with the same effect and result refs.

### Restart and commit-ack loss

At each durable boundary the process is terminated and rebuilt with no
in-memory cache. Recovery order is:

```text
load checkpoint refs only -> re-read committed heads/Receipt
-> recompute milestone -> reconcile existing invocation -> legal next action
```

Commit acknowledgement loss must resolve by exact key/readback. A Runtime flag
cannot authorize replay.

### Indeterminate external/provider effect

An accepted-but-unconfirmed provider effect remains tied to the original
Attempt and provider request identity. Both runtimes may only query/reconcile
that Attempt. No replacement call, new Recipe/QC fact or success projection is
allowed. External publication is disabled, so any Platform-port call is a
test failure; publication-indeterminate fixtures may test the shared state
machine only with a local fake that performs no external operation.

### Recovery

Both runtimes ask the same `RecoveryCatalog` for the strategy already allowed
by Admission. The Kernel derives the same fingerprint and performs the sole
RecoveryLedger CAS/debit. Conformance compares reservation, attempt ordinal,
cost, resulting Admission and exact final balance. Insufficient budget creates
the same exhausted evidence and no executable slot. A runtime switch reuses
the existing ledger epoch/reservation and cannot reopen budget.

### Runtime switch

Switch tests use one lineage and Store, not two independent Jobs. The new
runtime receives only lineage/ref correlation, re-queries the Kernel and must
return/reconcile the existing Receipt before doing anything else. At every
Command boundary, Pipeline-to-Agent and Agent-to-Pipeline switches must leave
one root identity, one invocation per semantic slot, one recovery debit and one
visible local output.

## 6. Test Architecture

### Pure fixture suite

- strict decoding, duplicate-key, additional-property and invalid JCS cases;
- projection ordering independent of database/observation insertion order;
- each allowed exclusion randomized independently and in combination;
- unknown/wildcard/business-field exclusion negatives;
- one-bit mutations for request, Artifact, Receipt, Admission, Recipe, every QC
  layer, Recovery balance and milestone;
- missing/duplicate/uncommitted/unreachable Artifact members;
- fake Runtime self-reporting success while Store projection is denied;
- closed Agent DTO rejection for path/PTS/Recipe/QC/dict/provider/platform
  fields;
- architecture scan for direct forbidden imports and calls.

### Disposable PostgreSQL suite

The harness creates two isolated schemas from the production migrations and
loads the same committed deterministic seed. It runs Pipeline and Agent through
their public adapters with the same Kernel composition, then compares Store
projections. Required cases are:

1. admitted golden path through Recipe, local render and all QC layers;
2. precondition/Admission denial and no downstream command;
3. same-request replay and changed-request conflict;
4. restart before/after claim, handler result, ArtifactSet/Receipt commit and
   control-plane acknowledgement;
5. commit-ack loss and stale runtime lease/fence;
6. provider attempt `indeterminate` followed by original-identity reconcile;
7. recovery reserve, crash before execute, finalize, last-budget contention
   and zero/insufficient-budget exhaustion;
8. Pipeline-to-Agent and Agent-to-Pipeline switch at every Command boundary;
9. runtime DB principals denied direct business DML;
10. publication canary records zero calls and no publication Artifact/outbox
    facts.

The real local render case may invoke FFmpeg only behind the registered Kernel
Renderer port. Runtime objects receive a canary port that raises if invoked,
and call-stack/principal evidence proves the effect was initiated by the Kernel
handler. SQLite, monkeypatched private authority tables or filesystem scanning
cannot satisfy these tests.

## 7. Architecture Enforcement

The architecture gate scans both source and dependency graph:

- Pipeline/Agent adapters may import only the Kernel public Gateway/query DTOs;
- deny Runtime imports of Store implementation/UoW, Admission evaluator,
  physical-edit/Recipe/QC constructors, rendering/FFmpeg, Provider/Platform,
  publication controller, legacy `autocut_core` or legacy agent pipeline;
- deny `subprocess`, dynamic imports, direct SQL clients/cursors and generic
  network clients in the v2.1.3 Agent adapter/tool package;
- assert the restricted nanobot registry exact tool-name set and reject all
  default/general-purpose tools;
- run the Kernel import firewall and isolated-wheel smoke unchanged.

Runtime permission tests complement static analysis. Runtime workload roles
cannot write business tables or read provider/platform credentials. The
Gateway service principal has only the profile-required capabilities, and the
publication capability is absent for this task.

## 8. Failure and Rollback

The comparator produces `deny` for stale/mismatched provenance, missing input
closure, invalid trace, projector error, illegal exclusion, unequal business
facts or missing required test. It never partially passes a stage or emits a
warning-only result. If the projection design cannot represent a predecessor
fact without inventing a field, implementation stops for an Authority Change.

Before any publication enablement, rollback disables the Agent v2.1.3 tool
profile and Pipeline conformance routing while retaining immutable business
facts and receipts. Schema/migration changes are additive; they are not rolled
back by deleting committed authority records. An `allow` receipt is invalidated
by any authority, Schema, Registry, Kernel/runtime build, fixture, policy,
projection or exclusion-policy change.

## 9. File Ownership Waves

No two active implementers may own the same file. Later waves start only after
the dependency they consume is accepted.

| Wave | Owner boundary | Files/directories | Dependency |
|---|---|---|---|
| 0 | independent Authority Change, not Task09 | tracked authority/schema/Registry and generated outputs | must be accepted before Task09 starts |
| 1 | Kernel conformance owner | `packages/autocut-kernel/src/autocut_kernel/runtime_conformance/**` and only `tests/runtime_conformance/unit/**` | Wave 0 and shared Store/Command predecessors |
| 2 | Pipeline adapter owner | `auto_cut_bot/pipeline/runtime/{ports,models,stages,service,composition,worker,__init__}.py` and only `tests/pipeline/runtime_adapter/**` | Wave 1 and completed Pipeline worker |
| 3 | Agent adapter owner | `auto_cut_bot/autocut_agent_runtime/**`, the manifest-listed dedicated v2.1.3 tool/profile composition module, and only `tests/autocut_agent_runtime/**` | Wave 1; no overlap with Wave 2 |
| 4 | paired harness owner | only `tests/runtime_conformance/fixtures/**`, `tests/runtime_conformance/postgres/**`, `tests/runtime_conformance/integration/**` and `tests/runtime_conformance/support/**`; no unit or production adapter edits | Waves 2 and 3 |
| 5 | independent checker | read-only candidate, receipts and test evidence | Wave 4 exact candidate tree |

If the accepted predecessor exports different filenames, the task manifest is
replanned before implementation. Ownership is never expanded ad hoc, and Wave
0 protected files are never added to Task09's ordinary write allowlist.
There are no shared root-level files under `tests/runtime_conformance/`: Wave 1
keeps unit-only helpers below `unit/`, while Wave 4 owns cross-runtime seeds and
harness helpers below its four listed subtrees. A needed top-level
`conftest.py`, package initializer or test configuration change is a planning
change and must receive one explicit owner before work resumes.

The accepted `TaskSnapshot` records this path-to-original-owner map. A Wave 5
`repair` finding names exactly one owning wave and the minimal path within that
owner's original boundary. A finding that spans boundaries is decomposed into
separate findings with dependency order; Wave 4 never patches a Wave 1 module
or unit test, and an owner is not silently replaced. Any owner repair changes
the candidate tree and invalidates downstream receipts/check evidence before
the dependent wave or checker reruns.
