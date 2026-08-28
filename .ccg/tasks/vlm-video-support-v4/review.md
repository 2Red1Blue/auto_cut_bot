# Independent adversarial review

Reviewer: recompute_design_review; implementation owners were separate. No Claude,
real provider or production database access by the reviewer.

Resolved P1 findings:
1. Outward-rounded PTS falsely overlapped adjacent milliseconds. Semantic order
   and intersections now use exact original integer milliseconds. Fractional
   clock regression covers adjacency and false event/fact/candidate overlap.
2. Installation-only parser digest was not a frozen request fact. V4 now requires
   explicit parser_contract_sha256 through policy/profile/request/payload/reuse;
   Store compares it before original-raw reparse. Old V3 omits the new field.
   Reviewer reproduced old committed request rejection under changed parser
   digest with no extra provider call; new explicit digest changes identities.

Review exit: no additional blocking finding in Generation/Runtime/provider or
Store/Batch -w diff. Same-Job exact SourceReceipt/provenance/claimed proxy,
original raw reparse, V3/V4 batch separation and unsupported Stage1–3 were checked.

Verification checkpoints (overlapping suites; do not sum):
- Whole pipeline+VLM offline: 1761 passed, 218 skipped. Excludes the pre-existing
  legacy `test_artifact_cache.py` collection failure (`autocut_core` unavailable).
- V4 Store + old Generation/Batch real disposable PostgreSQL: 39 passed.
- Profile0030 + thinking + run-store real disposable PostgreSQL: 218 passed.
- Independent targeted review: 204 passed, 1 database skip; pure Kernel Ruff clean.
- Changed modules strict type checks clean except two pre-existing reported
  calibration type errors in unrelated postgres.py lines2697–2698.

Real enablement is a separate checkpoint: no claim that fixture success proves
real video semantics, full pipeline, ASR/VAD, rendering or publication.

## Bounded prompt6 optimization checkpoint — 2026-08-28

Independent reviewer: recompute_design_review; implementation split between
bounded prompt/schema, profile migration tests, and root Runtime/provider wiring.
No Claude, real provider dispatch, or database startup in this checkpoint.

No P0/P1 findings remain in the scoped implementation. The reviewer explicitly
checked generation-subset versus unchanged V4 admission, declaration capacities,
typed references, old wire preservation, early schema rejection, and the 0031
validation-only projection. Final installed-authority changes passed independent
150-test regression; old runs still reconstruct their own frozen profile.

Final overlapping verification:
- pipeline + VLM: 1830 passed, 257 skipped (same pre-existing artifact-cache
  legacy collection exclusion).
- Ruff: all changed Python files clean.
- BasedPyright: five modified/new production modules clean.
- PostgreSQL 0031 tests added but NOT executed in this checkpoint: Podman VM is
  stopped; do not report prior database runs as verification of this schema.

Measured same-window input_text: 4517 -> 3380 UTF-8 bytes; schema 13151 -> 10797.
These are not token measurements or proof of improved real output. No fifth
paid call. New installed default requires 0031 before creating a new run.
The wider V4 task remains open for real semantic success and downstream readers.
