# Shadow-bootstrap HTTP entry plan

`/v1/pipeline/run` must not be reused: a shadow request there would still enter
the ordinary frozen stage chain. The bootstrap entry is a separate application
operation, not a Pipeline stage or worker command.

```text
POST /v1/pipeline/shadow-bootstrap-observation
  {source_run_id, episode_index}
  -> validate authenticated, closed request
  -> read succeeded shadow SourcePrep Receipt/ArtifactSet
  -> derive exact episode BlobRef and audio clock from persisted manifest
  -> CollectShadowBootstrapObservationCommand
  -> {command outcome and, if terminal, Receipt/ArtifactSet IDs}
```

The client cannot provide a blob ID, Receipt ID, artifact reference, timing
clock, profile, or endpoint. The service fixes both source/destination Job
profiles to `shadow`, uses the SourcePrep idempotency key to find the committed
source outcome, and repeats the Kernel provenance checks before dispatch.

The composition owns the loopback FunASR endpoint/token/limits. The API route
uses the existing Pipeline authentication middleware and requires an
`Idempotency-Key`; it does not register any stage port or alter a normal
PipelineRun status.

Required tests: closed API shape/auth/replay, no dispatch for a missing,
non-shadow, non-succeeded, or out-of-range source; source-manifest provenance;
and actual Command outcome projection. Unknown dispatch remains `running` and
has no terminal Receipt.
