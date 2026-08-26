# Stage 3 HTTP Runtime integration wave

## Entry and scope

Continue Task07 after the independently checked Stage3 Admission/Command slice.
This is wiring of the existing shared Command, not a second Blueprint compiler.
No local database/migration/model/service execution. Remote desktop owns actual
acceptance. External publication remains closed; user configuration is not an
implementation input. Keep saving tested coherent commits on
`feat/v213-contract-codegen`.

## Frozen policy and resource ownership

Advance only local-run source to `autocut-local-run-profile-v4`, carrying the
complete closed `stage3_command_policy` and its canonical
`stage3_command_policy_sha256`. Reuse Stage3CommandPolicy as the decoder and
validation owner, with explicit generation/draft/context/feasibility/retry and
whole-provider-body byte limits. No caller defaults, fourth source file or
physical edit policy inside the semantic request.

Narrative/shadow sources retain their existing bytes, references and accepted
CalibrationRecord. Adding a semantic policy must not regenerate ASR/VAD
calibration. A changed registry snapshot needs a new explicit local-run profile
version and matching timed-speech entry version; never overwrite an anchored
profile version. Resource compilation and isolated-wheel reconstruction must
carry all three semantic policies without importing the application into Kernel.

Execution profile advances to `pipeline-execution-profile-v8`. The internal
field is `stage3_command_policy_json`; the wire field is the closed
`stage3_command_policy`. from_policies requires the exact Stage1, Stage2 and
Stage3 policies; build_stage3_command_policy reconstructs the same typed value.
Historical v1-v7 remain decodable only for terminal history. Do not backfill
missing policy values or silently resume old active runs under new policies.

## Predecessor and thin stage adapter

Runtime order becomes:

`source_prep -> vlm -> stage1_narrative -> stage2_portfolio -> stage3_blueprint -> media_preflight`

Keep whole-run terminal success disabled until Stage4/Recipe/Render/local QC
are connected. Both execution and reconciliation register Stage3.

Move the current Stage2 request reconstruction and Stage2 idempotency key into
the existing semantic_predecessors owner, then migrate Stage2 and Stage3 to it.
Do not invoke another stage's execute/private method, forge a stage context or
copy Source/VLM/Stage1 reconstruction. The helper returns the exact Stage2
request or pending; failed/denied predecessors remain terminal errors.

Stage3 reads the succeeded outcome using that frozen Stage2 request key, then
calls Stage3CommandPolicy.build_request with the exact predecessor request and
Job/slot/Receipt/ArtifactSet identity. Its idempotency key is namespace-separated
over run ID, complete execution-profile hash and Stage2 request key. The Kernel
Command still owns actual predecessor reading, request/raw audit, independent
Admission and atomic 3N+1 commit/replay.

Before Store/provider access, compare all three persisted semantic policies
with the installed resource. Use the existing streaming DoubaoDraftProvider,
with Stage3's own explicit byte limits. Run synchronous Store/request/Command
work off the async event loop. Execute and reconcile use the same Command and
durable invocation recovery; adapters do not decide semantic pass or publish.

## Migration and ownership

Add migration 0021 for v8 policy shape, current-write guards and six-stage
scheduling. Preserve prior migrations and terminal history. Pre-v8 active runs
require explicit resolution before upgrade, not in-place policy changes.
SQL shape guards do not replace typed policy or semantic Admission.

- Resource owner: registry authority_profiles/installed_local_run, source
  compiler/schema, authority tests and installed_profile_fixture only.
- Runtime core owner: models/postgres/profile guards, migration0021,
  runtime_profile_fixture and scheduler/profile tests. No resource fixture edit.
- Root: semantic_predecessors, Stage2/Stage3 adapters, composition, dedicated
  adapter/composition tests, task/desktop documentation and commits.
- Reviewer: read-only focused diff, regression and integration boundary review.

One writer per file; coordinate shared fixtures explicitly. No new governance
framework, legacy bridge or parallel semantic authority.

## Acceptance

- Exact policy roundtrip/hash, missing/extra/rehash negatives; v1-v7 read-only.
- Unchanged calibration and Source identity; anchored versions cannot mutate.
- Pending/failed/succeeded predecessors; exact identity and stable Stage3 key.
- Installed-policy mismatch rejected before effects; no private writes.
- Complete runner/reconciler/composition registration and independent budgets.
- Command replay retains actual complete 3N+1; no provider regeneration.
- Pure tests, scoped lint/types and wheel boundaries. Collect database tests
  only here; migration/restart/Ark/real episode acceptance stays remote.

This file freezes the next implementation slice; it does not claim HTTP Stage3
is implemented or remotely verified.
