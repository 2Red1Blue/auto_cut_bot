# Ownership and execution

1. Selection/planner implementation remains pending: user switched to immediate Mac local run.
   No claim that the planned HTTP recompute API is implemented.
2. Worker identity: `autocut_kernel/vlm/reuse_identity.py` and matching tests.
   Stage-local semantic policy and exact input identity from existing request/manifest facts; no Runtime imports.
3. Main: `auto_cut_bot/pipeline/vlm/reuse.py` source-bound projection + integration tests, documentation.
   Project existing original request/SourcePrep facts into shared identity. No planner or paid dispatch.
4. Independent reviewer checks new code and tests; primary fixes findings; lint/type/tests; scoped Git commit.
5. Worker resume owns runtime/postgres.py and tests/pipeline/test_run_store_postgres.py only;
   fix semantic-only nonterminal wake and prove CAS/no terminal reopening on disposable PostgreSQL.

Do not modify private configuration or legacy code. Workers are not alone; file ownership does not overlap.
No Claude Code calls. Do not spawn from workers. New database schema/recompute HTTP enablement is not part of this commit.
