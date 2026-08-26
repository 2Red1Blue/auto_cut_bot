# Local-window calibration domain requirements

## Purpose

Persisted local-window speech measurements must be replayable and independently
verifiable without being mistaken for a complete-source calibration or an
accepted runtime authority record.

## Acceptance criteria

- The domain defines an ordered local corpus and measurement manifest/results
  grammar whose identity includes the pre-acceptance shadow-local service
  profile, each exact case/request, source provenance and independent anchors.
- A result references the exact manifest, preserves each original raw response
  identity and the independently replayed projection, but has no `pass`,
  accepted bound, registry activation or Receipt field.
- An independent validation report is produced only by recomputing every raw
  response from the exact case/request. It reports measured errors in their
  native source time bases; it neither invents a positive bound for zero error
  nor compares bare ticks across sources.
- Foreign profile/case/request/anchor/projection/raw identity, reordered,
  omitted or duplicated members, and non-canonical payloads are rejected.
- The new domain imports no Store, BlobRef, PostgreSQL, Job, Receipt, Registry
  or legacy module. Durable ownership and CAS remain a later Pipeline/Store
  slice.

## Explicit non-goals

- No PostgreSQL migration, command recovery, raw-blob reader, authority
  registry binding, normal-profile installation, or local profile activation.
- No reuse or semantic weakening of the complete-source
  `MeasureShadowCalibrationCommand@2.1.3`, `CalibrationRecord`, migration
  `0016`, or migration `0017` contracts.
