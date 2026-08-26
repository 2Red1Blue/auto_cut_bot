# Stage 2 durable Command wave

## Scope

Continue Task07 after pure business delivery `53cda7bc`/docs `76f2b1fd`.
No local database, provider, services or full Pipeline execution. User config is
out of scope. No legacy imports, fixture authority or new governance gates.

## Request and provenance

CompileStoryPortfolioRequest binds the full frozen Stage1 request and its exact
succeeded outcome (Job/slot/Receipt/ArtifactSet), own idempotency key/revision,
explicit text generation policy, prompt byte budget, response limits,
Candidate/Job/Story policies and retry policy. Reuse current registered Ark text
stream policy values, not a second provider implementation. No default prompts
or mutable environment policy lookups. Preparation verifies exact predecessor
record/content, derives CandidateCatalog and semantic binding, and includes the
complete Graph/Digest/Card/candidate context and explicit editorial policies.
No ASR/VAD/physical endpoint request fields; oversized context is rejected, not
silently clipped. Prompt/schema/model/budgets and predecessor identities enter
the durable canonical request; provider body is separately byte-hashed.

## Shared generation execution

Extract the already tested Stage1 transport/attempt lifecycle into a small
pipeline/draft_generation_lifecycle.py owner. It owns claim, request Blob,
reservation, dispatch/reconcile leases, request-ID callback, raw response Blob,
finite provider retry and causal terminal Receipt. It does not compile business
members or grant Admission. Stage1 migrates without changing request bytes,
keys, outcomes or public result/read APIs. Stage2 uses the same execution owner.

Frozen API:

- DraftExecutionPlan(command_name, provider_key_namespace, job, idempotency_key,
  request_hash, request_payload, provider_payload, provider_id, model_id,
  adapter_strategy_version, retry_policy, denial_codes)
- DraftExecutionState(outcome, attempt=None)
- DraftGenerationLifecycle(store, provider).execute(plan) -> state
- .reject(plan, outcome, attempt, code, detail) -> state
- assert_draft_attempt(plan, outcome, attempt)
- read_draft_request_bytes(store, plan, attempt)

Plan provider-key derivation is the existing canonical command namespace/Job/
idempotency/request hash/attempt ordinal mapping. Stage1 namespace remains
BuildNarrativeGraph; Stage2 is CompileStoryPortfolio. Terminal denied codes are
explicit per command. An unknown dispatch reconciles the original invocation,
never blindly creates a successor. Semantically rejected drafts retain raw
bytes and a denied Receipt; model regeneration requires a new explicit request,
not repeated identical-input semantic retry. Provider-retryable failures use the
frozen bounded retry policy. Exhaustion keeps every attempt's causal diagnostics.

## Stage 2 commit and read

Read predecessors with read_committed_story_design_inputs; never execute Stage1
as a side effect. On durable raw response, run actual compiler + independent
seventeen-rule evaluator. SD-IN-001/002 are added only after exact Store inputs,
frozen request/policy and durable request/raw audit comparisons. All nineteen
must pass. Failed/unknown selection creates no partial business members, preserves
its support/search diagnostics in rejection audit, and never reduces target count.
Create the closed Admission over four subjects, decode five members, and call
existing atomic commit_generation_success. Replay re-reads exact five members,
all attempt request/raw bytes and original predecessor; independently recompute
checks and compare all hashes/policies/targets, not fresh substitute artifacts.

Standalone Command policy is explicitly supplied and content-bound. Installed
Runtime-profile matching remains Runtime composition's responsibility; a pure
request is not proof of a registry or active head. Do not falsely label caller
policy values as Store-committed resources.

## Parallel ownership

- Request owner: semantic_chain/story_design_command_policy.py,
  pipeline/compile_story_portfolio_request.py and focused request tests.
- Lifecycle owner: pipeline/draft_generation_lifecycle.py,
  pipeline/build_narrative_graph_command.py and focused lifecycle/Stage1 tests.
- Root: pipeline/compile_story_portfolio_command.py, exact reader and tests,
  task/desktop docs and integration. No overlapping writes.
- Reviewer: read-only targeted review, then combined regression evidence.

## Required verification

Request round-trip/foreign predecessor/content-policy hash/budget tests; exact
five-member positive path; no partial on invalid/unfeasible; one invocation after
retry/restart/reconcile; callback request ID durability; raw/request/admission
rehash tamper; attempts from foreign slots denied; Stage1 migration parity;
Ruff/types and pure semantic/architecture regression. DB restart/concurrency and
real Ark runs remain remote-only acceptance, not implied by MemoryStore tests.

## Delivery and independent review (2026-08-26)

- Request/policy committed in `ff5fef1b`: closed predecessor identity and all
  generation/editorial/budget values; 51 request tests include the actual
  PostgreSQL retry-envelope parser with a fake cursor, without a DB connection.
- Shared lifecycle committed in `a8dcdf53`: Stage1 request/provider keys and
  public outcomes preserved; the generic owner never compiles business values.
- Stage2 Command/reader committed in `9e924be9`: all nineteen independently
  sourced checks, exactly five members committed together, no partial targets
  on infeasible/unknown selection, exact original set references on replay.
- Independent request, lifecycle and final reader reviews: ALLOW. The initial
  reader warning was fixed: every existing prior attempt raw Blob is verified
  for owner/hash/length/media, not only the final response. Failed drafts need
  not decode semantically; only final raw supplies successful business content.
- Root review retained the original canonical JSON encoding for semantic
  denial details (including UTF-16 key order and float rejection). Provider
  failure detail handling is unchanged.
- A cross-layer test still inspected the former private Command provider
  attribute; it now checks the actual shared lifecycle provider, retaining
  model/transport request and response budget assertions.

Final local evidence: 2085 semantic-chain/architecture tests passed; 104
Stage1 adapter/composition/Doubao draft adapter tests passed. The Stage2 Command
file has 32 positive/negative tests, including rehashed raw/request/admission,
later-feasible self-asserted pass, foreign Job/slot and retry-chain corruption.
Changed-file Ruff, production BasedPyright and diff whitespace checks pass.
No local DB/migration/provider/model/service or complete Pipeline was run.

## Next Runtime wave (not yet implemented)

Add a thin `stage2_portfolio` adapter after `stage1_narrative`, before the
existing `media_preflight`. It must reconstruct the original Stage1 request
and succeeded outcome from Store, never execute Stage1 or synthesize members.
Keep the same Command for execute/reconcile. Installed Stage2 policy comparison
must happen before new Store/provider side effects.

Runtime needs an explicit frozen Stage2 policy in its next execution profile,
matching installed local-run resources, plus schema/migration/registry/runner
and reconciler updates in one coherent wave. No stage or profile activation
until those consumers agree. Source-prep's provenance-only request must remain
unchanged: Stage2 policy belongs to the generation request/profile, not source
identity. Do not modify calibration measurements just to add semantic policy.
The exact source-wire placement is to be finalized with its owner before coding.

Stage3, admitted Blueprint -> Stage4/Render/QC, Agent Runtime and remote
whole-run acceptance remain open. Completing Stage2 does not turn the bootstrap
stage sequence into a successful complete Pipeline.
