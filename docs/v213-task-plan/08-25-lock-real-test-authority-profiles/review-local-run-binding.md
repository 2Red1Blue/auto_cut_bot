# Review — local-run predecessor and accepted calibration binding

Date: 2026-08-26. Scope: local_run_context.py, local_run_calibration.py and tests.

## Independent decisions

Both implementations received ALLOW from an independent read-only reviewer.
No production/test files changed after their corresponding reviews.

- Source context: two independently verified A/B/C chains; four exact predecessor
  fields; current profile/schema class and raw-byte binding; no caller context or
  Registry injection; no bootstrap or runtime permission.
- Accepted-anchor binder: existing Store reader with old shadow/source/Registry
  identity and exact 0/3 references; complete 11-field record identity, producer
  identities, child hashes/bounds and ordered corpus references; no Store write
  or repeated inference. PostgreSQL JSONB normalization is compatible.

## Verification

- New source-context tests: 32 passed, zero skipped (independently repeated).
- New calibration binder tests: 37 passed, zero skipped.
- Combined binder/profile/runtime-boundary/record regression: 128 passed,
  zero skipped.
- Additional record/validator/migration selection: 77 passed, 18 skipped;
  database tests were not enabled in this slice. This is not new database or
  native-inference acceptance; earlier PostgreSQL results remain separate.
- Both production modules: basedpyright zero errors/warnings. All four Python
  files: Ruff passed. git diff --check passed.
- Positive source tests use real synthetic Git chains. Binder tests explicitly
  use FakeAcceptedAnchorReader and synthetic accepted-record bytes with exact
  0/3 references. They do not establish a real accepted calibration.

## Unfinished deployment work

The repository still lacks a real ready eight-pack Registry source. The existing
tracked contracts/source tree is not that format; only synthetic tests produce
the required manifest plus five registry documents. Actual source authoring,
timed-speech registry contract hash derivation, installed-resource emission/loading,
real independent calibration and HTTP activation remain open.

Task remains in_progress. User configuration was neither edited nor staged.
