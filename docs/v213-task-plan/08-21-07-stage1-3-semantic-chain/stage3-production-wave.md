# Stage 3 production wave: committed Portfolio to Editorial Blueprint

## Status and scope

Task07 remains in progress. Stage1 and Stage2 have production Kernel Commands;
this wave implements Stage3, not a second semantic pipeline. Start with the
exact input reader while the Stage2 Runtime wave receives final verification.
No local DB, migrations, real provider/model calls or services. Remote desktop
owns real acceptance. External publication stays closed; user config is out
of scope. Use Trellis before-dev/check and independent scoped review, not the
retired governance/commit gates or the old pipeline skill.

## Explicit corrections to older design examples

- The first registered strategy is `unpartitioned-batch-v1`: one audited
  generation request contains every frozen target Story, in Portfolio order.
  Output has per-Story Blueprint/Closure/Context (3N business members) and one
  batch SemanticFeasibilityAdmission, committed in one ArtifactSet/Receipt.
  This implements Task07's all-or-nothing batch. It is not N=1-only and not
  independent per-Story successful Receipts combined by an unaudited finalizer.
- Partitioning, optional-context pruning and independent_outputs are not
  registered by this strategy. Reject unsupported modes. Required input
  overflow is an explicit failure, never truncation or partial success.
  Adding partitioning later requires a registered strategy and tests for
  writer ownership, aggregate budget and deterministic merge.
- Narrative functions use the current VLM v3 closed enum:
  `hook|setup|escalation|confrontation|reveal|reversal|payoff|aftermath`.
  Reuse that owner; do not introduce aliases such as hook_and_orient or
  emotional_payoff, or a private Stage3 capability_assessment DTO.
  Candidate declarations are accepted only through independently replayed
  Stage2 projection over committed raw VLM evidence.
- Context/request budgets measure exact canonical UTF-8 bytes, explicitly
  labelled bytes. Generation max_output_tokens is an actual provider request
  limit, not measured usage. Missing provider token/cost telemetry is unknown,
  never fabricated from bytes. The first strategy does not claim exact
  tokenizer-based accounting or a verified monetary budget.
- Pending semantic members use existing SemanticMemberIdentity and
  SemanticObjectRef. Database artifact IDs cannot appear before commit;
  parent-child refs must remain acyclic. Stage3 does not revive the deleted
  total contract, legacy Stage3 prototypes or structural-only owner fragments.

## Read boundary (first implementation slice)

`pipeline/editorial_blueprint_inputs.py` owns:

- `CommittedEditorialBlueprintInputs(semantic, narrative, portfolio)`, where
  semantic is CommittedSemanticInputs, narrative is PersistedNarrativeGraphSet,
  and portfolio is PersistedStoryPortfolioSet.
- `read_committed_editorial_blueprint_inputs(store, *,
  stage2_request, stage2_outcome)`.

Read the actual exact Stage2 five-member set through its existing replay reader,
including raw-generation auditing and all nineteen checks. Re-read Stage1/root
semantic inputs through the existing shared seam. Rebuild the prepared Stage2
request and compare actual request/input hashes and pending Catalog identity.
Require matching Job UUID, slot, Receipt, ArtifactSet and complete target order.
CommandOutcome dataclass equality excludes job_id, so compare it explicitly.
No claim, execute, generation, private Store write or DTO-as-commit shortcut.

## Semantic compilation and context

The selected Proposal, not the model, owns mandatory obligations, facts,
material requirements, physical requirements, source restrictions and Story
duration bounds. Each material requirement maps exactly once into Blueprint
evidence requirements; arrays/hashes of deferred physical requirements remain
unchanged. Blueprint may refine grouping and order, but cannot drop a mandatory
requirement, introduce an unauthorized Source or claim a safe endpoint.

Build complete per-Story evidence closures before generation. Include exact
selected Proposal/material obligations, required facts and dependency/state
context, candidate metadata, Source grant and owner-bound VLM observations.
Resolve closure objects from admitted predecessor payloads with content hashes,
not just IDs or summaries. The provider request and persisted ContextManifest
must bind the same complete content. Optional exclusions, if introduced by a
later strategy, require explicit omission records.

Identity direction is closure content -> ContextManifest -> durable request;
never put the complete request hash back into its embedded ContextManifest.
Invocation/request audit metadata belongs outside that manifest.

The draft is a closed batch with input binding and ordered stories, each with
nonempty ordered Beats. The parser assigns local ordinal and the compiler
derives stable Beat IDs from Story ID, strategy partition identity and ordinal.
Model-written artifact IDs, Source endpoints, ASR/VAD evidence and self-filled
pass/fulfilled fields are rejected. All references and preferences resolve.

Validate narrative-function support independently for every alternative,
obligation/fact coverage, one_of/all_of semantics, ordering consistency/cycles,
span intent, coarse duration and Source reuse feasibility. Stage2's initial
assignment is evidence of its earlier search, not permission to ignore tighter
Beat requirements. Reuse exact-search primitives where their semantics fit;
do not turn a candidate maximum/average duration into physical feasibility.
Keep all physical selection and exact A/V safety in Stage4.

## Durable execution and output

The Stage3 policy/request freezes generation, parser, context/compiler and retry
parameters plus the complete Stage2 request/outcome. Reuse DraftGenerationLifecycle
and DoubaoDraftProvider, with explicit per-stage byte limits. Provider timeouts
remain same-invocation reconciliation, not blind retry. All attempts and raw
responses are durable before semantic parsing; rejected drafts leave causal
receipts but no partially admitted Story outputs.

Pure compiler produces 3N business members. Independent evaluator rechecks
actual predecessors, draft, context/closures and business members before the
single batch Admission. Exact committed reader checks 3N+1 membership, complete
frozen target order, all attempt audit and independently recomputed Admission.
The declared business subject excludes Admission itself. Runtime adapters
only invoke this shared Command and never decide semantic success.

Business member order is frozen target order, then editorial_blueprint,
evidence_closure_set, context_manifest for each Story; Admission is last.
Every target has exactly one trio. Feasibility includes one joint assignment
across the full batch, not only separately feasible per-Story witnesses.

## Execution slices and ownership

1. Reader owner: editorial_blueprint_inputs.py and its dedicated tests only.
2. Model/parser owner: closed Blueprint draft/policy/value models and strict
   response schema; APIs must be frozen before context/compiler consumers.
3. Context/compiler owner: closure construction, Blueprint projection and
   semantic feasibility. Keep persistence out of these pure functions.
4. Root integration: Command/request/exact replay, batch Admission, public
   exports, Runtime configuration/adapter/migration, documentation and commits.
5. Independent reviewer: read-only tests and boundary review, not self-approval.

One writer per file. Existing Stage2 Runtime owners finish their current files
before accepting Stage3 edits. Reuse the current agents rather than spawning
another team. Deliver coherent tested commits throughout; do not wait for the
whole pipeline to become runnable before saving code.

## Acceptance checklist (delivery evidence below; Runtime and real acceptance open)

- Actual Stage2 reader and mixed Job/Receipt/Set/raw tamper negatives, no side effects.
- Two-or-more Story frozen batch, no skipped Story/Beat/requirement.
- Full closure/object identity, request-manifest parity, byte overflow rejection.
- Unsupported narrative function/unknown refs/physical fields rejection.
- Alternative coverage/capability, ordering, coarse duration and reuse negatives.
- Deterministic IDs/hashes, atomic 3N+1, finite recovery and exact replay.
- Both Runtime adapters use the same Command; no complete-run success until
  Stage4, Render and local QC are integrated.
- Pure tests, Ruff, production typing and independent review; actual
  PostgreSQL/Ark/one-episode/whole-series acceptance remains remote and open.

## Delivered reader and draft slices

- b4568448: exact input reader, 32 new tests and independent review.
- b5798b6c: editorial_models.py/editorial_draft.py, 78 new tests and independent
  review. Related focused regression: 221 passed. Full semantic-chain and
  architecture regression after both slices: 2195 passed.
- Ruff and production typing passed; no DB/provider/service execution.

Frozen draft API: EditorialDraftPolicy (explicit bytes/depth/collection/text
bounds), EditorialBlueprintDraft(input_binding_sha256, stories),
decode_editorial_draft(raw, *, expected_input_binding_sha256,
expected_target_story_ids, policy), and editorial_draft_response_schema(policy,
*, target_story_ids). StoryBlueprintDraft contains exact selected Proposal ref,
ordered Beats, ordinal-based closed ordering, duration and editorial/teaser
intent. Beat references carry exact member/object owners; evidence alternatives
use EventCard event refs and Catalog candidate refs. The compiler, not draft,
will copy Stage2 physical requirements and derive stable IDs.

Response schema keeps the provider's homogeneous Story items shape (target enum
and exact N); the strict decoder additionally enforces exact target order,
cross-field invariants and aggregate limits. Shape-valid unknown object refs,
unsupported semantic alternatives or order cycles still require independent
business rejection; parsing alone does not admit them.

The reader/draft slices alone do not produce an admitted Stage3 output. Current
context/compilation progress and remaining execution work are recorded below.

## Current implementation decisions: context and intent timing

- Provider context uses one complete shared exact-member payload pool plus the
  ordered per-Story closure and manifest rows. It does not repeat the full series
  once per Story. Each manifest binds the hash and byte length of its expanded
  content; the actual request includes every referenced payload. Persisted
  predecessor identities remain exact, never latest-head lookups.
- Story/Beat duration ranges describe contiguous editorial output intent.
  `precedes` must agree with array order; `adjacent` means immediately following;
  `max_gap` measures from the end of the earlier Beat to the start of the later
  Beat. It is not distance on a Source clock. Teaser intent does not implicitly
  add another clip, padding or duration. Targets are preferences, min/max are hard.
- `editorial_timing.py` solves all duration and max-gap constraints jointly with
  exact rational difference constraints. A separate verifier checks each witness
  directly. This proves intent consistency only, not that those durations can
  be allocated from actual footage. Candidate coarse-duration evidence must not
  be summed repeatedly to claim physical capacity; final allocation is Stage4.
- Alternative candidate refs form a candidate pool. `one_of` requires one whole
  alternative, not pieces borrowed from different alternatives; `all_of` requires
  each alternative independently. A witness selects a nonempty subset covering
  all that alternative's events. Source reuse is checked jointly across Stories.
- Candidate `editing_modes` (`dialogue|action`) are not SpanPolicy modes
  (`tight|scene|context`). Do not intersect these different vocabularies or invent
  a capability field. Preserve span intent for Stage4 evidence/endpoint checks.

These are pure compilation slices, not a Stage3 Admission or Runtime activation.
Durable 3N+1 commit and real acceptance remain open; material feasibility is
delivered in the execution slice recorded at the end of this document.

Business composition uses `compose_editorial_business_members(contexts,
projection)` and the strict `decode_editorial_business_members(members,
contexts=...)`. Blueprint member payloads bind the input hash and exact
ContextManifest identity, which in turn binds the EvidenceClosureSet identity.
Thus hash dependency order is closure -> manifest -> Blueprint, while serialized
member order is Blueprint/closure/manifest per Story. This is acyclic and does
not include database IDs or Admission. Composition produces 3N pending business
members only; the future independently admitted Command must add the single
Admission and commit all 3N+1 in one transaction.

## Context, Blueprint and business-member delivery

Pure implementation now includes:

- `editorial_context_models.py` / `editorial_context.py`: complete deduplicated
  predecessor pool, exact request/pack/Source/window and nested raw-owner joins,
  per-Story closures/manifests, closed decoding and explicit byte overflow.
- `editorial_blueprint.py`: exact frozen targets/Proposals, stable Beat and
  strategy-bound evidence requirement IDs, complete mandatory facts/obligations,
  unchanged physical requirements and Source constraints. Story duration may
  narrow but never widen Proposal bounds; teaser strategy remains Proposal-owned.
- `editorial_members.py`: 3N pending members and strict roundtrip, including
  exact agreement with closure material IDs, obligations and facts.
- `editorial_timing.py` (77390e9d): joint rational editorial intent consistency
  plus direct independent witness checks, without physical feasibility claims.

Review fixes are in these owners, not compatibility adapters: compiled values
reuse draft/material validators; rehashed nested references and fabricated IDs
are rejected; a substituted VLM request or raw semantic owner cannot hide behind
a recomputed context hash. Tests include fully rehashed DAGs so failure cannot
be credited merely to a stale outer hash.

Still open: independent SemanticFeasibilityAdmission; audited Command and exact
3N+1 replay; HTTP/Agent Runtime;
remote DB/provider acceptance. No Stage3 admitted output, real model execution,
database transaction or whole-run success is claimed by these pure slices.

Delivery commits: 2d0cd511 (context), 7643723c (Blueprint and 3N members),
77390e9d (timing). Final semantic-chain/architecture regression: 2344 passed.
Scoped independent review accepted context84/Blueprint20/members27/timing18
tests; these are subsets, not additional end-to-end acceptance counts. Changed
production typing, Ruff and diff checks passed. No local DB/model/service ran.

## Material search and generation request execution slice

The material search universe is built from the exact selected Stage2 Proposal
support, not unrestricted Catalog candidates. Every declared alternative pool
member must resolve to an eligible RequirementAlternativeProof and support the
Beat narrative function. Unknown or ineligible references are explicit errors,
not silently removed; context-only event edges never count as coverage.
Required events must be covered by the
selected nonempty subset's directly supported events; all mandatory material
facts remain protected by the upstream support proofs.

The bounded canonical order is frozen: requirements follow Story/Beat/requirement
order, alternatives are sorted by exact alternative key, and each pool's subsets
are visited by cardinality then lexicographic candidate key. all_of creates one
slot for each alternative; one_of creates one slot permitting exactly one whole
alternative. Source ownership is joint over all slots: reuse within a Story is
allowed, cross-Story reuse follows the frozen Job policy. Every inspected subset
is charged, including rejected coverage/source choices. Exhaustion is
indeterminate with no partial witness, never infeasible or successful fallback.
The independent positive verifier checks complete assignment, event coverage
and Source ownership directly; canonicality/negative claims require evaluator
recomputation, not the witness verifier.

EditorialFeasibilityPolicy binds strategy_version and max_search_states.
Stage3CommandPolicy additionally freezes explicit generation, draft, context,
Blueprint strategy, revision, request-byte budget and finite retry policy.
BuildEditorialBlueprintRequest binds the complete Stage2 request and succeeded
Job/slot/Receipt/Set identity. Preparation includes the full deduplicated context
and exact response schema in the streaming Doubao request. Preparing bytes
does not prove commitment; only the exact reader and future Command own that
boundary. No physical span choice, Store write or Runtime success is added here.

### Full-event evidence correction

Stage2 material eligibility proves required Facts, not complete Event carry.
A candidate may overlap an Event while carrying only the required Fact. Stage3
therefore rebuilds candidate-to-Event coverage edges from the exact raw pack,
EventCard and Source timeline: map the raw Event interval again, verify its
owner/range identity, then require the whole Event outer uncertainty interval
inside the candidate's conservative guaranteed-inner interval. Reuse
decode_candidate_source_context and conservative_support_bounds; no local
translation approximation. The first strategy does not join two half-events
into a complete Event proof. A legal pool candidate with no such coverage edge
can remain in the pool but contributes no event coverage.

The feasibility API additionally requires actual CommittedSemanticInputs and
the frozen JobPolicy, whose hash must match the Portfolio. Stage1/Stage2 value
objects alone do not contain the actual timeline or Source reuse policy.
Every Beat preference must belong to that Beat's eligible alternative pool;
being somewhere in the Catalog is insufficient. Preference order is soft and
does not change this strategy's canonical material search order.

## Material feasibility and generation-request delivery

Delivered and independently reviewed:

- 5718d7fc: lazy bounded whole-alternative subset-cover search, direct positive
  witness verifier, shared succeeded predecessor outcome codec. The shared
  codec preserves Job/slot/Receipt/Set identity; transport freshness does not
  change durable request bytes.
- b5b1a125: exact Stage2 eligible pool plus actual raw Source/Event/Fact proof,
  complete-event coverage, Beat function/preferences, taint, frozen Source
  restrictions and whole-batch reuse. Exact rational timing witnesses use
  canonical reduced positive decimal strings, not rounded JSON numbers.
- a45a57e4: Stage3CommandPolicy, BuildEditorialBlueprintRequest and
  prepare_stage3_request with complete context, strict response schema, exact
  whole-body byte limit, full Stage2 request/outcome and all policy hashes.
  Request preparation has no provider call or Store write.

The feasibility result's input_binding_sha256 is a domain-separated feasibility
binding over actual Source/aggregate/predecessors/projection/policy. It is not
the generation context input binding; future Admission must bind and check both,
not compare these different domains as if they were the same hash.

Independent review caught and closed two concrete issues in this wave: an
eligible Fact-carrying candidate need not carry the whole Event; predecessor
record/value types must be checked before accessing their fields. Actual
strict-admitted in-memory Stage1/2 fixtures exercise the first distinction.
A positive witness is independently checked without the solvers; a negative
conclusion, canonical first-choice and examined-state counts still require
recomputation by the future Admission owner.

Final regression: 2472 semantic-chain/architecture tests passed. New scoped
tests (included in that total): search26, outcome16, feasibility46,
request-policy34 and crossflow6. Small search oracle enumerates 1024 cases;
large-pool/deep-stack tests check bounded lazy execution. Ruff, changed production
typing and diff checks passed. No local DB, model, service or real Pipeline ran.

Next is independent batch SemanticFeasibilityAdmission, the audited
BuildEditorialBlueprintCommand, atomic3N+1/exact replay and HTTP registration.
Reuse the delivered readers/context/request/material owners; do not re-open
this as another design/discovery wave. Then continue Stage4/Recipe/Render/local
QC, Agent conformance and remote actual acceptance. Task07 remains in progress.

## Independent Admission and durable Command execution wave

The first registered evaluator is `stage3-ss-unpartitioned-v1`. It evaluates
the current full-batch, VLM-v3, byte-budget strategy, not the old partitioned
prototype. This strategy requires exactly these six batch checks and fourteen
checks for each frozen Story, in addition to a closed full feasibility witness:

| Scope | Required IDs | Actual evidence |
|---|---|---|
| Command batch | SS-IN-001 | Actual exact committed Stage1/2/root reads; not latest-head lookup |
| Command batch | SS-IN-002 | Actual immutable request/raw bytes and exact attempt audit; retry chain on replay |
| Command batch | SS-CTX-BYTES-001 | Actual complete provider body within frozen byte limit and rebuilt context budgets |
| Pure batch | SS-BATCH-001 | Complete ordered 3N subjects and frozen target order |
| Pure batch | SS-REUSE-001 | Full-batch source assignment satisfies frozen reuse policy |
| Pure batch | SS-SEARCH-001 | Canonical material/timing result recomputed from actual predecessors, never claimed pass |
| Per Story | SS-REF-001, SS-ENUM-001, SS-OBL-001 | Exact raw-draft refs/types and complete Proposal obligation/fact conservation |
| Per Story | SS-EV-001, SS-EV-002, SS-CAND-CAP-001 | Whole alternative local coverage and exact eligible/function-supported pools |
| Per Story | SS-PHYS-DEFER-001, SS-PREF-001, SS-SPAN-001 | Original material/physical requirements, legal preferences and closed span intent |
| Per Story | SS-CTX-001, SS-CTX-002, SS-HASH-001 | Actual rebuilt closure/context contents and their exact business identities |
| Per Story | SS-DUR-002, SS-TAINT-001 | Joint rational intent consistency and clean committed semantic dependencies |

The pure evaluator cannot fill the three Command-owned checks. It reconstructs
context, decodes the actual audited raw draft, reprojects the expected Blueprint
and compares actual pending/stored members before computing checks. Direct
positive witness verification complements canonical recomputation; it does not
prove negative search claims. Local Story feasibility is determined from
eligible full-event edges, separately from joint Source conflicts or global
search exhaustion. The existing domain builder becomes the public pure
editorial_material_requirements owner; do not copy its raw/time-map logic.

This explicit rule strategy replaces the old applicability of SS-CTX-003/004,
SS-PART-001/002, SS-MERGE-001, SS-BUDGET-001, SS-RECOVERY-001 and SS-DUR-001.
Those eight old rules are not emitted as pass or N/A: optional pruning,
partitioning, tokenizer/monetary budgets, the old RecoveryController and
physical duration allocation are not implemented by this strategy. Complete
one-batch composition and derived IDs are checked by BATCH/HASH; transport retry
auditing belongs to IN-002. DUR-002 proves only current contiguous editorial
intent, with no implicit transition/teaser footage. Stage4 still must prove
actual material allocation, required media evidence and exact A/V safety.
No removed check authorizes publication.

One SemanticFeasibilityAdmission binds the exact ordered 3N business subjects,
raw/canonical draft hashes, complete Command policy hash, separately named
context and feasibility input bindings, frozen Story rows and full recomputed
feasibility result. Admission does not belong to its own subject. The strict
3N+1 decoder proves shape/identity closure; only actual reads plus independent
re-evaluation prove execution authority.

BuildEditorialBlueprintCommand uses the existing DraftGenerationLifecycle and
Store transaction/CAS. Complete successful outputs commit in one operation;
invalid drafts, infeasible intent/material and search exhaustion keep causal
denial Receipts with zero business outputs. These are not transient transport
retries. Provider transient errors use the frozen finite retry policy; unknown
outcomes reconcile the same invocation. Successful replay audits all attempt
request blobs and any retained earlier response bytes, then re-evaluates actual
stored 3N+1 members. It cannot regenerate or substitute freshly compiled output.

Shared read_draft_response_bytes/read_committed_draft_audit own transport audit
for all three semantic Commands; Stage1/2 migrate to that owner in this wave.
Previous failed raw bytes need integrity checks, not successful-draft parsing.
No local DB, real provider, Runtime success or full-Pipeline claim follows from
the in-memory Command tests. Remote transaction/restart acceptance remains open.
