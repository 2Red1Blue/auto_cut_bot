# Review — 2026-08-28

Delivered only compatibility-identity foundation, exact-source projection and
nonterminal resume fix; **not** the recompute HTTP/selection/ledger feature.

- Independent reviewer: identity/projection four files no blocker;55 tests.
- Main: VLM + projection + request-factory suites160 passed.
- Independent resume review: no bypass of predecessor order, leases, terminal
  state or calibration-only rule. Existing later pending command can wake queue
  but cannot execute ahead of unfinished predecessors.
- Worker PostgreSQL regression: initial resume tests4 failures reproduced,
  fixed14 resume tests; main expanded to full suite and exposed12 stale v9
  assertions after the prior v10 migration. Worker aligned exact current error
  expectations and added no-row/unchanged-history assertions; full116 passed.
- Worker additional no-database runtime regressions72 passed.
- Ruff, production BasedPyright and whitespace checks passed.
- Tests use only disposable `autocut_resume_check_20260828`, never real `autocut`.

Deferred: cross-Job access, selective recompute planner/HTTP/finalizer and
lineage budgets/hold. Host-independent identity tests do not prove an actual
PC-to-Mac handoff. No source/video/private config was added to Git.
