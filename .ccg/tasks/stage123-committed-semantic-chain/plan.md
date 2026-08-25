# Implementation plan

1. Freeze the replacement boundary and refuse incremental activation of the
   existing `production_stage1/2/3.py` prototype.
2. Replace VLM v2 in one incompatible wave: prompt, schema, parser, decoder,
   persisted artifact type, exact reader and tests become semantic-pack v3.
   Keep Doubao Ark streaming, durable attempts, retry/reconcile and window
   timeline mapping unchanged.
3. Add a content-bound Source operation grant at source preparation and return
   only Store-verified typed authorization projections to downstream stages.
4. Freeze the small replacement API:
   `semantic_chain/{authority,rules,stage1,stage2,stage3}.py` plus three thin
   commands.  Rules start indeterminate and become pass only after an evaluator
   performs the named check.
5. Before Stage 1 business code, correct the shared owners once: rename and
   strengthen the VLM aggregate as an exact `vlm_semantic_pack_set`, return the
   typed SourceOperationGrant from the committed reader, move cross-window
   continuity diagnostics out of Store, generalize generation persistence by a
   closed execution kind, replace pre-commit ArtifactRef identities, and remove
   default-pass Rule helpers.  Do not add consumer aliases or command-name
   whitelists.
6. Implement `BuildNarrativeGraph` against the exact committed upstream batch;
   strict-global coverage is the only first-slice policy.
7. Implement `CompileStoryPortfolio`; editing modes and semantic measurements
   come only from the committed v3 candidate hypothesis, while all physical
   feasibility remains deferred to Stage 4.
8. Implement one unpartitioned, all-or-nothing
   `BuildEditorialBlueprint` batch with a single evaluator-owned Admission.
9. Add exact Stage 1/2/3 output readers and PostgreSQL restart/replay tests.
10. Switch Pipeline and Agent runtimes to the three shared commands. The unused
   fixture semantic command, v2 adapter, production mega-facade and dead
   Stage 1-3 prototype were removed at the v3 contract cutover; runtimes remain
   fail-closed until the replacement commands exist.
11. Run one real committed Doubao episode to admitted Blueprint, prove the
    semantic compiler cannot reach Transcript/VAD/physical endpoints, and run
    two independent adversarial reviews before Stage 4 begins.

## Keep / replace boundary

- Keep: exact Receipt/ArtifactSet/Blob readers, atomic command persistence and
  replay, VLM window/timeline mapping, Doubao Ark streaming state machine,
  canonical JSON values and Stage 4 integer exact-span primitives.
- Replace: coarse observation v2, implicit Source authorization,
  `computed_rule_results`, hash-only pending sets and all public evaluator DTO
  construction in the current Stage 1-3 prototype.
- Deleted at contract cutover: fixture semantic chain command/adapters, old v2
  typed readers and production code paths that imported the prototype.

## No-patch gate

Every new finding is classified before editing:

1. contract gap;
2. domain-model gap;
3. implementation defect;
4. test/fixture gap.

If the same root cause affects at least two modules, local patches are denied.
The owner contract or model is changed once, all consumers are migrated, and a
cross-layer regression test is added. This migration permits no compatibility
re-export, translated old request, dual write, minted authority or invocation
of an old builder because v2 never produced executable production data.
