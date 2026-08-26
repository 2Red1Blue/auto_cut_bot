# Local profile compilation and resource transport — 2026-08-26

## Decision and architectural scope

ALLOW for this implementation slice after the one import-format warning was
fixed. Independent design and read-only implementation reviews found no blocking
logic defects. This is not acceptance of the full Trellis task or a real run.

The local shadow/run path no longer invokes the old generic eight-pack,
19-command Registry compiler. It verifies the same immutable A/B/C source chain,
then compiles its three actual profile roles into a domain-separated local
identity. Generic RegistrySet readiness is unchanged and still incomplete.
The old total-contract document is not restored. Task 01 and this task record
the different scopes explicitly.

## Delivered behavior

- Local source builders use exact locked narrative/profile/schema bytes and
  preserve grammar, schema-component and four-part predecessor checks.
- Fixed installed-resource decoding recomputes both local identities, retains
  all six original sources, rejects extra fields/substitutions and limits reads.
- Build emission calls both real source builders and the accepted-calibration
  binder internally; it accepts neither a caller snapshot nor caller context.
- The complete calibration comparison is shared by Kernel and build tools,
  without reverse imports, database writes or accepted-anchor reinterpretation.
- Snapshot documentation distinguishes a local profile identity from full
  command-matrix readiness and publication permission.

## Verification evidence

- Combined codec/source/profile/anchor/runtime-absence/firewall suite:
  272 passed, zero skipped (54.49 seconds).
- Additional local compiler/resource emitter/component-contract suite:
  109 passed, zero skipped (16.18 seconds).
- Existing generic locked Registry suite after shared verifier extraction:
  29 passed, zero skipped (38.73 seconds).
- Final frozen source/compiler/emitter/anchor suite, including the late
  generic-profile substitution negative: 116 passed, zero skipped (53.56 seconds).
  This overlaps earlier suites; do not add it as distinct tests.
- Ruff on all 15 changed Python files: passed.
- BasedPyright on all nine changed production modules: zero errors/warnings.
- Independent codec review: 118 tests passed; no findings.
- Independent builder/emitter review: logic ALLOW; one missing import separator
  was corrected by its owner and the final scoped Ruff run passed.
- No native model invocation, real calibration, PostgreSQL operation or external
  publication was performed by this slice. Positive Git histories and accepted
  Store readers are explicitly synthetic/fake test evidence.

## Still required

Explicit resource preparation/packaging for both wheels, installed-only admin
bootstrap, accepted-calibration plus full bootstrap-entry verification before
worker recovery, narrative/provider compatibility, actual source inventory/lock
publication without the withdrawn document, real measured calibration and a
causal HTTP-to-local-output Pipeline run.

The controlled installed wheel is the deployment trust root. Its sibling digest
detects byte drift but does not authenticate an arbitrarily replaced wheel.
No valid default or fixture authority resource is installed in the real tree.
