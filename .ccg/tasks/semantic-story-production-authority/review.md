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

## First real PC run

- SourcePrep and ContextPrepare succeeded for one real episode.
- The VLM provider completed three times, but the strict parser exhausted its
  retry budget: non-canonical tag order, one unknown fact reference, then a
  candidate measurement outside its declared semantic closure.
- The first raw response hit the enum-order rejection before the parser could
  report a later candidate-support overlap error. Enum ordering is still an
  over-strict format gate, but normalizing it does not claim that the complete
  response is admissible; semantic validation continues after normalization.
- V4 provider parsing now normalizes `editing_modes`,
  `narrative_functions`, and `tags` into their registered enum order. Unknown
  values and duplicates still fail, persisted mappings remain canonical, and
  all reference-closure checks remain fail-closed.
- Updated verification: 317 passed, 1 environment-dependent test skipped;
  Ruff and `git diff --check` passed.
