# Candidate physical compiler review — 2026-08-26

Scope: candidate dialogue guard, native exact A/V compiler, shared word/VAD/roll
and visual/subtitle leaves, absolute presentation-time coverage and assessment.
Independent read-only reviewer: `review_calibration_migration`. Implementation
and tests were split between root and the existing bounded agents; no new
agents, Claude invocation, DB, model or media service execution was used.

## Findings and disposition

- No production Critical/Warning found in the scoped review.
- Source clocks must be compared as `tick * time_base`, not independently
  rebased to zero. Added delayed-audio valid coverage and rebased-only false
  coverage regressions. Explicit word-only sentence `not_applicable` is retained;
  candidate guard still rejects `unknown` and complete-dialogue requests.
- Review identified one missing regression, not a new production defect:
  `_assess_window` offset boundary-touch. Added a valid delayed-audio root with
  word/VAD crossing the absolute video-window edge; it must detect both touches
  and truncation. Final Prepare suite: 115 passed; independent delta review and
  its single-test rerun passed, closing the Info finding.
- The native search oracle independently enumerates the unfiltered four-way
  Cartesian domain, computes rational pairing and hard predicates, and compares
  the complete relation digest, counts and canonical winner. It does not call
  compiler search/filter helpers. Six tolerance/VAD variants are included.
- Additional cases exercise budget exhaustion after feasible results (no partial
  winner), coarse non-frame edges, gap-spanning pairs, subtitle uncertainty and
  clearance, stable-shot neighborhoods, unknown visual ticks, no legal cut,
  strict values, foreign clocks, and a probe-proven out-only source end with
  unequal A/V tails. Native compiler suite: 20 passed.

## Evidence and limits

Root joint run: 249 related tests passed before the final offset-touch addition.
Independent broader review run: 76 passed before later source-end/UNKNOWN
additions; subsequent delta run: 13 passed. Architecture/import tests: 15 passed.
Scoped Ruff, basedpyright and diff whitespace checks passed. Counts are distinct
runs with overlap, not additive coverage claims.

This allows committing the pure compiler slice. It does **not** establish
physical Admission, a committed Stage4 Command/Recipe, VLM editing-mode policy
derivation, real candidate-local ASR calls, real calibration, rendered A/V or
publication approval. Original source roots and exact predecessor identities
remain unchanged; the actual producer's full-source speech path must still be
replaced explicitly, not hidden by a synthetic root or a `NO_SPEECH` default.
