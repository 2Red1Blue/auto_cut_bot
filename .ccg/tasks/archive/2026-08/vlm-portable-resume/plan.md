# Portable resume regression, not a new recompute API

User goal requires Mac/PC compatibility. Existing SourcePrep readback should
reconstruct the exact persisted inputs without touching the original machine's
filesystem. Add focused regression for that existing behavior and fail-closed
missing Blob/claims. No provider call, legacy dependency, SSH or production DB
test. Immutable historical requests must not acquire current thinking/prompt.

Worker owns only tests/pipeline/test_vlm_portable_resume.py; main owns task/docs.
Use existing fixtures where they exercise real readback rather than mocking away
the property being tested. PostgreSQL tests use disposable database only.
Independent review before commit. A simulated missing Windows path test does
not prove actual Windows/Mac deployment or implement cross-Job reuse.
