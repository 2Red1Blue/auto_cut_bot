# Stage 1 narrative projection and coverage analysis — 2026-08-26

## Delivered scope, not whole-Stage acceptance

This wave implements actual Graph/Card/Digest business values and the direct
observation coverage analysis. It does not activate BuildNarrativeGraph, grant
Admission, persist an eight-member set, or enable a new HTTP stage. Task 07
remains in progress.

- `narrative_models.py`: closed, immutable EventCardSet, EpisodeDigestSet and
  NarrativeGraph values; eleven Graph attribute variants and exact Fact values.
  Only earlier member/object owners may be referenced. External existence,
  source authorization and semantic truth remain evaluator responsibilities.
- `narrative_projection.py`: deterministic committed-VLM/draft projection to
  the three pending members, with factual Cards, all observation entities and
  standalone Facts, explicit participants, declared summary evidence and
  stable draft node IDs. No name-based identity merge or physical cut endpoint.
- `continuity_analysis.py`: compares only touching windows with equal
  source ID/hash/stream/clock. Preserves both continuation claims for conflicts
  and reports missing context at open edges or gaps. Compatible true flags do
  not prove identical events or states. Ambiguous neighbors reject.
- `coverage_analysis.py`: re-decodes the raw draft and retains one row per
  Fact/Event/window/obligation. Source grant is checked by purpose AND exact
  source ID/hash. Policy has an explicit exact-decimal minimum confidence,
  no implicit threshold and only the implemented strict-global strategy.

The source reference owner remains the exact whole-series Source member.
Raw observations use VLM-owned vlm_entity/vlm_fact/vlm_event. Event nodes point
to EventCard-owned canonical events; local draft assignment IDs in the analysis
are NOT domain references and must be resolved by the upcoming Ledger compiler.

## Coverage decisions that must not drift

1. A Fact/Event not used by a Beat can be supporting only through explicit
   declared summary evidence or an assigned narrative requirement. Mere Graph
   presence is not a supporting fallback.
2. Unassigned or insufficiently supported units remain unresolved/unassigned.
   No exclusion, water-content classification, automatic merge or fabricated
   Event is generated.
3. A required Fact assignment expresses the intended narrative responsibility;
   it does not prove arbitrary free-text success criteria have been fulfilled.
4. Unproven merge proposals preserve observations separately, retain the proposal
   cause hash and cause identity_unresolved. Future diagnostics must retain the
   full raw proposal/evidence; future proof must preserve its unknown frontier.
5. Direct resolution is not transitive isolation. For A causes B, A may be
   directly unresolved while B has sound direct evidence. The window still
   fails strict-global coverage. Dependency projection must propagate A to B;
   B.resolved must never be interpreted as an untainted closure or Admission.
6. A missing adjacent window or contradictory continuation claim remains
   structured and traceable. It is not Store corruption and is never fixed by
   silently changing a VLM boolean.
7. Content hashes, raw-response hashes and committed provenance remain distinct.
   These pure functions neither establish nor fabricate database commitment.
8. A summary with no declared Fact/Event evidence is retained with exact
   source-window provenance, not rejected before diagnostics. Provenance is not
   grounding: its window has summary_evidence_missing even if the draft assigns
   every Fact/Event. This does not make those units supporting automatically.

## Review and verification

- Narrative models: 273 focused tests, independent read-only ALLOW for this
  value-model scope; local Ruff and production BasedPyright pass.
- Continuity: 42 focused tests and independent main-agent review; full claim
  shape, four-dimensional source grouping and missing-neighbor regressions.
- Coverage: 24 focused tests. Independent review ALLOW for direct coverage;
  the identified direct-resolution versus causal-isolation boundary is now
  documented and has an explicit A-to-B regression.
- Projector: 12 focused tests. Review fixes preserve Decimal precision (including
  under a low ambient decimal context), deduplicate shared requirement evidence,
  and combine reciprocal cause/effect claims into one edge retaining both raw
  declarations. A further independent finding removed premature rejection of
  summaries with no declared evidence; this is coupled to the explicit missing-
  summary-evidence coverage regression above.
- This workstation did not run PostgreSQL, migrations, providers, ASR/VAD,
  services or the complete Pipeline. Synthetic Store-shaped tests are not
  durable database acceptance.

Final combined pure regression: **776 passed** across semantic_chain, VLM,
Command-kind unit/lifecycle checks, exact A/V span, Runtime composition and
architecture/package isolation. Changed production modules pass BasedPyright;
changed production/tests pass Ruff. Independent model/projector delta review
is ALLOW after the missing-summary-evidence finding was corrected.

Code checkpoints: models 701d3056, continuity 3371bfaf, direct coverage ae049a65,
projection 4966e27e. The subsequent coverage fix keeps summary provenance
distinct from semantic evidence; no runtime stage was activated by these commits.

## Next implementation, still required

1. Produce closed EvidenceDiagnostics/ConflictDiagnostics, CoverageLedger with
   locally owned seeds/windows, and the full Graph/attribute/external dependency
   projection plus DependencyClosureProof.
2. Implement the seventeen KC evaluators independently from compiler results;
   bind CoverageAdmission to exactly seven business member identities, never
   to itself. Mutation tests must prove omitted facts/arcs/claims cannot continue.
3. Add the real generation Command, exact eight-member reader and atomic
   commit/replay integration. Re-decode audited draft bytes at the Command;
   never treat public Python DTOs as acceptance capabilities.
4. Synchronize affected Stage 1 wire examples and remove inactive partial owners
   before Runtime activation. Then continue Stage 2, Stage 3, downstream Stage 4,
   Render/QC and both Runtime/HTTP connections.
5. Run real services and the complete local-video workflow on the remote
   desktop. Real-run completion is not claimed by any local test count.
