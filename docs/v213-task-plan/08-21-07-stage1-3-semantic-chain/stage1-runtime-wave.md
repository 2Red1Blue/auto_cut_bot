# Stage 1 runtime integration wave

Status: code implemented; local regression and independent review passed. Remote acceptance remains open.

## Existing boundary

`BuildNarrativeGraphCommand` now owns durable generation, finite transient retry,
unknown-outcome reconciliation, independent seventeen-rule admission and exact
eight-member replay. `read_committed_narrative_graph` validates stored bytes and
actual generation audit, not freshly generated replacement members.

The HTTP runtime now schedules
`source_prep → vlm → stage1_narrative → media_preflight`. The persisted run plan,
worker/reconciler registry and frozen execution profile select the same Stage 1
adapter. Stage 1 success is not full Pipeline success; the remaining semantic
and physical consumers still have to reach Render/QC.

## Integration plan

1. Extract the existing closed Stage 1 request policy into a reusable pure
   `Stage1CommandPolicy`: artifact revision, generation, draft, coverage,
   dependency and retry policy. Its decoder must not manufacture Source/VLM refs
   to validate configuration. Existing Command request bytes must stay unchanged.
2. Add `Stage1NarrativePipelineStage` as a thin adapter. Resolve actual succeeded
   Source and VLM aggregate outcomes through public Store readers. Build exact
   `CommittedSemanticInputsRequest`, then call the same Kernel Command for
   execute/reconcile. Never use raw SQL, fixture builders or process defaults.
3. Freeze the new policy in a versioned Pipeline execution profile, validate its
   installed-policy binding before dispatch/reconcile, and update the persisted
   stage plan and composition together. Historical profiles remain audit-only;
   do not upgrade old run rows implicitly.
4. The transitional plan is `source_prep → vlm → stage1_narrative → media_preflight`.
   Timed speech remains for physical edit feasibility, not a Stage 1 semantic
   prerequisite. Do not report the incomplete plan as a full successful run.
5. Add adapter/profile/plan tests with pure Store doubles. Real PostgreSQL
   restart/concurrency, real Doubao generation and whole-pipeline acceptance run
   only on the remote desktop.

## Ownership

- Policy worker: Kernel Stage 1 request/policy owner and its pure tests only.
- Adapter worker: new runtime Stage 1 adapter and its pure tests only.
- Integration owner: runtime profile/models, composition, plan and any required
  installed profile binding; no private authority or hidden defaults.
- Independent reviewer: targeted changed-code review and pure regression checks.

No external publication is added. Stage 2/3 and the full Task 07 acceptance
criteria remain open until the actual consumers and remote evidence exist.

## Frozen configuration and restart semantics

- `pipeline-execution-profile-v6` stores the complete six-field
  `Stage1CommandPolicy`, including actual text prompt/model, draft limits,
  coverage/dependency policy and finite retry schedule. No environment-derived
  Stage 1 defaults are injected on restart.
- The narrative, shadow-calibration and local-run **source wire schemas** are v2;
  operating profile-state names remain `*_v1`, and the architecture stays v2.1.3.
  The narrative source owns the complete typed policy and a single
  `stage1_command_policy_sha256`. This replaces the three unused coverage,
  dependency and conflict hash slots. Old v1 sources are rejected, not upgraded.
- Standard composition obtains that policy only from the installed source.
  The value owner is `semantic_chain/stage1_command_policy.py`, not the
  execution-layer pipeline facade: isolated profile loading must not require
  importing a database driver. Existing request bytes and hashes are unchanged.
  Before Stage 1 dispatch/reconcile the adapter compares the persisted policy
  and its hash with the installed narrative policy. It uses public committed
  Source/VLM readers; source-prep revision is independently fixed at 1.
- Stage 1 can consume an earlier committed VLM pack whose original frozen
  policy/raw response pass the Store's exact verification. This check does
  **not** promise that every field of the old run matches the current installed
  release. Stage 1 never substitutes the current VLM defaults. VLM and media
  stages retain their own installed-policy checks when they execute.
- Stage 1 does not read ASR/VAD. However, the current whole-runtime composition
  still requires a calibrated installed local-run resource at startup; this
  wave does not offer a calibration-free semantic-only HTTP mode.

## Database and desktop handoff

Migration `0019_stage1_pipeline_profile.sql` accepts new v6 rows and keeps
terminal pre-v6 rows read-only. It refuses unresolved accepted/running pre-v6
runs; it neither rewrites their policies nor inserts Stage 1 into old plans.
Its SQL guard checks closed shape; actual semantic policy validation remains in
the Kernel decoder. PostgreSQL execution/restart/concurrency must be verified on
the remote desktop, not inferred from static SQL tests.

Rebuild installed source resources for source v2 and the Ark VLM request adapter
`doubao-ark-files-responses-stream-v2`; do not change only a version label or copy
test fixtures into installed resources. A real reviewed Stage 1 prompt and
explicit budgets must be provided. Production profile packaging and a real
one-episode run remain open remote acceptance work.

## Local verification and review (2026-08-26)

- Final selected cross-layer suite: **2322 passed**. Scope: semantic-chain and
  VLM suites, exact Store readers/execution-kind tests, exact A/V span,
  Runtime composition/profile/service/adapter and installed-policy tests,
  source grammar and architecture checks (including both isolated wheels).
- Media Runtime pure cases: **2 passed**, 4 PostgreSQL cases explicitly excluded.
- PostgreSQL/control-plane/media files: **63 cases collected only**. They include
  0019 shape/NULL/bounds rejection, active-v5 transaction rollback, immutable
  terminal history and the new four-stage sequence. They were not executed here.
- Changed Python files: Ruff passed. Changed production paths: BasedPyright
  reported 0 errors. Request/hash gold tests stayed unchanged after policy move.
- Independent source, Runtime and final adapter reviews: ALLOW within the local
  code slice. The discovered isolated-wheel database-import dependency and
  incomplete synthetic fixtures were fixed and re-tested, not waived.

This does not prove PostgreSQL migration execution, real model quality, real
calibration, complete Stage 2/3 or a complete Pipeline run. Those remain open.
