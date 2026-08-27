# Scope

Broader non-DB Pipeline regression found9 failures in historical-profile rejection
tests: precise runtime rejection changed after v10, assertions still require v9
wording. Update only tests/pipeline/test_evidence_read_limits_profile.py and
test_stage2_execution_profile.py, preserving rejection and exact historical data.
Do not alter runtime, SQL, fixtures to make forbidden legacy profiles executable.

Full Pipeline collection separately hits unavailable autocut_core in the old
Agent artifact-cache test; do not install/import legacy to work around it. Report
that separate test was excluded, and DB/native-audio tests remain explicitly skipped.
Workers are not alone; no changes outside owned files, no provider/SSH/Claude.
