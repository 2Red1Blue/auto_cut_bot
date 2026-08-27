# Review — 2026-08-28

Independent reviewer found no blocker. Independently compared v3 template and
rendered bytes/schema against HEAD: unchanged. v4 uses actual proxy time base;
no extra generation parameter, schema omission, raised budget or implicit
rewriting of frozen profiles. Installed semantic-only authority selects v4;
full pipeline default stays v3. Resource digest verified.

Validation: worker144 tests; independent160 tests; main combined405 passed,
18 PostgreSQL-only cases skipped in that invocation. Production BasedPyright,
Ruff and diff checks passed. Main real database read-only SQL validation accepts
both v3/v4 profiles and reconstructs the original failed v3 profile hash exactly.

Existing test_installed_vlm_policy used an obsolete tuple index from the earlier
stage refactor; fixed to assert source, policy and request separately,21 passed.

This verifies the implementation, not model quality or guaranteed completion.
Real v4 single-episode run is the next operation; no true result is claimed yet.
Selective recompute HTTP/ledger remains separate unfinished work.
