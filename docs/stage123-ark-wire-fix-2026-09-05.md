# Stage 1–3 Ark Responses wire correction

## Observed failure

PC real Stage1 Kernel execution consumed the committed single-episode V22
Source/Context/VLM inputs in `pipeline_run_cc2196abcdef4645a7fa587c843d0d1a`.
Request compilation succeeded (26,389 bytes). Ark rejected dispatch with HTTP400;
Receipt `1fa1521a-861f-4365-a48a-93def2138588`, no result ArtifactSet.
The unchanged original HTTP run still contains only its three completed stages.

The text draft path sends nested `text.format.json_schema`. The VLM adapter
already documents and implements the endpoint-accepted direct `text.format`
shape in versions4/5. Existing text tests only assert the SDK type annotation,
which is not evidence that the endpoint accepts the nested shape. The saved
error lacks provider parameter detail, so HTTP400 alone is not proof of its
exact cause; the wire mismatch is independently established in existing code.

References: local `ark_api/reponses/2. 查询 Response 详情.md` text.format fields,
[official Responses documentation](https://www.volcengine.com/docs/82379/1783709),
and `auto_cut_bot/pipeline/vlm/doubao_ark_provider.py:_response_text_format`.

## Implementation plan

1. Register text adapter `doubao-ark-text-responses-stream-v2` for the direct
   `{type, name, strict, schema}` format. Retain v1 and its exact historical
   request serialization for replay; do not silently rewrite v1 at transport.
2. Put versioned format construction/decoding in the existing Kernel text-draft
   boundary (`semantic_chain/draft_provider.py`), used by all three request
   compilers. Decode exactly the two supported shapes, reject mixed/unknown
   keys, preserve strict mode and actual payload hash validation.
3. Text provider accepts an explicit registered strategy (legacy v1 default
   remains for existing callers). Before SDK construction, verify body format
   matches that strategy. Derive schema name from the validated descriptor for
   debug naming. Forward the exact body with no fallback or local retry.
4. Composition supplies each frozen stage policy's adapter strategy. Only the
   installed semantic-run resource's three text strategy fields move to v2;
   recompute its paired digest. Do not change VLM policy/prompt/schema, local-run
   resource, source data, SQL schema, or semantic ownership contracts.
5. Test all three compiler shapes, v1 byte/hash replay, v2 request identity
   separation, malformed/mixed forms, provider strategy mismatch before I/O,
   and exact SDK outgoing body. SDK annotations must not be a runtime oracle.
6. Commit and Git-sync PC, run focused semantic/provider/composition tests and
   lint/type checks there. Then execute real Stage1 with a new policy-bound
   command key, preserving the failed v1 Receipt; inspect real model output and
   independent compilation. Do not rerun VLM or call this a full HTTP E2E.

## Boundaries still open

Plan cross-review: both Codex and Claude approved under supervisor
`85c0576d-406d-44d0-9c1a-3e1f830ca890`. Review follow-ups included bounded,
credential-redacted SDK error diagnostics (`code/param/type/message` only),
explicitly retaining the separate local-run authority's frozen v1 strategies,
and verifying the semantic-run digest loader and unchanged VLM policy. The old
semantic-run file digest is `sha256:231fbe236087b1b6426a8bc9446576b108479e56eafba52410bb914cab18befd`.
Only semantic-run switches to v2 in this slice; this is not a claim that all
full-local-run paths have moved to v2 or have passed live integration.

Cross-run semantic successor integration is a separate architectural change:
many semantic contracts currently require Source/VLM and Stage1–3 output scope
to match. Do not relax those checks incidentally to fix Ark serialization.
HTTP Stage4/Render/QC and local video output remain unfinished.

## Implementation and PC evidence

- Branch: `feat/v213-contract-codegen`; implementation `088956f8`, diagnostic
  type/import follow-ups `1474dd6d` / `5a1dcf1a`, installed-SDK serializer test
  `9ce3b642`. User config and untracked `.trellis/` were excluded.
- PC WSL clean validation clone fast-forwarded through Git bundle, not source
  copying. At `088956f8`: semantic-chain plus selected runtime/provider tests
  **2768 passed in 126.88s**, JUnit `/tmp/stage123-wire-088956f8.xml`.
  Independent focused run **219 passed in 4.52s**.
- Initial Ruff/type checks found import order and dictionary type narrowing;
  fixes are separate commits. Final checks must be recorded after PC updates.
- Code reviews `0588369e-10f4-44e4-ab3e-628a0e598ffc` and
  `44951414-240b-4014-b358-cbfc88b94808`: Codex approved; Claude exceeded
  120/180 seconds without a terminal report. This is **not** dual approval.

### Real Stage 1 v2 invocation (not an HTTP run completion)

Original business database `autocut`, original Source/Context/VLM inputs;
no schema reset, media upload or VLM rerun. The existing successful HTTP run's
three-stage frozen plan is unchanged. The direct Kernel integration command is:

- Key: `stage1-narrative:be88fdd068259220a7220ca81ec906b603fd0d7bdfd0bf60596565a7060af812`.
- Request hash: `sha256:5eee84546b07138a7a46e87839efd1a7a2e69a2b98bbcc2180616d5e97160ee4`.
- Slot: `3447e7c5-67b3-4cda-babf-25ae2a44750a`.
- Attempt: `ad78cb29-6ba5-4740-8fb8-18d6f97cbdea`.
- Provider response ID: `resp_0217885400625945f505d6745d4b07a5efbd0dd2f16cfcb71f8a3`.
- Outgoing body: 26,373 bytes, explicit text adapter v2.
- Debug root: `/home/laiu/autocut-debug/real-semantic-v2-20260905/`.

The request completed and passed the real Stage 1 compiler, coverage checks,
independent evaluator and committed-reader replay:

- Receipt: `7b562a87-d5f4-4873-b7c6-3d1115ec1b08`.
- ArtifactSet: `268a512c-50fb-4f48-bf96-a404c6224a18`.
- State: `succeeded`. This does not mean the HTTP successor path or later stages succeeded.
- Provider usage: 13,696 input + 23,368 output = 37,064 tokens; output includes
  17,667 reasoning tokens. The long latency is not a VLM rerun: it is one real
  text Stage 1 invocation with provider-default thinking. Explicit versioned
  thinking controls and cost/quality comparison remain a follow-up, not a silent
  mutation of this successful request.

Two additional observations from actual execution:

1. A read-only Responses retrieve during generation returned HTTP404
   ResourceNotFound for the announced response ID; the original stream then
   completed successfully. Investigate reconciliation handling: a premature
   lookup 404 alone must not be treated as proof the generation failed.
2. The installed SDK preserves the supplied direct schema but adds known
   optional top-level fields as null. The mock-HTTP serializer test checks all
   supplied fields exactly and only permits the observed optional null fields;
   it must not claim SDK HTTP bytes equal the pre-SDK request bytes.
