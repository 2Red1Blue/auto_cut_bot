# Stage 1 diagnostics and Ledger integration

Status: six-member compiler and dependency projection implemented and independently
reviewed; task 07 remains in progress. This is not Runtime or real-pipeline acceptance.

## Compiler ownership

`compile_stage1_coverage` re-decodes the raw draft against the supplied typed input
closure, projects Card/Digest/Graph, and constructs the two diagnostic members and
Ledger in that order. It returns six pending business members, never an Admission
or a Store receipt. The real Command must obtain inputs from the exact committed
reader and bind the audited raw response. Public DTO construction is not authority.

Diagnostics bind both the raw-byte draft SHA and decoded canonical draft SHA.
Each low-confidence cause retains the actual observation, score, threshold and
policy hash; inherited failures must not invent a low score for the affected Event.
Unassigned and missing-summary causes identify their original unit. Merge proposals
retain all observations/rationale as unresolved claims, not accepted identity.
Continuity conflicts retain both original claims; missing context retains only
the one real claim, without fabricating its absent neighbor.

Ledger assignment refs resolve to actual projected Beat/obligation/thread/Digest
objects. Canonical events belong to EventCardSet, never a Graph event alias. Each
unresolved/conflicted row owns a seed rooted in that unit and its actual causes.
Window roots use local Ledger selectors until the Ledger is hashed. Missing context
and unresolved identity retain nonempty frontier; no seed claims isolation.
Counts are structural checks only; independent evaluation must compare exact input
identities, not trust declared totals or caller-built member identities.

## Dependency projection strategy

The explicitly selected `semantic-dependencies-v1` policy freezes all owner,
edge, attribute and external projection rules in its canonical mapping/hash.
Supports/satisfies/causes/resolves propagate forward, requires in reverse;
precedes/involves/contradicts alone do not propagate. Attribute dependencies
include Fact subject/entity value, Event participants, obligations, character
identity/state, relationships, questions and foreshadow setup/payoff.

External projections are SourceWindow → CoverageWindow; CoverageWindow →
Source/Fact/Event; Source → Fact/Event. Standalone facts remain reachable even
without an Event. These are conservative dependency relations, not a statement
that every Source fact is narratively equivalent. The independent evaluator must
enumerate the complete required relations separately before certifying closure.

## Verification and remaining integration

Unit and mutation tests must cover causal provenance, exact member ownership,
canonical draft-local IDs, no self-hash references, unresolved frontier, every
attribute/edge direction, omitted/extra dependencies, and deterministic output.
Independent cross-review is required before committing this wave.

Still required: strict DependencyClosureProof and CoverageAdmission wire owners,
independent KC rule checks, exact eight-member output reader, generation Command,
Stage 2/3 and Runtime integration. No local DB/model/service/full pipeline runs;
those acceptance checks belong on the remote desktop.

## Completed code review and tests — 2026-08-26

- Closed diagnostic values: original observation measurements, local competing
  claims and merge causes; raw-byte and canonical draft hashes remain distinct.
- Ledger values: exact unit vocabularies, canonical owners, one local seed per
  unresolved/conflicted row, local windows, structural count conservation.
- Compiler: six pending members in acyclic order, complete assignments and
  actual cause propagation; no accepted merge, no invented missing neighbor.
- Dependency projector: all registered attributes/edges and external roots,
  exact raw Fact → Graph Fact, raw Event → Graph/Card Event, input window →
  Ledger window domains, and per-window identity sets.
- Independent review found and closed two concrete holes: arbitrary seed reason
  codes/empty unknown frontier; deleting window members or whole windows could
  silently remove external arcs. Enum/frontier checks and exact-domain deletion
  regressions now reject these cases. Transitional missing-module import handling
  was removed after Ledger landed; imports are ordinary static imports.
- Independent reviewers: calibration_contract reviewed root cause/projection and
  diagnostic values; review_calibration_migration reviewed Ledger, compiler and
  dependency corrections. Both scope conclusions ALLOW; neither conclusion is
  a whole-Stage or persistence acceptance claim.
- Final selected regression: **1,236 passed**, including semantic/VLM, execution-kind
  Store unit tests, exact A/V span, Runtime composition and architecture tests.
  All changed production modules pass BasedPyright; changed files pass Ruff.
  The selection did not run PostgreSQL, models, services or a real Pipeline.
- Compiler integration includes a causal Event with directly resolved evidence
  that is still reachable from a tainted predecessor. A resolved row is not an
  isolation certificate. Reviewer also exercised 64 combinations of low-score,
  missing-summary and empty-draft inputs plus an unused low-confidence entity.

Next: implement the strict proof/Admission models and independent KC checks;
then the exact eight-member reader and generation Command. Keep current Runtime
profiles closed until those and downstream Stage 2/3 consumers are migrated.
