# Stage 1 runtime integration wave

Status: in progress; not an activation or remote acceptance claim.

## Existing boundary

`BuildNarrativeGraphCommand` now owns durable generation, finite transient retry,
unknown-outcome reconciliation, independent seventeen-rule admission and exact
eight-member replay. `read_committed_narrative_graph` validates stored bytes and
actual generation audit, not freshly generated replacement members.

The HTTP runtime still schedules `source_prep → vlm → media_preflight`.
Registering a callable alone is insufficient: the persisted run plan and frozen
execution profile must select the same new stage. Stage 1 success is not full
Pipeline success; Stage 2/3/4/Render/QC remain required.

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
4. New semantic execution order is `source_prep → vlm → stage1_narrative`.
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
