# Independent review: exact five-member timed-media reader

Date: 2026-08-26. Result: ALLOW for this slice, not Task06 completion.

Scope: pipeline/committed_timed_media.py, the shared strict JSON extraction in
media/root_evidence_codec.py, and tests/authority/test_committed_timed_media.py.
Producer prerequisites were separately reviewed and committed as fd515321.
The concurrent v9/batch work is outside this approval.

No Critical/Warning findings. The first per-blob budget test stopped at inline
JSON size and did not exercise the BlobRef guard. The test owner corrected it
to rehash a valid member with an oversized declared BlobRef and assert zero
materialization attempts. Corrupt and foreign leases are closed on rejection.

The independent reviewer verified exact record/member identity, Source/VLM
replay, installed accepted ASR/VAD bindings and v2 presentation-map replay.
Empty-candidate bound/version/calibration-policy mutations update provenance
and dependent admission hashes consistently, but still fail the installed
acceptance check. Real Chinese UTF-8 evidence passes with distinct raw Blob
and canonical media hashes. ValueError is wrapped with its cause; Store/I/O
failures are not swallowed or retried by this reader.

Root verification: 1920 scoped media/domain/preflight/architecture tests plus
22 reader tests passed. Independent verification: 22 reader tests and 1689
root-codec tests passed; scoped Ruff and basedpyright clean.

These are producer-shaped in-memory tests. No real PostgreSQL, model/provider,
whole-batch reader, physical Admission or production rendering was exercised.
The next implementation remains whole-batch exact validation with independent
cumulative evidence budgets, then the actual Stage3/Catalog physical join.
