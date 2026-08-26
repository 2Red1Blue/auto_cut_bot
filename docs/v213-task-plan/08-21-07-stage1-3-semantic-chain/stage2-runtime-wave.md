# Stage 2 Runtime integration wave

## Scope and prerequisites

Continue Task07 after ff5fef1b/a8dcdf53/9e924be9 and docs d3fb1650.
Stage2 Kernel request, compiler, nineteen checks, five-member commit and exact
reader are delivered. This wave wires those owners into real HTTP scheduling;
it does not reimplement semantic compilation or claim real Pipeline acceptance.
Use current Trellis development/check guidance, with independent scoped review.
No new governance gates, legacy adapters, caller defaults or external publishing.
No local DB/migration/service/provider calls. User config is out of scope.

## Frozen configuration boundary

Only local-run source advances to `autocut-local-run-profile-v3`. Embed the
complete closed `stage2_command_policy` and its explicit
`stage2_command_policy_sha256`; decode with the existing pure Stage2CommandPolicy
and require exact hash. No fourth source file. Narrative/shadow sources stay v2
and retain their exact bytes/references. CalibrationRecord and accepted detector
measurements must not be regenerated or relabelled to add a semantic policy.

LocalRunProfileSource exposes `.stage2_command_policy` and
`.stage2_command_policy_sha256` (derived from the validated typed policy).
Installed resource compilation, schema, packaging and reconstruction must carry
the new field; absence is not filled from tests or environment. Source format
version and deployment profile version are distinct. Existing anchored
local_run@version cannot be reused for a different registry snapshot: deploy a
new explicit local-run profile_version with matching timed-speech entry version,
reusing the unchanged accepted calibration refs. Keep the existing Store anchor
conflict rejection. No mutation of already bootstrapped anchors.

PipelineExecutionProfile advances to `pipeline-execution-profile-v7`, embedding
`stage2_command_policy_json` internally / `stage2_command_policy` on the wire.
`from_policies(..., stage1_policy=..., stage2_policy=...)` requires both explicitly.
`build_stage2_command_policy()` returns the exact typed policy. Policy, prompt,
model and budget changes change the profile and new Command identity.
Historical v1-v6 stay decodable as read-only terminal history, not executable,
with no old-row rewriting or inferred Stage2 policy.

Source-prep's provenance-only request remains unchanged. Its current Source
grant is checked by actual readers; semantic policy is not source identity.

## Stage adapter and shared predecessor reading

Runtime stage order becomes:
`source_prep -> vlm -> stage1_narrative -> stage2_portfolio -> media_preflight`.
This remains incomplete: no success terminal for a whole run until Stage3 and
downstream stages are integrated. Add Stage2 to both runner and reconciler.

Extract Stage1's existing Source/VLM -> BuildNarrativeGraphRequest reconstruction
into one small shared read-only runtime helper used by Stage1 and Stage2.
Do not call Stage1.execute, copy its reconstruction algorithm or forge a Stage1
context to invoke its private method. The helper reads exact successful
Source/VLM outputs and builds from the frozen Stage1 policy; pending predecessor
returns unavailable, failed/denied predecessor is not reclassified as success.

Stage2 reads the prior Stage1 outcome using the reconstructed Stage1 idempotency
key, requires exact succeeded Job/slot/Receipt/Set and passes that frozen request
and outcome to Stage2CommandPolicy.build_request. The Kernel Command performs
the authoritative independent predecessor reread. Stage2 idempotency is a
deterministic namespace-separated key over run, execution profile and Stage1
request key; the complete predecessor outcome also enters its durable request.
No private writes or DTO-as-commit shortcuts.

Before Store/provider access, the adapter compares both frozen Stage1 and
Stage2 policies with the installed local-run resource. Each stage gets the same
registered Doubao text provider implementation with its own explicit request
and response byte limits. No second transport, unbounded prompts or SDK retries.
Store reconstruction and Command calls run off the async event loop.
Execute/reconcile use the same Command and original invocation recovery.

## Scheduling and migration

Add migration 0020 for v7 closed policy shape and historical write guards;
preserve terminal records. Pre-v7 accepted/running jobs require explicit
resolution before upgrade; do not silently advance their policies/stages.
Runtime stage creation, stage ordinals, predecessor-success checks, profile
validation, runner and reconciler must agree. Database shape checks are not
semantic Admission; typed policy and installed-resource matching remain owners.
Do not edit old migration files to change already defined migration identity.

## Parallel ownership

- Resource owner: Kernel registry authority_profiles/installed_local_run and
  local-run source/resource compiler consumers; local-run source schema;
  tests/authority and the shared tests/pipeline/installed_profile_fixture.py.
  Do not touch Runtime models/composition, Stage adapters, SQL or user config.
- Runtime core owner: runtime/models.py, runtime/postgres.py, runtime execution/
  reconciliation profile guards; migration0020; runtime_profile_fixture.py,
  execution-profile/scheduler tests and necessary test-only call-site updates.
  Do not edit installed_profile_fixture.py, resource/compiler code, composition
  or stage adapter files. Coordinate any shared test before writing.
- Root: shared semantic predecessor helper, Stage1/2 stage adapters,
  composition and their dedicated tests, task/desktop docs and integration.
- Independent reviewer: read-only design/dataflow and final code review.

## Acceptance

Profile/source exact roundtrip, missing/extra/rehash tamper, old profile read-only;
unchanged calibration and Source identity; new anchored version requirement;
Stage2 predecessor pending/terminal/success cases; exact request and stable key;
installed policy mismatch before effects; recovery parity and no complete-run
success; actual composition registration and per-stage transport budgets.
Run pure tests, lint/types and isolated-wheel import checks. Collect DB tests
only; actual migration/concurrency/Ark runs stay on the remote desktop.

## Local delivery and review

Delivered: 92263149 (source/resource policy) and f39b562d (HTTP Runtime).
Independent resource, adapter/composition and core/SQL reviews accepted this
implementation slice. Whole-run terminal success remains disabled.

Closed findings: preserve the historical v6 four-stage terminal tuple; align
test context and installed policies rather than weakening the production guard;
check SQL basic nested shape/NULL; update current DB guard tests to v7 while
preserving explicitly isolated historical-migration tests. Added v1-v6 exact
roundtrip and non-execution coverage. No current v2 production bug was reproduced.

Final local evidence: 299 Runtime/profile/provider-free tests, 2 selected media
pure tests, 413 resource/import tests and 20 source-build/resource/wheel tests.
The later Stage3 addition reran semantic-chain/architecture: 2195 passed.
These groups overlap; they are not a summed acceptance score. Ruff and production
BasedPyright passed. DB/media tests: 65 collected only. SQL execution, real
migration rollback/concurrency, Ark calls and complete Pipeline remain remote.

The desktop runbook now describes v7/local-run-v3 and unchanged accepted
calibration/new local-run version binding. Next is the Stage3 production wave,
not reimplementation of Stage1/2 Commands or another governance bootstrap.
