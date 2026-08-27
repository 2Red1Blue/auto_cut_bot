# Explicit thinking, not hidden transport mutation

Real v4-compact episode still ended length: output32768, reasoning26780,
input34413. Compact body was one line but only10973 chars before truncation.
No third call until this versioned implementation passes checks.

API evidence: official Responses example at
https://www.volcengine.com/docs/82379/1795150 uses `thinking={"type":"disabled"}`;
installed Ark SDK has the same Responses field and enabled/disabled/auto enum.

Contract:
- Retain all current v2/v3/v4 adapter behavior and default v4 request bytes.
- Register `doubao-ark-files-responses-stream-v5` as explicit-thinking adapter;
  same upload MIME/direct-schema behavior as v4, same file-cache partition.
- DoubaoVlmRequestPolicy adds thinking_type=None by default. v5 REQUIRES an
  explicit enabled/disabled/auto string; old adapters require None. Only v5
  includes thinking_type in canonical request_parameters and API thinking.type.
- Semantic-only authority selects v5 + existing compact prompt + disabled,
  same32768 cap/parser/schema. This is an explicit new semantic policy, not a
  claim that all models must disable reasoning or that quality is already proven.
- Existing profile envelope v10 retains a versioned nested adapter union;
  new SQL migration adds only the v5 parameter variant for semantic v10.
  No old row/default rewrite; v9/full-pipeline validator stays unchanged.
- Original requests/profile hashes remain exact. Runtime reconstruction and
  reuse identity preserve/compare thinking_type. No secret/default injection.

Ownership (workers are not alone; no cross-owner edits):
1. Provider worker: vlm/doubao_ark_provider.py, vlm/request_factory.py,
   packages/autocut-kernel/src/autocut_kernel/vlm/reuse_identity.py,
   new tests/pipeline/test_vlm_explicit_thinking.py. v5 constant name
   DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION. Preserve old defaults.
2. SQL worker: new migrations/0029_vlm_explicit_thinking.sql and new
   tests/pipeline/test_vlm_thinking_profile_postgres.py. Test only disposable
   autocut_resume_check_20260828; preserve real autocut. Reject missing/NULL/
   wrong-typed/unknown v5 mode, old adapters with new field, extra keys; old
   v3/v4 histories unchanged. Main supplies new profile via typed factory when ready.
3. Main: runtime/models.py, runtime/semantic_authority.py, semantic-run JSON/digest,
   affected existing expectations, docs, integration tests/review and real run.
4. Independent reviewer audits version/replay/closed union/SQL before real enablement.

No Claude, SSH, legacy imports, new schema defaults, partial-JSON repair or
automatic increase of output allowance. No provider call from workers/tests.
