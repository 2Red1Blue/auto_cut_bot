# Stage 4 durable Runtime adapter design

## Decision

Add one dedicated `stage4_recipe` worker stage after `media_preflight`. It performs
only the existing Kernel `CompileProductionRecipeCommand` closure and projects its
durable Receipt. It does not render, QC, publish, expose a physical endpoint, or
inspect latest artifacts.

The stage reconstructs its two predecessor requests through their original request
builders and deterministic idempotency keys:

```text
Stage 3 request + succeeded outcome
Media finalizer request + succeeded outcome
  -> CompileProductionRecipeRequest
  -> CompileProductionRecipeCommand
```

The request is never reconstructed from a receipt payload, an HTTP body, a logical
head, a current default policy, or a provider response.

## Frozen authority

`Stage4RecipeAuthorityProfile` is a closed hash-bound value containing:

- `artifact_revision`;
- `EditorialExactSpanPolicy`;
- `CandidateExactSpanPolicy`;
- `ProductionRecipeCompilationLimits`;
- a registered strategy version.

It is supplied by composition from a protected installed authority source. Missing,
unknown, or mismatched profile identity prevents stage construction. The CPU/CUDA
resolver is selected only from the concrete reconstructed media finalizer request
type. The adapter passes the selected installed resolver to Kernel; it never builds a
parallel speech authority.

## Runtime behavior

- Missing/pending predecessor: `indeterminate`.
- Succeeded Stage 3 and media batch: execute the deterministic Kernel command.
- Terminal predecessor: reject the Runtime stage without calling the Kernel.
- Kernel `pending`/`running`: `indeterminate`; terminal Kernel Receipt projects directly.
- Reconcile repeats the same reconstruction and command invocation; Kernel idempotency
  provides replay rather than a second physical compilation commit.

`stage4_recipe` success is an admitted report→Recipe→Admission ArtifactSet. It is not
a Render result and cannot create a visible media file.

## Required implementation sequence

1. Land the closed authority DTO and its codec/tests.
2. Extract Stage 3 and CPU/CUDA media-finalizer request reconstruction helpers; add
   parity tests against the current Stage 3 and MediaPreflight adapters.
3. Add `Stage4RecipePipelineStage` plus unit/reconcile tests.
4. Version the durable runtime profile/command chain so a new run explicitly includes
   `stage4_recipe`; existing run records stay on their historical chain.
5. Register the port in composition/runner/reconciler and run a disposable PostgreSQL
   stage closure test.
6. Generate a PC shadow authority from calibrated policy input, then run one real
   episode through Stage 4. Do not claim render/QC in this milestone.

## Explicit exclusions

Render/QC requires a separate production-attempt finalizer, verified Blob upload, and
atomic promotion state machine. It follows Stage 4 but is not folded into this adapter.
Chainable R4 edit/revert is also independent of unattended Stage 4 execution.
