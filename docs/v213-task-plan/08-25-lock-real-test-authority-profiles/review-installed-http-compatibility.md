# Installed HTTP compatibility review — 2026-08-26

Result: ALLOW for the code slice. The overall task remains in progress.

## Scope and ownership

- root: standard HTTP composition, VLM/media recovery compatibility, integration tests.
- calibration_contract: Kernel parser implementation identity; media policy binding.
- calibration_migration: static prompt/sampling identity extraction.
- review_calibration_migration: independent read-only review and delta verification.

No Claude Code, real model, real database or complete drama Pipeline was used.

## Findings and resolution

1. Installed media adapter lacked direct integration coverage (Warning).
   Added execute/reconcile mismatch-before-I/O tests, missing-anchor failure
   before Kernel command construction, and exact snapshot delegation after full
   installed resolution. Independent delta review: 4 passed, Warning closed.
2. Suspected parser-change replay bypass was investigated and withdrawn.
   The real current→shadow narrative inheritance, accepted aggregate identity,
   and persisted media aggregate comparison already bind the executed parser
   and template. Existing media Kernel claim also binds the Registry snapshot.
   No v6 field or models.py change was made for this unproven concern.
3. Existing API tests omitted the already-required typed v5 execution profile.
   Updated only their fake service fixture and qualified the shared fixture
   imports. Production validation was not relaxed. Tests now assert persisted
   v5 identity and no Store writes on unauthenticated requests.

## Verified boundaries

- Standard composition uses only the fixed installed loader, with no caller
  snapshot parameter; missing resources cannot trigger a fallback.
- Real startup resolver reads accepted calibration and the full immutable
  bootstrap entry before worker recovery. HTTP never bootstraps.
- VLM/media execute and reconcile validate actual persisted policy, without
  modifying it; mismatch blocks provider/command work.
- Source sampling validates both dynamic policy digest and actual PTS anchors.
- Static prompt extraction preserves request prompt bytes; per-window hashes
  remain distinct from stable deployment policy hashes.
- Media binding compares service/tool/model/producer identities, aggregate and
  child calibration hashes, detector identities, timing policy and exact
  rational microsecond/tick bounds.
- Parser identity is a fixed five-source installed bundle, not a formal proof
  or entire-wheel authentication.

## Evidence

Final root combined related suite: 550 passed, 6 skipped. The skipped PostgreSQL cases
were not run; they do not count as database acceptance.
Additional media recovery tests: 4 passed.
Architecture/package boundary tests, including root server wheel install:
17 passed.
Independent reviewer: 151 targeted checks plus 42 existing VLM regressions passed;
then 4 delta media-recovery tests passed. These overlap root tests, not extra
completion counts. API/run-service standalone regression: 52 passed; final
independent test-delta review: 73 passed, ALLOW. Production type checks (zero
errors/warnings), Ruff and diff check passed. No real services were started.

## Remaining scope

No production profile resource was manufactured or installed. Real calibration,
accepted source publication and full execution remain for the remote desktop.
HTTP currently registers source_prep, vlm and media_preflight only. Stage 1–3,
exact editing and Render/QC production integration are not certified by this
review; their remaining tasks stay open. External publication remains excluded.
