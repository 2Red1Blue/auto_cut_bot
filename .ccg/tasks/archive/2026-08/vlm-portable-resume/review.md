# Independent review

No blocking finding. Five PostgreSQL regressions passed on disposable DB;
Ruff/diff checks clean. Real Store and unmodified persisted SourcePrep reader
are used. Only media construction uses synthetic fixture input. Success path
prohibits source-root stat/open/probe/subprocess while checking exact request
bytes/hash and stored proxy bytes.

Missing claim/bytes are injected by a real psycopg Cursor hiding a matching
SELECT result. No DB rows/constraints/triggers are modified to fake corruption;
errors and zero provider calls are asserted. Frozen producer Job, Slot, Receipt,
Set and Artifact rows remain exact. Changed prompt/thinking does not relabel the
original idempotent run.

This proves simulated host-path independence with the same database. It does
not prove actual Windows execution, database/Blob transfer, cross-Job reuse or
the HTTP current source-catalog authorization path.
