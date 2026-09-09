# Durable Stage 4 runtime adapter analysis

Design the smallest production-correct implementation that makes a durable HTTP
Pipeline run execute `stage4_recipe` after both committed Stage 3 and
MediaPreflight succeed.

Hard constraints:

- The Runtime adapter must reconstruct exact predecessor requests/outcomes and
  invoke the existing `CompileProductionRecipeCommand`; it cannot reconstruct
  from a receipt, caller JSON, latest lookup, or defaults.
- CPU and CUDA timed-media batch types must select their corresponding installed
  authority resolver and use only hash-bound Stage 4 policies.
- The worker stage must participate in normal claim, lease, replay and reconcile
  semantics. A succeeded result means only an admitted Recipe ArtifactSet, never
  a rendered video or published output.
- Do not put Render/QC, HTTP UI editing, or any arbitrary physical endpoint into
  this slice.

Return the required profile/authority DTO shape, predecessor helpers, runtime
scheduling/profile migration plan, failure/reconcile semantics, and focused tests.
