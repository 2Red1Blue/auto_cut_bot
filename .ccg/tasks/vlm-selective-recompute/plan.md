# Selected-only VLM recovery implementation plan

## Objective

After a terminal semantic-only run has produced valid VLM results for some
episodes but failed one or more others, execute only explicitly selected source
episodes under a new immutable run. The new result must be inspectable and
durable, but cannot masquerade as a whole-series VLM batch or unlock Stage 1.

## Non-goals of this slice

- Do not change V18 output acceptance, silently repair provider JSON, or lower
  parser rules.
- Do not reuse the 49 prior semantic outputs as a new complete batch yet.
- Do not re-run SourcePrep, resolve a filesystem path, or refetch metadata.
- Do not add Agent-only behaviour or external publication.

## Implementation order

1. Add closed `VlmSelectedOnlyRecomputeRequest` and a stable selection
   identity: `base_run_id`, `expected_version`, `stage=vlm`,
   `completion_scope=selected_only`, and strictly ascending `episode_numbers`.
2. Persist a target-run recompute control record and immutable plan payload
   in the same transaction as target run creation. Its selected zero-based
   episode indices are the worker's sole authority; an empty or duplicate
   selection is rejected before any source binding or provider call.
3. Reuse only the base run's verified `source_prep` evidence via the existing
   binding command. Bind before queuing the target run, keeping the target
   job's ordinary blob-read authority and never reading under the parent job.
4. Make `context_prepare` run for all source episodes only when it is a
   required dependency for a selected VLM request; make VLM dispatch consume
   the persisted selected indices, not a caller-provided runtime list.
5. Persist one `VlmSelectionResult/v1` when every selected member succeeds.
   Its payload includes target run, base run, source-set identity, selected
   episode numbers and exact child Receipt/Artifact references. No
   `vlm_semantic_pack_set` is created and no Stage 1 command is enqueued.
6. On one selected-member failure, keep all successful child receipts and
   record the target VLM command failed. The result is not a success and it
   never changes the base run.
7. Add unit, store and HTTP tests: validation, idempotency, source binding,
   worker selection, no full-batch finalizer, and no downstream stage.
8. Run targeted tests, lint/type checks, review the diff, commit and push via
   Git relay. Apply the migration to PC, invoke only the failed source episode
   from V18, and inspect its exact model records.

## Acceptance criteria

- A selected-only request with `[n]` performs no VLM calls for episodes other
  than `n`.
- Identical request/key returns the same target run; a changed request/key
  conflict cannot change that run's selection.
- Source blobs are read using the target job's binding, not parent-job access.
- Partial results remain explicitly partial and cannot be read as
  `vlm_semantic_pack_set` or sent to the semantic story chain.
- No provider request occurs for malformed, stale, unbound or unsupported
  recompute requests.
