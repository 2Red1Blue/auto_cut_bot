# Explicit thinking review

Result: no remaining blocking issue after independent review and correction.
No Claude or real provider invocation was used in these tests.

Independent reviewer checked provider/factory/profile/reuse/SQL and caught a
non-v10 SQL fallback accepting adapter v5 when thinking_type was absent. Fixed
the new outer validator to reject top-level OR nested v5 outside v10. The
underlying v9 function and historical rows remain unchanged. Added 15 cases
covering top/nested/both and absent/NULL/enabled/disabled/auto.

Verified: legacy request/payload/profile/reuse fixed hashes; three explicit
modes with distinct identities and exact roundtrip; malformed inputs rejected
before client creation; v4/v5 identical upload MIME and actual cache keys.

- Provider/factory/reuse focused suite: 191 passed.
- Pipeline suite without PostgreSQL/native audio: 1437 passed, 181 skipped at
  that checkpoint (subsequent added cases covered by focused suites).
- SQL migration and profile suite: 46 passed on disposable PostgreSQL only.
- Ruff/BasedPyright on changed production code: clean.
- Unfiltered pipeline collection still has the unrelated legacy-only
  test_artifact_cache.py -> autocut_core import failure; it was explicitly
  excluded, not silently counted as passing.

Deployment condition: stop all pre-v5 workers before migration/new authority.
Outbox has no adapter-specific lease partition; do not claim mixed-version
worker rollout safety. Real calls remain separate acceptance evidence, not
covered by the successful fake-provider tests.
