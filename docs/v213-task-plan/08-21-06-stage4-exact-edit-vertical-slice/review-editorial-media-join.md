# Editorial/media join review — 2026-08-26

Scope: `pipeline/editorial_timed_media_inputs.py` and its two test files.
Reviewer: independent `review_calibration_migration`; production code unchanged
after initial review. Root owns the added negative tests.

Initial result: no production correctness finding. Test Warning: the fixture
only had one alternative per requirement and did not cover independent Source
owner drift or authorization revocation. Corrected the fixture description:
model replies, detector output and Store I/O are synthetic; Commands/readers
and their evaluators are the production implementations.

Fixes and delta ALLOW:

- Add a second alternative to actual Stage3 raw input, run the real Command,
  and prove all four joined rows and reversed candidate order survive.
- Change the child's Source Command slot without changing its semantic selector;
  the join rejects the mismatched committed Source. Changing the Source receipt
  alone is already rejected by the typed request constructor.
- Revoke render purpose after successful Commands. The recomputed predecessor
  request hash changes and exact Store reading rejects it before purpose checks.
  The test does not claim that execution reaches the later purpose check.
- Six tests, scoped Ruff and type checks pass (root and independent reviewer).
  Earlier combined clock/join/batch regression: 54 passed before the two new
  negative cases. No DB, FFmpeg/model invocation or physical admission executed.

This permits committing the read-only join, not claiming full Stage4 completion.
