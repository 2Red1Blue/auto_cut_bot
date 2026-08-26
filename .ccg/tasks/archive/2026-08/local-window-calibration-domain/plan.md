# Plan

1. Keep raw response identity in the already added pure
   `ShadowLocalMeasurementEvidence`; Store owns eventual `BlobRef` and owner
   checks.
2. Define one closed, ordered local corpus/manifest type.  A member is
   identified by ordinal plus its case/request content identities, not source
   identity, because a source may contribute more than one window.
3. Define closed results and an independent validation report.  Rebuild every
   measurement from raw bytes; report exact per-case errors and only an
   explicit rational-duration aggregate, never an accepted policy bound.
4. Test negative origins, zero/empty observations, multiple time bases,
   reordering, substitution and raw/projection tampering.
5. Independently review the new pure-domain diff, then archive this bounded
   task.  A separate high-risk task will implement commands, durable recovery,
   Postgres migrations and a protected local validation artifact family.
