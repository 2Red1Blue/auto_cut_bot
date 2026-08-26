# Installed packaging and startup binder review — 2026-08-26

Scope: explicit local-run resource preparation; standalone Kernel package data;
fixed-resource startup resolver; separate admin bootstrap; corresponding tests.
No HTTP activation or actual measured production resource is claimed by this
checkpoint.

Independent reviewer: review_calibration_migration, read-only.
Result: no production Critical finding. One test-isolation Warning was repaired:
the architecture wheel test now establishes its own tools import path instead of
depending on collection of the sibling authority suite. It does not replace the
global tests module namespace.

Evidence:
- Independent combined packaging/startup/bootstrap suite: 29 passed.
- Root rerun after isolation repair: 29 passed.
- Actual root and standalone wheels were built and installed into clean temporary
  virtual environments; fixed resource loading succeeded without checkout/tools.
- Ruff and explicit production type checks passed.
- Runtime binder reads accepted calibration before resolving and comparing the
  whole immutable profile entry. Admin bootstrap reuses the protected Command.
- Emission invokes both Git source verification and the accepted Store reader;
  build hooks do not connect to a database or invent a profile.

All Git chains and accepted Store objects used in these tests are explicitly
synthetic/fake. No real database, native model, calibration, HTTP drama run or
external publication occurred. Deployment and real-run acceptance remain open.
