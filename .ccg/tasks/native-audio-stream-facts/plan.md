# Native audio stream facts

Implement the independently reviewed design in
`docs/v213-task-plan/08-21-05-media-preflight-calibration/audio-stream-facts-followup.md`.
No new database, top-level source schema version, migration, speech invocation,
Runtime activation or policy default is part of this slice.

One native worker owns the new immutable audio fact/closed decoder, optional
source-manifest leaf, real source probe normalization, Command reconstruction,
and focused synthetic source tests. Root owns this task record, integration,
review and scoped commits. Other workers own the physical prelude files and
must not be disturbed. No Claude, recursive agents, real codecs/models/DB,
protected config access, legacy access, formatting sweeps or commit by workers.

Root also owns the separate `physical_edit/presentation_map.py` exact-root type
extension and `tests/media/test_physical_root_mapping.py`. This permits only
the already replayable six-set physical root in the mathematical mapper; it
does not widen old speech guards or edit Admission. All complete-span and
decoded boundary checks remain unchanged.

## Acceptance

- Old absent-leaf manifests roundtrip without new key/null/default and retain hash.
- New source preparation measures positive native rate/channels from selected
  audio metadata and binds them to the exact source, index, clock, range and
  probe execution. Time base is not a sample-rate inference.
- The new normalized selected-audio preimage includes rate/channels and has its
  own reproducible hash; do not reinterpret the old presentation output hash.
- Closed typed decode rejects boolean/float/wrong-shape/unknown fields, foreign
  source/index/clock/range and metadata/hash substitutions; decoder verifies
  consistency, not falsely claimed independent native execution.
- Readback reconstruction preserves the optional field.
- Existing source preparation/manifest replay tests plus new focused synthetic
  tests, scoped Ruff/BasedPyright and independent review precede acceptance.
- Real desktop source probe/extraction and local-window dispatch remain later
  checks; this slice alone does not close the speech Runtime.
