# Review — locked Registry and shadow source context

Date: 2026-08-26. Scope: locked_registry.py, shadow_context.py and their tests.

## Result

Independent read-only review: ALLOW for both modules. The reviewer made no code
changes. This closes source loading, not the complete authority-profile task.

- Explicit C Git blob is checked against B/A; every inventory blob is verified.
- Registry source coverage, regular Git modes, private materialization and the
  real ready compiler are checked. Checkout/index bytes are not inputs.
- Shadow/narrative/schema sources require exact lock classes and raw hashes.
- Source decoding cannot bootstrap, publish, or enable HTTP. Native identity
  projection remains checked at the existing calibration input boundary.

## Verification

- Targeted new tests: 29 locked Registry + 21 shadow context passed.
- Combined regression: 296 passed, zero skipped (authority suite, Registry source
  compiler, shadow service projection/input resolution/execution).
- basedpyright: both new production modules, zero errors/warnings.
- Ruff: all four new Python files passed. git diff --check passed.
- Tests use synthetic Git A/B/C sources and fixtures. No real calibration,
  database bootstrap, source publication or Pipeline run was performed.

## Remaining implementation

Resolve local-run against its independently verified predecessor lock/Registry
and accepted calibration Store anchor; then package/load the runtime resource.
The current published lock still has no real target profile/Registry sources.
