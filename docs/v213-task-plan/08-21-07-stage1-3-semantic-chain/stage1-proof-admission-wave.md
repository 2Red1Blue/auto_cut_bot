# Stage 1 proof and independent admission wave

Status: core Command delivered; Runtime activation and remote acceptance remain
open. This wave adds proof, independent checks and the real Command, not a
replacement authority framework.

## Dependency proof binding

`DependencyClosureProof` is immutable and strict. Its constructor fields are:

- `dependency_closure_proof_id`, `input_binding_sha256`, `canonical_draft_sha256`,
  `coverage_policy_sha256`, `dependency_policy_sha256`;
- `source_member_ref`, `graph_member_ref`, `event_card_member_ref`,
  `ledger_member_ref` (exact SemanticMemberIdentity, one scope);
- `analysis: DependencyGraphAnalysis` (the existing canonical algorithm values).

Wire payload flattens analysis into `node_refs`, `dependency_arcs`, `sccs`,
`condensation_arcs`, `seed_proofs`. Arc/SCC set hashes and each seed's closure hash
are derived, not caller defaults. Each seed proof contains the existing closure
fields plus `isolation_status` (empty frontier → bounded, otherwise unbounded)
and `closure_hash=SHA256(JCS(affected_refs))`. These are producer claims, not
Admission. A decoder checks shape, canonical order, hashes and structural joins;
only an independent evaluator establishes completeness and the truth of bounds.

`build_dependency_proof(inputs, *, graph_member, event_card_member, ledger_member,
policy, revision)` uses the registered projector and shared SCC primitive. It
expands Ledger-local coverage window roots/frontiers only after Ledger hashing.
It emits one pending ArtifactMember of type `dependency_closure_proof`; it does
not read current heads, generate artifact UUIDs, or set a KC rule to pass.

## Independent verification

`verify_dependency_proof(inputs, *, graph_member, event_card_member, ledger_member,
proof_member, policy)` returns exactly four checked results: `KC-DEP-001`,
`KC-DEP-002`, `KC-DEP-003`, `KC-ISO-001`. Each result is a value with `rule_id`,
`status=pass|fail|indeterminate`, and sorted closed `violation_codes`.

The verifier must not call the producer projector or proof builder. It independently
enumerates the registered Graph attributes/edges and Source/window relations from
decoded members and exact raw input universes, including canonical Event ownership.
Compare complete node/arc identities, not just a producer hash. The proven shared
SCC primitive may be reused over independently reconstructed nodes/arcs/seeds;
producer analysis cannot be its input oracle. Compare SCC membership, condensation
and every closure. Verify exact Ledger seed set and preserved root/frontier,
including nonempty frontier for unresolved identity or context.

Malformed closed payload/owner/hash/projection yields a typed failure or explicit
failed check; unreadability never becomes pass. A nonempty frontier is correctly
encoded as unbounded but fails the first strict-global admission path. Empty seed
sets pass seed-specific checks only after exact empty-set equality is established.

This verifier does not validate all raw-draft coverage semantics, Source grants,
or Store commitment. Those belong to the remaining KC evaluators and Command.
Do not use four passing dependency checks as an eight-member Stage 1 admission.

## Factual and coverage checks

`stage1_members.decode_coverage_members` is the shared strict six-member decoder:
exact member types/logical IDs/scope/revision/hash, followed by each typed payload
decoder. It is not a committed reader or a permission-bearing wrapper.
`Stage1Check(rule_id,status,violation_codes)` is only a checked value; no `pass`
default exists, and it cannot be used as input to bypass evaluation.

`verify_factual_members(inputs, raw_draft, *, members, draft_policy, coverage_policy)`
checks GRAPH-001/002, AUTH-001/002 and EVENT-001. It independently rebuilds expected
factual node/edge/Card/Digest values from exact VLM observations and the decoded
draft, not by calling the producer compiler. Scores come from original evidence.
Unknown or invented identities, altered evidence owners, changed content/ranges,
missing edges and unsupported summary evidence must fail even after rehashing.

`verify_coverage_members` with the same signature checks COV-001 through COV-005,
EXCLUDE-001 and GATE-001. It independently reconstructs the fact/event/obligation/
window universe, assignments and actual failure causes. Compare diagnostics,
claims, seed roots/frontier and row resolution against those causes, not just
declared row flags or totals. First implementation excludes nothing; exclusion
rules pass only after the complete closed row set has actually been traversed.

Neither pure verifier may report KC-IN-001=pass. Only the real Command's exact
Store/Attempt/request/raw-response read can establish that fact. Final Admission
must contain all seventeen results bound to the seven-member business subject;
missing verification is indeterminate, never success by default.

## Final Admission value and evaluation boundary

`CoverageAdmission` contains an ID, input-binding/raw-draft/canonical-draft hashes,
draft/coverage/dependency policy hashes, explicit `coverage_mode=strict_global`
and `evaluation_strategy_version=stage1-kc-v1`, exactly seven business member
identities, and all seventeen distinct KC results. It derives the canonical
seven-member subject hash and repeats that subject on every serialized rule.
No Admission self hash, database UUID or caller permission is part of that subject.
Decoding validates shape and derived fields; it does not establish that checks ran.

`validation_status` covers the sixteen contract rules (not KC-GATE-001): a failure
means invalid, otherwise any indeterminate means indeterminate, otherwise valid.
The policy action is independent: only seventeen passes permit `continue`.
Failures in GRAPH/COV/EXCLUDE request repair; DEP-003/GATE failures quarantine;
other failures stop. Indeterminate GRAPH-002/EXCLUDE/AUTH/DEP-003/GATE quarantine,
other indeterminate rules stop. When several apply, `stop > repair > quarantine`.
This is a recommendation to the executor, not a retry reservation or permission
to publish. Repair requires its own explicit bounded execution policy.

`evaluate_stage1_business_members` composes the five factual, seven coverage and
four dependency checks over exactly seven pending members. It checks evaluator
result coverage without filling missing rows. It never emits KC-IN-001 and never
accepts caller-supplied rule results. A malformed member raises a typed value
failure; the Command must preserve that rejection in its durable audit instead of
committing partial business members. The real Command adds KC-IN-001 only after
exact Store/Attempt/request/raw reads, then constructs the Admission itself.

## Required tests and integration

- Producer/decoder round-trip and malformed primitive/owner/canonical order/hash
  mutations; no Ledger/proof self hash.
- Independently injected omitted/extra/reversed arcs, removed standalone facts,
  whole-window omissions, wrong owner with the same object ID, changed SCC/closure,
  missing/duplicate seeds, forged empty frontier and policy substitution.
- Clean six-member compilation yields seven business members; unresolved data
  retains proof and a failing policy result, never implicit partial success.
- Subsequent Admission binds exactly seven business identities and excludes
  itself; real Command audits the raw draft and commits all eight atomically.
- Local work is code/pure tests only; DB/model/restart and full-pipeline acceptance
  remain remote desktop tasks. No runtime is activated by this proof slice alone.

## Delivered durable Command and exact replay

Store commit `1580e127` adds `read_committed_artifact_set` and
`read_committed_generation_attempt_chain`: exact Job/slot/request/Receipt/set
joins, ordered member hashes, contiguous Attempt ancestry and all Receipt links.
It does not use latest heads or turn a caller-built value into authority.

Kernel commit `0ac205f0` adds the actual `BuildNarrativeGraphCommand`, closed
request compiler, text-only provider port and strict eight-member decoder.
The request stores the exact provider JSON string/hash, semantic input binding,
all policies and registered compiler/evaluator versions. Generation callback
persists the response ID while retaining the current CAS version. Responded
attempts resume from durable raw bytes. Unknown remote results only reconcile;
explicit transient failures may reserve the next bounded/backoff-bound attempt.

Semantic denial retains raw response and the full causal Attempt chain without
committing any partial business set. A crash between failure and Receipt keeps
the same denied classification on re-entry. Successful replay reads persisted
eight-member values and audit refs, independently recomputes all semantic checks,
and rejects a hash-consistent forged stored pass without invoking producers again.

Local evidence: 36 Command tests, including backoff tampering and crash windows;
117 new pure Store reader tests; a selected cross-layer regression passed 2,000
tests before the final added backoff/Ark terminal-event cases. These are pure
tests, not PostgreSQL concurrency or real provider acceptance. Independent review
found no remaining blocker in this delivered scope. Task 07 stays in progress;
[Runtime integration](stage1-runtime-wave.md) and Stage 2/3 remain required.

## Ark adapter correction

Text and video share one Responses streaming/retrieval transport; text drafts do
not invoke Files API. The video adapter moves to
`doubao-ark-files-responses-stream-v2` because the installed official SDK expects
`text.format={type:json_schema,json_schema:{name,strict,schema}}`, not flat schema
fields. Fresh profiles and the three schema mirrors bind v2; persisted v1 is not
silently reinterpreted. Remote deployment must rebuild/install matching profiles.

Failure/incomplete stream events must match the saved created-response ID,
expected model and terminal status before any failure classification. Foreign or
missing identity stays indeterminate and cannot create a retry. This regression
uses real SDK-validated event shapes; single-event SDK validation alone does not
prove cross-event identity. SDK retries are disabled; the HTTP client does not
inherit environment proxy settings. None of these pure checks calls the provider.
