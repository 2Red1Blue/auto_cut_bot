# R4B-A SpanVariantSet command / Store design

## Boundary

`BuildSpanVariantSetCommand@1` is a deterministic, zero-provider child of one exact
successful `CompileProductionRecipeCommand@1` execution.  It writes one new
`span_variant_set` member in its own ArtifactSet and Receipt.  It does not modify the
parent Recipe ArtifactSet, create a logical-head alias, create a database table, or
grant edit, render, or QC authority.

Changing request binding, variant ordering, retained-set semantics, or payload shape
requires a new strategy version.  `build-span-variant-set-v1` is therefore frozen with
`provider_call_budget = 0` and a canonical payload limit of 8 MiB.

## Request and result API

The command request must carry both the complete executable parent request and the
complete expected persisted identity:

```python
@dataclass(frozen=True, slots=True)
class BuildSpanVariantSetRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    parent_request: CompileProductionRecipeRequest
    parent_outcome: CommandOutcome
    expected_parent_request_hash: str
    expected_parent_set_hash: str
    parent_report_ref: CommittedArtifactMemberReference
    parent_recipe_refs: tuple[CommittedArtifactMemberReference, ...]
    parent_admission_ref: CommittedArtifactMemberReference
    variant_policy: SpanVariantSetPolicy

@dataclass(frozen=True, slots=True)
class BuildSpanVariantSetResult:
    outcome: CommandOutcome
    committed: PersistedSpanVariantSet | None = None
```

The independent idempotency key is not derived from or shared with the parent key.
The request constructor checks exact types, canonical Job scope/revision, canonical
text, succeeded parent outcome shape, nonzero UUIDs, one parent ArtifactSet owner,
ordered parent member layout (`report`, one or more `recipe`, `admission`), and exact
scope/revision.  These are static checks only; Store truth is checked before claim.

The resolved request hash is canonical JSON over:

- `build-span-variant-set-v1`, `provider_call_budget = 0`;
- Job, output scope and revision;
- the independently recomputed parent request hash;
- parent command slot, Receipt, ArtifactSet and Job identifiers;
- parent ArtifactSet hash and every exact member reference;
- the complete variant policy mapping.

The full `parent_request` remains a typed execution input rather than being duplicated
as a second wire schema.  Its independently recomputed hash is what enters the child
request identity.

## Store Protocol

The command depends on the following narrow protocol:

```python
class SpanVariantSetCommandStore(ProductionRecipeCommandStore, Protocol):
    def commit_span_variant_set_success(
        self,
        request: BuildSpanVariantSetRequest,
        success: CommandSuccess,
        *,
        authority_profile_resolver: AuthorityResolver,
        limits: TimedMediaReadLimits,
    ) -> CommandOutcome: ...
```

Inherited methods used by the command/reader are `claim_command`,
`commit_command_rejection`, and exact `read_committed_artifact_set`.

`PostgresRunStore.commit_span_variant_set_success` must lock the Job then command
slot, independently resolve/read the parent and rebuild the expected single artifact
with the explicit resolver/read limits, require deterministic execution, require the
exact command name and request hash, require canonical Job scope, and only then use
the existing immutable ArtifactSet/Receipt transaction.  The command supplies these
dependencies only as explicit keyword arguments; the Store must not consult a current
default profile.  `commit_command_success` must explicitly raise
`CommandStateError` for `BuildSpanVariantSetCommand@1`; generic success is not an
alternative write route.

No generic-success fallback is permitted in a fake Store either.  Command tests use
the dedicated method so the production-only restriction cannot disappear behind a
test double.

## Execute and replay algorithm

1. Reject a malformed or non-succeeded parent outcome before `claim_command`.
2. Call `read_committed_production_recipe_set` with the complete parent request,
   outcome, resolver, and read limits.  This independently resolves the Stage 3/media
   closure, recompiles/admit-checks Stage 4, and reads the exact parent set.
3. Compare the returned record request hash, set hash, command identity, Job identity,
   and every report/recipe/admission reference with the request.  Any mismatch is an
   input error before claim.
4. Resolve the parent inputs again for variant enumeration.  For each persisted report
   entry, locate its exact candidate and episode-local root/plan/timed-evidence/clock,
   call `compile_candidate_av_span_variants(..., max_variants=K)`, and require result
   ordinal 0 to equal the parent `selected_result` exactly.
5. Assemble `SpanVariantEntry`/`SpanVariantSet` using the compiler DTOs.  No Provider
   method is available on this command path.
6. Claim the independent deterministic child slot.  A succeeded replay never returns
   immediately: it performs the exact child reader below.
7. Enforce the canonical single-member payload ceiling (8 MiB).  Overflow becomes an
   explicit denial; the command must not silently lower K or commit a partial result.
8. Commit only through `commit_span_variant_set_success(request, success)`.
9. Read the exact committed child ArtifactSet and independently repeat steps 2-5.
   Require one member, ordinal 0, exact type/logical id/scope/revision/content hash,
   canonical decoded payload equality, and exact ArtifactSet hash.  Only then return
   `PersistedSpanVariantSet`.

The reader is used for both first execution and duplicate-key replay.  Therefore a
duplicate succeeded slot with corrupted bytes, a changed parent, a changed installed
authority, a request collision, or a changed compiler result fails closed rather than
returning the old Receipt as success.

## Compiler seam

The physical-edit compiler owns the low-level pure enumeration API:

```python
compile_candidate_av_span_variants(
    request,
    root,
    candidate,
    plan,
    profile,
    clock_map,
    policy,
    *,
    max_variants: int,
) -> tuple[CandidateExactSpanResult, ...]
```

`span_variant_set.py` owns `SpanVariantSetPolicy`, `SpanVariant`,
`SpanVariantEntry.from_results(...)`, `SpanVariantSet`, and its closed JSON codec.
The command layer owns traversal from the verified parent report into those APIs; the
physical-edit layer must not import pipeline command or Store DTOs.

## Postgres acceptance cases owned by the parent task

- dedicated writer persists exactly one member and replays the same Receipt;
- generic writer rejects the protected command name directly;
- wrong command name, execution kind, Job/scope, or request hash is rejected;
- parent/request/content mismatch is rejected without a new ArtifactSet;
- no schema migration or new table is present.
