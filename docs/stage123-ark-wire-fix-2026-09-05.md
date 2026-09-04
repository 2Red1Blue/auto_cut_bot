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
