# v2.1.3 Replacement Implementation Path

## Decision

The implementation proceeds by replacing incorrect ownership boundaries, not
by enabling the current Stage 1-3 prototype one exception at a time.

The following foundations are retained because they already have focused tests
and an independent authority boundary:

- PostgreSQL Command, Receipt, ArtifactSet, Blob, CAS and exact replay;
- Source/window identity and integer piecewise timeline mapping;
- Doubao Ark Files/Responses streaming, durable attempt and reconcile state;
- canonical JSON values and exact integer A/V span primitives.

The following components are replaced as complete contracts:

- coarse VLM observation v2;
- implicit Source authorization;
- the unused Stage 1-3 production prototype and its synthetic rule passes;
- fixture semantic commands and adapters that accept caller-built authority.

This is a selective rewrite, not a repository rewrite.

## Ordered vertical slice

```text
Committed Source + content-bound operation grant
  -> Doubao VLM semantic-pack v3
  -> BuildNarrativeGraph / CoverageAdmission
  -> CompileStoryPortfolio / PortfolioAdmission
  -> BuildEditorialBlueprint / SemanticFeasibilityAdmission
  -> exact timed evidence and Stage 4 physical compiler
  -> deterministic render and local Publication QC
```

No downstream stage may compensate for a missing upstream field with a
heuristic default.

## VLM semantic-pack v3 boundary

Provider output is a closed local semantic pack containing:

- window summary and continuity;
- local visual entities;
- visible facts;
- events whose fact references close locally;
- optional highlight/hook hypotheses with semantic measurements;
- integer proxy intervals, conservative uncertainty and allow-listed frames.

The Kernel derives source intervals, global IDs, core ownership, request and
manifest identities, raw-response hash and every persisted reference.

Stage ownership is strict:

- Stage 1 consumes entities, facts, events and continuity.
- Stage 2 consumes candidate hypotheses and semantic measurements.
- Stage 3 consumes only admitted Stage 1/2 references.
- Stage 4 alone reads ASR/VAD/frame/sample/subtitle evidence and chooses
  physical endpoints.

The pack contains no float seconds, final cut point, lead-in duration,
Transcript/VAD input, Source authorization or publication decision.

The v3 switch is incompatible. Prompt, JSON Schema, parser, decoder, artifact
type, exact reader, batch finalizer and tests move together. Historical v2
artifacts remain audit-only; there is no v2-to-v3 authority conversion,
dual-write or compatibility field map.

## Source operation grant

Source preparation commits the following content-bound minimum:

```text
authorization_id
authorization_policy_sha256
series_id
authorized_purposes  # canonical subset
sources[]             # source_id + content_sha256
```

The first local vertical slice recognizes `semantic_analysis` and
`render_source`. Missing purpose is deny.

- Stage 1 independently requires `semantic_analysis`.
- Stage 2 may select only a Source carrying `render_source`.
- Stage 4 rereads and verifies the same committed grant; a Candidate witness is
  not a permanent capability token.
- External publication remains a separate decision and is not granted here.

## Replacement Stage 1-3 API

```text
semantic_chain/
  authority.py
  rules.py
  stage1.py
  stage2.py
  stage3.py

pipeline/
  build_narrative_graph_command.py
  compile_story_portfolio_command.py
  build_editorial_blueprint_command.py
```

Compilers are pure functions over typed committed authority, an audited parsed
draft and a frozen policy. Commands own claim, provider audit, exact reads,
atomic persistence and replay.

Rules start `indeterminate`. Only the evaluator that performed a named check
may set it to pass. No helper may fill all unspecified rules with pass.

The first slice deliberately supports:

- Stage 1 `strict_global` coverage only;
- deterministic Stage 2 target freeze;
- one unpartitioned Stage 3 batch with one batch Admission;
- all-or-nothing business ArtifactSet commits.

Partitioning and dependency-scoped isolation are added only after real failure
data proves they are needed.

## No-patch gate

Before changing code, classify the finding as a contract gap, domain-model gap,
implementation defect or test/fixture gap.

If one root cause affects two or more modules, do not patch its consumers.
Change the owning contract/model once, migrate every consumer and add one
cross-layer regression test. Compatibility re-exports, request translation,
dual writes and calls to an old builder are not allowed on this migration
because v2 never produced executable production data.

The unused old Stage 1-3 and fixture authority are removed at the v3 contract
cutover. Until the replacement passes exact Store tamper tests, PostgreSQL
restart/replay, Pipeline/Agent conformance and one real Doubao episode, the
semantic pipeline remains explicitly fail-closed. This temporary lack of an
admitted semantic path is preferable to two coexisting authorities.

Source/Window canonical decoding is owned by Kernel. Store and both runtimes
consume that decoder; Kernel must never import an application runtime to
reconstruct committed evidence. Any repeated decoder discovered in an app is
migrated to the Kernel owner and removed from the app in the same change.

## Required acceptance gates

- Provider-forged Source, Window, request, core-owner or Artifact fields fail.
- Legal low-confidence facts persist and become Stage diagnostics; parser
  structure validation does not silently discard the entire pack.
- Candidate hypotheses may be empty; ordinary dialogue or setup is not forced
  into a highlight.
- Stage 1 has exact one-to-one coverage and fails closed on unresolved,
  conflicted, tainted or unauthorized units.
- Stage 2 cannot infer editing mode from ASR, VAD, summary text or a local
  heuristic and cannot select an unauthorized Source.
- Stage 3 cannot reference raw VLM text or emit PTS, Transcript, VAD or a render
  decision.
- Each successful Command replays the same Receipt, ArtifactSet and member
  references without another provider invocation.
- Pipeline and Agent runtimes produce the same business hashes for identical
  committed references and policy hashes.
- One real drama episode reaches an admitted Blueprint before Stage 4 is
  enabled; the 45-episode run starts only after the one-episode slice passes.
