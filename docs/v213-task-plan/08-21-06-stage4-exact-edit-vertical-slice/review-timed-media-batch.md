# Review: exact timed-media batch and v9 read budgets

Scope: Task06 committed-reader-wave, not full production Stage4 or whole-run
acceptance. Decision: allow this implementation slice after owner fixes and
independent read-only review.

## Closed findings

- Batch completeness is derived from the committed SourceManifest, not the
  supplied number of successful children. Every child shares the Kernel Job,
  exact Source/VLM selectors and full five-member identity.
- A first pass validates metadata and cumulative Blob lengths before any full
  evidence read or finalizer claim. It retains only compact references/hashes.
  Each full replay releases its root/plans/candidates before the next episode.
- Replayed pending/running outcomes never produce a successful aggregate. The
  public reader neither claims nor writes and verifies the final Receipt/Set.
- v9 freezes an independent closed per-blob/whole-batch byte budget. Composition
  rejects incompatible copy chunk/budget settings before provider registration.
  SQL 0022 keeps terminal old profiles read-only and refuses active old runs;
  it does not fabricate defaults or recalibrate models.
- The actual LocalMediaPreflightPort previously omitted the installed ASR/VAD
  adapter identity, although synthetic reader fixtures provided it. The producer
  now receives that identity from the installed Runtime resource, preserves it
  in provenance/bindings, and retains the accepted timing bound after checking
  each response. No reader relaxation or consumer-side identity repair.
- Remote test oracles were stale (v8/5-stage assertions). They now cover v9 and
  six stages, preserve explicit old migration baselines, and include 33 new
  0022 database cases. The media replay test uses actual measurement, independent
  validation and bootstrap over clearly synthetic raw/gold observations; it is
  not claimed to exercise real SenseVoice/FSMN.

## Independent review and verification

`calibration_contract` independently reviewed Runtime/batch/compact lifetime
and actual two-episode fixtures; eight batch tests passed. The lifetime test
observes original reader results and would fail for retained full metadata or
across-episode decoded DTOs; closing Blob leases alone is insufficient.
`review_calibration_migration` independently reviewed v9/SQL and actual producer
identity/bound behavior: 192 pure tests passed. Root reviewed the reviewer's
remote calibration helper and PostgreSQL test changes; no local DB was run.

Root verification: 2356 related media/semantic/Runtime/architecture pure tests
passed, five PostgreSQL tests deliberately deselected. Eight additional batch
tests passed (not part of that 2356). Scoped production type checks and Ruff
passed. 112 cases in the remote run-store/media-stage test modules collected;
collection is not execution and proves no SQL behavior. The overlapping focused
Prepare+batch suite is a separate check, not extra unique test coverage.

## Remaining work

Continue editorial-media-join-wave.md, then native candidate-local timed guard,
committed v2 piecewise clock, independent physical admission, A/V Recipe,
Render/local QC and Agent Runtime conformance. Real PostgreSQL migration,
restart/concurrency, providers and media acceptance remain on the desktop.
External publication and whole-run success remain disabled. User-owned local
configuration is excluded from this change.
