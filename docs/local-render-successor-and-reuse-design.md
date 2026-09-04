# Local-render successor and semantic reuse

Status: implementation design; not a claim that cross-run rendering works.
Tasks: global `08-29-audit-vlm-io-incremental-rerun` and
`08-21-07-stage1-3-semantic-chain`. Preserve both tasks until HTTP E2E passes.

## 1. Evidence and target

The real PC Stage 1 succeeded; see
[run evidence](stage123-ark-wire-fix-2026-09-05.md). Stage 2's predecessor reader
then rejected the original Source grant, which authorizes `semantic_analysis`
only. The requested outcome is a local rendered movie, not external publication.

Two independent gaps must be fixed, not hidden behind a full VLM rerun:

1. `BindWholeSeriesSourcesCommand` requires the target operation policy to equal
   the original policy. It cannot issue new rendering authorization by itself.
2. `VlmReuseIdentityV1` puts `source_provenance_sha256` in its compatibility hash.
   `PersistedPreparedSources.provenance_mapping()` includes Job, Receipt, slot
   and ArtifactSet IDs. A real new target source owner therefore changes the
   supposed semantic fingerprint even when the media and model input are equal.
   The complete SourceManifest hash also incorporates the operation policy.

Do not change old success records, reinterpret old hashes, remove render-purpose
checks, or turn missing reuse into implicit provider dispatch.

## 2. Separate three decisions

| Decision | Essential fields | Must not stand in for it |
| --- | --- | --- |
| Semantic compatibility | exact source/window/PTS/proxy evidence, rendered prompt, context, schema, immutable model/provider scope, parser and generation policy | Job, Receipt or target rendering settings |
| Target operation authorization | trusted local catalog/policy, target Job, exact source content set, requested purposes | compatible hash or old semantic-only grant |
| Reuse provenance | exact origin request/response/Receipt/ArtifactSet/attempt and source owner; target binding | debug file or latest-success lookup |

All three are required for durable cross-run use. Compatibility is pure and
can be calculated before authorization; it does not grant Blob access or mint
an accepted result. Unknown original provider scope means reuse unavailable,
not permission to substitute current credentials.

## 3. Portable identity v2 (first implementation slice)

Retain `VlmReuseIdentityV1` and `VlmReusePlan/v1` mappings/hashes exactly.
Add `VlmReuseIdentityV2` as a projection over the existing fully validated v1
request facts; do not duplicate its payload/parser/context validation.

- The v2 compatibility mapping uses discriminator `VlmReuseIdentity/v2` and all
  v1 semantic fields, excluding only `source_provenance_sha256` and the complete
  `source_manifest_sha256`. Source ID/content, window and window-set hashes,
  timeline/frame evidence, episode index and exact context/prompt remain bound.
- Retain both excluded hashes in a separate provenance mapping. v2 plan census
  serialization includes origin and target provenance; the complete plan digest
  changes on owner/authorization changes even when compatibility does not.
- No trimming Context Pack, removing prompt fields or changing generation/parser
  policy to force a match. Such differences remain explicit non-reuse reasons.
- Extend the existing planner rather than duplicate it. All-v1 plans preserve
  v1 serialization. All-v2 plans use `VlmReusePlan/v2`; mixed versions within or
  between episodes fail validation. No implicit v1-to-v2 promotion.
- Target census count and exact target source owner hashes remain mandatory.
  `inspection` never becomes `complete_batch` for a partial selection.
- Origin closure must bind the selected identity version's hash. Pure classes
  cannot establish the truth of that closure; Store rereading remains mandatory.

Acceptance: distinct actual source owner identities with identical source and
model inputs match in v2 but not v1; changed media/prompt/context/provider/parser
does not match; full plan identity still binds both owners; v1 regression suite
passes unchanged. No model, DB write or runtime activation in this slice.

## 4. Remaining end-to-end implementation order

1. **Target source authorization**: resolve an explicit configured local source
   policy containing `render_source`, bound to the same complete content census.
   Use a versioned source binding command/Receipt; the old binding is not allowed
   to silently expand purposes. New target Source snapshots preserve original
   source/proxy/timeline bytes and reference their old producer.
2. **VLM reuse commit**: independently reread the full succeeded origin closure,
   rebuild v2 compatibility from actual facts, validate target authorization,
   and atomically commit a target-owned reuse binding and Blob references.
   Do not fabricate a new provider invocation or copy the old Admission as a new one.
3. **Target batch**: finalizer accepts explicitly typed generated/reused target
   children, rereads each binding and closes every ordered census member.
   No raw cross-Job reference, partial success or caller-built pass.
4. **Semantic continuation**: Stage 1–3 readers must accept that target batch.
   Old Stage 1 outputs retain their original source references. Any reuse across
   the changed source grant/owner needs explicit deterministic rebinding plus
   independent re-evaluation; never substitute the old input-binding hash.
   If exact deterministic rebinding cannot be established, report affected
   text stages before dispatch; this must not invalidate the reusable VLM.
5. **HTTP successor**: reserve the new run and immutable selected plan before
   binding; activate only after required bindings commit. Preserve old run
   plan/status. Expose reused/executed/held episode lists and reasons.
6. **PC acceptance**: same drama, zero VLM calls for compatible episodes, real
   Stage 2/3 → Media Preflight → exact compiler → Render → QC → local movie.
   Agent Runtime remains another caller of the same Kernel, not the Pipeline owner.

Steps 1–6 are still required. Implementing the pure identity is not completion
of reuse, source authorization, successor HTTP or local E2E.

## 5. Reference principles and deliberate limits

[Prefect caching](https://docs.prefect.io/v3/concepts/caching) exposes input,
task-source and run-ID cache policies separately. We adopt the distinction
between input compatibility and execution identity, not Prefect's default key
or an automatic assumption that a cached result is authorized.

[DVC cache internals](https://github.com/treeverse/dvc.org/blob/main/content/docs/user-guide/project-structure/internal-files.md)
separates content-addressed data from workspace names and run metadata. We retain
our existing SHA-256 Blob/Receipt model; no new framework or MD5 cache is needed.

The field-necessity audit above follows the architecture skill's audit method.
Its older media schema/ASR semantic suggestions are not adopted: VLM-first,
ASR/VAD only for timed evidence, and no external publication remain unchanged.
