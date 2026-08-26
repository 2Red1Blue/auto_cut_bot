# Stage 2 current owner gap

## Evidence as of this research slice

The current production path stops after Stage 1.  The only semantic runtime stage
is `stage1_narrative`; `auto_cut_bot/pipeline/runtime/postgres.py:53` explicitly
records that Stage 2/3 are incomplete.  There is no tracked
`semantic_chain/stage2.py`, `pipeline/compile_story_portfolio_command.py`, Stage 2
runtime adapter, or Stage 2 test file.  The matching names found under
`__pycache__` are not source or a callable implementation.

The usable, non-authoritative inputs already exist:

- `autocut_kernel.store.CommittedSemanticInputs` is the exact Store-decoded
  Source grant/window/VLM aggregate boundary.  A VLM pack already has closed,
  Kernel-derived `VlmCandidateHypothesis` and `VlmSemanticMeasurement` objects
  (`vlm/models.py:760-1035`).  Candidate event/fact closure and owner-bound
  support are validated there; Stage 2 must not reparse provider JSON.
- `read_committed_narrative_graph` in
  `pipeline/build_narrative_graph_command.py` is the Stage 1 exact eight-member
  reader and verifies the generation audit, request binding, raw draft, full
  Stage 1 evaluation and Admission.  It is the appropriate Stage-1 predecessor
  reader, rather than caller-built Graph/Card/Ledger DTOs.
- The generic generation lifecycle (`BuildNarrativeGraphCommand`) and the Store
  Command/Receipt/ArtifactSet/CAS APIs are reusable.  A Stage 2 Command can use
  the same claim/attempt/raw-response/reconcile/replay shape without changing
  the Stage 1 kernel.
- `runtime/highlight_projection.py` is presentation-only: it reads committed VLM
  inputs and exposes raw candidate hypotheses for display.  It is not a Stage 2
  compiler and must not be elevated into authority.

The tracked `contracts/source/2_1_3/stage_02/shapes/*.json` files are not a
ready implementation seam.  They describe older generic `ArtifactRef` IDs and
storage locators, but do not supply the required five concrete members, typed
draft decoder, committed Stage-1/VLM joins, Source grant proof, deterministic
selection, or an atomic Command.  They cannot be wrapped as a production
adapter.

## Actual missing production boundary

Per `prd.md` and `design.md`, Stage 2 must produce one atomic, exact five-member
set:

1. `CandidateCatalog` projected from **committed** VLM candidate hypotheses and
   measurements, preserving owner-bound observation hash and canonical nonempty
   `editing_modes` (`dialogue|action`) without ASR/VAD inference;
2. `ProposalSet`, decoded from an audited generated draft, with an explicit
   disposition for every proposal;
3. `StoryPortfolio`, selecting the lexicographically first fully feasible
   proposal tuple and freezing `target_story_ids`;
4. `SourceUsageLedger`, with exact `render_source` grant/source witnesses;
5. one `PortfolioAdmission` whose rules are indeterminate until independently
   evaluated.

None of those models, strict byte decoders, deterministic compiler/evaluator,
committed set reader, request/provider contract, or `CompileStoryPortfolio`
Command exists in tracked source.  In particular, no current reader proves both
an admitted Stage 1 set and the exact VLM candidate universe to a Stage 2
compiler.  Stage 2 therefore cannot start from a Runtime adapter or from the
old contract shapes.

## Smallest next implementation wave

Start with a pure committed-input/compiler slice, not generation or Runtime:

1. Add Stage-2 typed value models and closed `Stage2Draft` decoder, including
   candidate support/measurement references, proposal dispositions, policy and
   admission models.  The decoder accepts only raw provider bytes plus exact
   committed inputs; it does not accept paths, timed-media data, or caller refs.
2. Add an exact Stage-2 input reader contract that combines the existing
   Stage-1 eight-member reader with `CommittedSemanticInputs`, verifies the
   `render_source` grant, and constructs the CandidateCatalog deterministically
   from the VLM pack candidate universe.
3. Add the deterministic portfolio compiler/evaluator: exact candidate universe,
   no silent proposal drop, canonical lexicographic feasible selection, frozen
   targets and five-member structural result.  Keep physical endpoint and
   transcript/VAD feasibility out of the rule set.
4. Only after those pure contracts pass, add `CompileStoryPortfolioCommand` on
   the existing generation persistence seam and its exact committed replay
   reader.  A Stage-2 runtime adapter is a later integration slice.

## Non-overlapping ownership

- **Stage-2 model/compiler owner:** new `semantic_chain` Stage-2 value models,
  draft decoder, candidate projection, evaluator/compiler and pure tests.
- **Stage-2 command/reader owner:** new `pipeline/compile_story_portfolio_command.py`,
  Store protocol additions only if the existing public readers cannot express
  the exact combined predecessor, generation/replay tests, and later PostgreSQL
  tests.  This owner reuses, but does not edit, BuildNarrativeGraph internals.
- **Runtime integration owner (later):** profile policy, installed-source
  binding, plan/composition and a thin Stage-2 adapter.  It must consume the
  public Command/reader, never mint a CandidateCatalog or accepted Portfolio.

This sequence leaves Stage 1 generation intact, does not activate Stage 2, and
does not assert full Pipeline success.
