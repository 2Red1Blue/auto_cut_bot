# Stage 2 production wave: candidates, proposals and exact portfolio

## 1. Scope and evidence

Status: implementation in progress, not Runtime-ready. Stage 1 is delivered in
`af2c9041`; Stage 2/3 remain open. This wave implements actual committed v3
Candidate/Proposal semantics, not the removed fixture prototype. Real database,
provider and whole-Pipeline acceptance remain remote-only. External publication
stays closed.

This note resolves the older Stage 2 principle examples against Task07 and the
current owners. It is not a replacement for narrative/physical separation,
atomic five-member commit, independent evaluation or finite audited generation.

## 2. Resolved design corrections

1. **Generation ownership:** CandidateCatalog is a deterministic projection of
   committed `vlm_semantic_pack_set` children. Their original VLM audit remains
   the evidence; do not fabricate a `candidate_enrichment` invocation. Only
   `story_proposal` adds a Stage 2 provider invocation. The older two-purpose
   example does not describe this v3 strategy.
2. **Exact references:** reuse `SemanticMemberIdentity` and `SemanticObjectRef`,
   with complete type/logical-ID/revision/scope/content-hash identity. Event cards
   and coverage windows retain their actual owners. Sources use the committed
   `whole_series_source_manifest` and must grant `render_source`. Stage 1 read
   must replay its exact eight-member result and independent admission first.
3. **Capabilities:** preserve v3 `editing_modes` and `narrative_functions`
   verbatim; dialogue/action are not mutually exclusive. `hook_and_orient` is
   not an alias for v3 `hook`. No ASR/VAD/text heuristic or caller override.
4. **Coarse support:** mapped ranges are uncertainty-expanded outer envelopes.
   They cannot prove a minimum usable duration. Derive the conservative inner
   duration from original proxy endpoints, declared provider uncertainty and
   exact timeline-map error using rational arithmetic. Empty inner support is
   zero, not a repaired interval. Increasing error cannot increase support.
   This is only a lower bound under declared semantic timing error, not proof
   of safe cuts, complete speech, or final Story duration. Do not sum alternatives
   or overlapping ranges to satisfy one requirement.
5. **No self-hash cycle:** ordered member DAG is CandidateCatalog → ProposalSet
   → Portfolio → SourceUsageLedger → PortfolioAdmission. ProposalSet contains
   every draft and local disposition, but no Story ID and no selected flag.
   Portfolio derives Story IDs from the completed ProposalSet identity and
   proposal ID. Admission subjects are exactly the preceding four members.
6. **Invalid versus unselected:** malformed drafts, unknown references, illegal
   enums/durations invalidate the complete generation; retain raw audit, commit
   no partial business set. Structurally valid but unsupported alternatives can
   remain diagnosed in ProposalSet. A feasible unselected proposal is not a
   failure. Original proposal indexes/counts and selected count never shrink.
7. **Selection is not scoring:** freeze `first_feasible_lexicographic_v1` and
   explicit search budget. No unused objective-weight fields or score ordering
   in the first strategy. VLM measurements drive capability validation only.
   This supersedes the old example of scoring within a feasible set.
8. **Physical handoff:** only closed dialogue/visual/subtitle requirements and
   their canonical hash pass forward. No fulfilled/pass or physical endpoint
   in Stage 2. Stage 4 independently checks actual evidence and source grants.

## 3. Minimal owned models and algorithm

- Candidate catalog: exact VLM candidate/support and Graph/Card/Ledger joins,
  canonical decimal measurements, source grants and coarse support lower bound.
- Proposal draft: narrative claim, exact Graph refs, explicit duration/profile/
  genre/teaser, and material requirements. Required fields have no business
  defaults. Draft byte/text/array limits are frozen, no silent context clipping.
- Job/Story policy: proposal-count range separately from selected-story count,
  duration bounds, explicit source restrictions, completion policy, allow-listed
  editorial values and fixed selection strategy. Every retained field drives a
  concrete validation or selection decision; speculative optimization is absent.
- Each material requirement has candidate/source alternatives, each satisfying
  all its semantic, authorization, taint and duration conditions together.
- Iterate fixed-length proposal-index combinations in lexicographic order. For
  each, backtrack in requirement order and canonical alternative order to find
  a joint assignment. Under `source_reuse=forbid`, a Source can serve several
  requirements of one Story, but not two different Stories. A Story may require
  several Sources. Never use first-candidate greedy or pairwise feasibility.
- Count every inspected proposal tuple and assignment edge against one explicit
  deterministic budget. Exhaustion returns indeterminate immediately, not
  infeasible and not permission to select a later tuple. Full exhaustion with
  no witness returns infeasible. Independent evaluation checks exact witness
  coverage plus canonical first-feasible selection, not producer self-approval.
- Solver values carry no commitment/admission capability. Command owns exact
  reads, generation audit, atomic persistence and replay. No partial catalog
  commit before proposals/admission are ready.

## 4. File ownership and verification

Parallel ownership (no overlapping writes):

- proposal owner: `semantic_chain/story_design_models.py`,
  `story_design_draft.py` and matching tests;
- candidate owner: `semantic_chain/candidate_catalog.py`,
  `candidate_projection.py`, `candidate_duration.py` and matching tests;
- integration owner: `semantic_chain/portfolio_search.py`, remaining portfolio
  compiler/evaluator, exact committed input seam and Command, related tests/docs;
- independent reviewer: read-only review and independent small-space oracle.

Required regressions: malformed/unknown fields, exact-owner mismatches,
nonexclusive capability round-trip, uncertainty monotonicity, self-hash absence,
A={X,Y}/B={X} feasible reassignment; three Stories sharing only X,Y infeasible;
one Story requiring X and Y; budget exhaustion before later feasible tuple;
full raw proposal retention; admission tamper and restart/replay. Test-only
fixtures cannot become production authority. Use Ruff, BasedPyright, pure
semantic tests and isolated-wheel/import checks; database acceptance remote.

## 5. Remaining end state

Pure values/search alone do not finish Stage 2. Delivery still requires a
five-member compiler, independent evaluator, strict decoder, audited durable
Command/exact reader, shared Runtime registration, installed frozen policy and
remote real-model/restart acceptance. Stage 3 Blueprint/all-or-nothing batch and
Stage 4/Render integration remain subsequent Task07 work.

## 6. Incremental evidence

First slice: exact finite assignment solver and the read-only committed Stage 1
predecessor seam are implemented. 19 solver tests include 1728 small universes
under both reuse policies; the independent reviewer additionally checked 794
product-oracle/budget cases. Seven predecessor tests run the actual Stage 1
Command/reader over an explicitly test-only persistence double. Both slices
received independent ALLOW, Ruff and production BasedPyright checks passed.
This proves neither PostgreSQL execution nor Stage 2 completion.
