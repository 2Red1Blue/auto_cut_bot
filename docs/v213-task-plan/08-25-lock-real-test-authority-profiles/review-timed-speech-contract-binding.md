# Timed-speech contract source binding — 2026-08-26

## Scope and decision

ALLOW for the five-file source-binding slice, not for task completion or runtime
activation. The independent reviewer modified no files. The integration owner
ran the combined source/profile/anchor regression suite.

The Kernel now derives the timed-speech wire-contract identity from the exact
reachable definition closure in the locked local-run schema. The locked builder
rejects a substituted digest, a whole-profile digest, a RegistrySet digest, a
changed reachable definition with a stale digest, or a changed root pointer.

## Verification

- Independent pure projection tests: 96 passed.
- Combined profile/source/local-run/anchor/runtime-boundary tests: 115 passed,
  zero skipped (191.34 seconds).
- Pure projection plus import-firewall checks: 107 passed, zero skipped.
  This includes the same 96 projection tests, so counts must not be added as
  distinct tests.
- Ruff on all five changed Python files: passed.
- BasedPyright on both production modules: zero errors/warnings.
- Independent review: no actionable defects; source loading adds no bootstrap,
  HTTP, native inference or Store-write permission.

## Remaining work

No real ready Registry pack, model calibration, installed authority resource or
real HTTP Pipeline run was produced by these tests. Git repositories and accepted
anchors in source tests are explicitly synthetic. Real source semantics and
calibration remain required; the enclosing Trellis task stays in progress.

