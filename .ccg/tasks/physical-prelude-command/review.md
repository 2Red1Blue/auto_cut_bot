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

In progress in disjoint Claude Code sessions. Not reviewed or accepted yet.
