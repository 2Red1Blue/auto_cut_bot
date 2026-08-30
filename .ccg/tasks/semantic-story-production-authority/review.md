# Review

## Outcome

Local review: allow for Git/PC validation. The change removes the accidental
`semantic_story -> installed local-run -> timed media` dependency without
widening the semantic runtime into ASR/VAD, physical edit, render or
publication.

## Contract checks

- The installed semantic authority is closed JSON, digest-bound and owns exact
  VLM plus Stage 1/2/3 command policies.
- Stage adapters accept either the historical local-run authority or the new
  semantic authority, never both. Full-pipeline behavior is unchanged.
- Persisted V11 policies must equal the installed semantic policies before any
  Stage 1/2/3 request is read or dispatched.
- Stage generation uses the existing strict Ark `json_schema` request path;
  models cannot supply Admission, material-support, physical-pass or publish
  decisions.
- The Stage 1-3 prompts prohibit ASR/VAD, physical endpoints and publication
  data. Stage 2/3 retain deferred physical requirements.
- Production policies use the configured Doubao production model and contain
  no synthetic test model or prompt identifiers.

## Verification

- Ruff: passed for all changed Python files.
- Targeted runtime/authority/semantic-chain tests: 306 passed.
- Migration tests: 14 passed; the real PostgreSQL V11 test was skipped locally
  because this Mac test process has no configured database fixture.
- Broader `tests/pipeline` collection is currently blocked by the unrelated
  optional legacy dependency `autocut_core` in `test_artifact_cache.py`; the
  affected semantic/runtime suites were executed explicitly instead.
- `git diff --check`: passed for scoped files.

## Remaining runtime evidence

PC must pull the commit, validate the V11 profile against the real PostgreSQL
functions and run one real episode. A new `semantic_story` run may create a new
VLM request identity; this patch does not silently relabel an existing
`semantic_only` V10 run as V11.

## Real PC evidence and adversarial follow-up

The first one-episode V11 run proved SourcePrep and ContextPrepare, then
exhausted all three VLM attempts before Stage 1:

1. `NONCANONICAL_ENUM_SET`: candidate tags used a valid but non-canonical order.
2. `UNKNOWN_REFERENCE`: event `e020` referenced undeclared fact `f049` after the
   response had reached the 48-fact bound.
3. `SEMANTIC_PACK_INVARIANT_VIOLATION`: a candidate measurement referenced
   facts outside its declared event closure.

The first response also contains a later candidate-support overlap error that
was masked by the earlier tag-order rejection. This is evidence that V23 asks
one expensive video call to produce two different products at once: factual
video observations and editorial candidate hypotheses with redundant support,
closure, ordering, and measurement representations.

Changing the frozen V4 parser in place was tested and rejected: startup
reconstruction immediately refused persisted V4 profiles whose bound parser
hash differed. Commit `60b7ec18` was therefore reverted by `7649e3d8`; old
profiles remain replayable.

Recommended next contract is versioned rather than patched in place:

- expensive VLM pass returns admitted core observations only;
- a cheaper text-only candidate compiler consumes the admitted local-ID graph;
- candidate support is deterministically derived from selected event support,
  not echoed redundantly by the model;
- model-visible candidate fields contain only editorial choices; Kernel expands
  aliases, canonicalizes order, recomputes closure, and records a separate
  Candidate receipt;
- old V23/V4 runs retain their exact resolver and parser identities.

This is now the P0 input-contract issue blocking a reliable Stage 1-3 real run.
