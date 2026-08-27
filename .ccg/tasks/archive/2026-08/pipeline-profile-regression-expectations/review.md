# Validation — 2026-08-28

Two test files only; no runtime/SQL or guard relaxation. Independent review
confirmed the same exception types and historical version/stage coverage,
precise stage-specific current messages, pre-database rejection sentinel,
and added immutable mapping/hash checks. Focused76 passed; Ruff/diff clean.

Main broader run:
`env -u AUTOCUT_TEST_POSTGRES_DSN .venv/bin/python -m pytest tests/pipeline --ignore=tests/pipeline/test_artifact_cache.py -q`
Result1392 passed,154 skipped. Separate real disposable PostgreSQL run-store
suite116 passed earlier. Skips are not represented as successes.

Unfiltered collection cannot import old Agent artifact_cache due to absent
autocut_core. Did not install legacy or edit that unrelated module. Therefore
this is not a claim that every repository test passes.
