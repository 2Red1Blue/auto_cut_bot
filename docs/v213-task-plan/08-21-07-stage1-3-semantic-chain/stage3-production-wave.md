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

## Acceptance still to prove

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

Remaining implementation: context/closure and CommandPolicy request budgets,
pure compiled Blueprint/evidence feasibility over the entire frozen batch,
independent Admission, durable Command/exact reader, Runtime activation and real
remote acceptance. No Stage3 admitted output or full-pipeline completion is
claimed by these first two commits.
