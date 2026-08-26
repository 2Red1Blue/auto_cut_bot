# Review checkpoints

## Shared certificate/resolver seam

Independent reviewer: ALLOW, no Critical/Warning. The certificate compiler and
replay accept exactly the old eight-set root or new six-set physical root;
neither type is interchangeable by hash. Existing clock/map/calibration checks
are unchanged. Speech admission still requires the old root. The Source/VLM
resolver takes a two-read-method Protocol, without a fake speech-registry
dependency or any runtime change.

Root: regression 160 passed; scoped Ruff and BasedPyright clean. Independent
reviewer: 139 passed, Ruff/types/diff checks clean. New physical certificate
tests cover unequal tails, a declared gap, changed root/probe/calibration/
manifest and structural lookalikes. Tests are pure and do not establish any
database, model or native codec execution.

## Command and producer

Completed by existing native subagents after the user stopped Claude Code due
to quota. Kernel owner and producer owner independently reviewed each other's
frozen files; both returned ALLOW. Root also reviewed the production changes
and supplied an adapter → Command → reader integration test.

Draft issues corrected before acceptance: failed-result handling after an
ambiguous success commit; truncated logical IDs; hidden read limits; invalid
reader type check; incomplete six-role policy/provenance binding; bool/int
equality during replay. Producer review additionally closed non-integer luma
thresholds, structural lookalikes, width-one subtitle division by zero, and
bounded metadata before constructing the shared producer DTO.

Final combined pure suite: **909 passed** (physical Command/reader/producer,
adapter integration, old whole-source golden, root/certificate/map/old timed
Command and import firewall). Scoped Ruff and production BasedPyright pass.
Exactly three ordered immutable members and one root Blob; no speech call or
speech-registry resolution on the physical path. Unknown claims never silently
redispatch and ambiguous success is never rewritten as failed.

This closes the physical-prelude slice only. It does not certify real
PostgreSQL transaction races, native detectors/models, local speech Commands,
shadow-local calibration or composed Runtime activation.

## Audio facts and compatibility baseline

Independent reviewer: ALLOW. The audio-stream-facts followup records the missing
native layout and versioned optional-leaf design without claiming persistence.
The old whole-source golden hash was captured at f2c59f35 with fake tools and
speech only; its initial test passed. It must also pass after the port refactor.
Scoped Ruff and diff checks passed; protected private config was not included.
