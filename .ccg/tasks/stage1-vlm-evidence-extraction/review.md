# Layer 1 review — Root Media Evidence and VLM contracts

## Scope

- Root media evidence contract: frame PTS, shot/scene boundaries, audio sample boundaries, transcript, VAD, visual validity, subtitle timing.
- Provider-neutral VLM window, proxy timeline mapping, request identity, strict response parser, and global semantic-core ownership.
- No provider invocation, persistence migration, semantic-chain adapter, physical endpoint compiler, render, or publication behavior is approved by this checkpoint.

## Independent review round 1 — No-Go

The first independent review found two P1 gaps:

1. `WindowManifest` frame samples were not required to be members of the root `FramePtsIndexSet`.
2. `parse_vlm_response` derived ownership from one window and did not require the complete `WindowManifestSet`.

Both gaps could have made provenance and unique ownership conventions rather than enforced contracts.

## Repairs

- `WindowManifest` now requires the exact `FramePtsIndexSet`, checks source/hash/clock/time-base identity, and rejects every sampled source PTS not present in that index.
- The frame-index-set hash is transitively bound by the manifest and `VlmRequestIdentity`.
- `VlmRequestIdentity` binds the complete `WindowManifestSet` hash.
- Identity construction, identity verification, and parsing all require membership in the exact manifest set.
- `core_owned` is derived only through global `select_core_owner`; a single window cannot self-assign ownership.
- Added negative VFR-membership, forged-hash, and overlapping-context ownership tests.

## Independent review round 2 — Go

The second read-only review confirmed both P1 findings are closed and found no new P0/P1 issue.

## Verification

- Combined targeted suite: `118 passed`.
- VLM suite: `35 passed`.
- Ruff: passed.
- BasedPyright: `0 errors, 0 warnings, 0 notes`.
- `git diff --check`: passed.
- Known unrelated warning: the repository pytest configuration declares an `asyncio_mode` option not recognized in this environment.

## Decision

**Go for the Layer 1 checkpoint only.** The generated values remain coarse semantic evidence and cannot be consumed as physical edit endpoints. Store/attempt atomicity, real provider invocation, A/V exact pairing, and end-to-end admission require later checkpoints and independent review.

## Layer 2 review — Store lifecycle and exact A/V compiler

### Findings and repairs

- Normal command completion previously terminalized the whole Job. It now closes only its slot and Receipt; only an exact `FinalizeRunOutcome` command can atomically terminalize the Job.
- The first generation-attempt implementation bound only an opaque request hash. Main-agent adversarial review required and added durable `provider_id`, `provider_idempotency_key`, and exact request-payload `BlobRef` identity. These fields are immutable and the payload must be claimed by the same Job.
- Request and response blobs are content-addressed, byte/hash/length checked, immutable, and locator-free at the Kernel API.
- An ambiguous provider timeout leaves the command slot running and the attempt `indeterminate`; the same attempt may only reconcile and cannot dispatch again.
- The exact compiler now enumerates four endpoints from authoritative frame/sample evidence. VLM types are rejected at this boundary.
- A zero subtitle-clearance floor is rejected for the production A/V policy; detector timing error and the positive policy floor are conjunctive.

### Verification

- Store/unit/migration and A/V targeted suite: `73 passed`.
- Real PostgreSQL 16 Store integration suite after the main-agent repairs: `41 passed`.
- Ruff: passed.
- BasedPyright: `0 errors, 0 warnings, 0 notes`.
- Podman database `autocut` was created in `ac_postgres`, owned by the existing `ac_user`, and migrations `0001` through `0003` were applied. The legacy `ac_db` database was not modified.

### Decision

**Go for the Layer 2 checkpoint.** This does not yet approve a real provider adapter, VLM command orchestration, semantic-chain consumption, rendering, or publication.

## Layer 3 review — Durable VLM command and semantic adapter

### Scope and guarantees

- Added a provider-neutral `VlmProviderPort`; adapters receive the exact immutable proxy bytes and canonical request payload, but cannot parse observations, assign ownership, persist Artifacts, or select physical edit endpoints.
- `GenerateVlmEvidenceCommand` now owns the durable reserve/dispatch/respond/reconcile/commit state machine. An ambiguous dispatch is persisted as `indeterminate`; replay reconciles the same provider idempotency key and never dispatches a second request.
- Request payload, proxy bytes, raw provider response, provider identity, idempotency key, and provider request ID are durably bound to the same Job and generation attempt.
- A successful transaction commits exactly one ArtifactSet containing request record, response record, and `vlm_observation_set`. Invalid provider output preserves the raw response Blob but cannot create semantic evidence.
- The one-way semantic adapter admits only globally `core_owned` observations from the exact committed Artifact payload. Provider summary text remains explicitly untrusted, and all VLM intervals retain `semantic_precision=coarse_only`.
- Production semantic admission remains closed until its independent evaluators are connected. This checkpoint does not approve a real provider adapter or claim a real video/VLM end-to-end run.

### Adversarial checks and repairs

- Moved immutable proxy loading before the durable dispatch transition so a local Blob failure cannot falsely record that an external call may have happened.
- Matched ArtifactSet hashing to the Store's exact UTF-8 canonicalization (`ensure_ascii=False`); a Chinese-summary integration fixture prevents encoding drift.
- Persisted `provider_request_id` for explicit terminal provider failure instead of losing the external correlation identity.
- Verified successful command replay reparses the persisted raw response without invoking the provider.

### Verification

- Default VLM/semantic/Store suite: `53 passed, 45 skipped` (PostgreSQL tests intentionally skipped without a DSN).
- Disposable PostgreSQL 16 integration suite: `45 passed`.
- Ruff: passed.
- BasedPyright: `0 errors, 0 warnings, 0 notes`.
- `git diff --check`: passed.

### Decision

**Go for the Layer 3 Kernel checkpoint.** The next gate is a typed adapter for the active non-legacy VLM implementation plus a real source-window/proxy/model run. Fake-provider success is not accepted as completion of the VLM stage.

## Layer 4 review — Real Qwen video adapter and live smoke

### Implementation boundary

- Added the cut_bot-side `QwenVlmProvider`; the shared Kernel remains provider-neutral.
- The adapter submits Base64 MP4 through Qwen Chat Completions with SDK retries disabled, a 20 MiB pre-network cap, closed request parameters, sanitized terminal errors, and no reconciliation redispatch.
- Added a versioned prompt pack containing the complete response Schema and exact Kernel frame anchors.
- Added `IdentityProxyWindowBuilder` for the narrow case where the submitted MP4 is itself the Source. It collects real decoded PTS and sampled-frame hashes; it cannot be used for a transcoded proxy.

### Adversarial live sequence

1. Live request v1 reached Qwen and returned meaningful semantic content, but used legacy-like flat fields. Kernel rejected it with `MISSING_RESPONSE_FIELD`; no observation Artifact was created.
2. Live request v2 tried strict provider-side `json_schema`. Qwen's multimodal endpoint rejected the request with HTTP 400. Kernel recorded a terminal provider failure and created no ArtifactSet.
3. The adapter was corrected to the officially supported multimodal `json_object` path, with the complete Schema included in the versioned prompt. Live request v3 succeeded and committed four coarse observations.

The two failed Attempts remain durable audit records; neither was overwritten or converted into success.

### Live acceptance evidence

- Source: existing authorized `w001-480p.mp4` test window (4.65 MB).
- Provider/model: `qwen-openai-chat` / `qwen3.7-plus`.
- Durable result: Attempt `committed`, provider request ID retained, exactly one `vlm_request_record`, one `vlm_response_record`, and one `vlm_observation_set` in the committed ArtifactSet.
- Parsed result: four observations, all `core_owned=true` and `semantic_precision=coarse_only`.
- Replay used a Provider implementation that raises on both `dispatch` and `reconcile`; replay still succeeded from the immutable raw response and produced four candidates/four Narrative nodes. This proves no hidden second provider call.

### Automated verification

- Runtime adapter tests (including real ffmpeg/ffprobe identity-window construction): `6 passed`.
- VLM/semantic targeted suite: `59 passed, 4 skipped` without a PostgreSQL DSN.
- Disposable PostgreSQL Store/VLM suite: `45 passed`.
- Ruff: passed.
- BasedPyright: `0 errors, 0 warnings, 0 notes`.

### Decision

**Go for the real Qwen VLM test slice.** This is not production VLM completion: HTTP Pipeline composition, transcoded proxy timeline proof, production semantic Admission, local ASR/VAD conjunction, and Ark provider file-id lifecycle remain open.

## Layer 5 review — Doubao Ark 主适配器与真实流式验真

### 实现边界

- 新增生产主适配器 `doubao-ark-files-responses-stream-v1`，使用官方 `volcenginesdkarkruntime`；Qwen 仅保留为备用与跨 Provider 验证。
- 视频通过 Files API 上传，Responses API 固定 `stream=True`、strict `json_schema`、SDK `max_retries=0`。一个 Kernel Attempt 最多一次上传、最多一次模型 create；只有已有 response ID 时才允许 retrieve reconcile。
- `file_id` 以 `provider + proxy content hash + preprocess policy hash + generation` 持久化。可用 ID 每次复用前重新验证；过期 generation 永久保留并允许新 generation，其他状态阻止并发重复上传。
- `response.incomplete` 即使含部分 JSON 也失败；缺 terminal event 保持 `indeterminate`。API key、旧 JSON cache、文件路径均不进入 Artifact。

### 真实测试序列

1. `vlm-live-w001-doubao-ark-v1`：8192 token 预算得到 `response.incomplete`，以 `PROVIDER_RESPONSE_INCOMPLETE` 失败；0 observation，未伪装成功。
2. `vlm-live-w001-doubao-ark-v2`：复用同一个已验证 `file_id`，32768 token 得到完整 strict JSON；本地 256 总摘要字符上限以 `SUMMARY_BUDGET_EXCEEDED` 拒绝。持久化响应为 4 条，摘要长度 71/66/79/75，总计 291。
3. `vlm-live-w001-doubao-ark-v3`：保持 32768 token，将显式 ParsePolicy 总摘要预算校准为 512；Attempt=`committed`、Command=`succeeded`，恰好提交 request/response/observation 三个 Artifact，得到 4 条 observation。
4. 使用会在 `dispatch/reconcile` 立即抛错的 Provider 重放 v3；结果仍为 succeeded，4 条全部 `core_owned`，确定性投影为 4 个 Candidate 与 4 个 Narrative node，证明没有隐藏远端调用。

### 可复算持久化证据（开发库 `autocut`，脱敏）

- v3 Attempt `292f9201-d90b-4c03-bf91-59d0381f319b`，request hash `sha256:df35178115bc69b47648860b9445bdc0162d4757eb757ee23ca234d79be99651`，state=`committed`。
- 原始流式响应 Blob hash `sha256:d41929a3305a7977564c9273495039736785597ecfa3ac8270d959e1922d07bf`；Receipt `7e1aa4e4-b307-400d-95a6-5b2e92bf195d`；ArtifactSet `8f3bd3c2-5495-43f9-aa50-108c9ea1f88a`。
- 三个成员 hash：request `sha256:7c02862b51ed0e2acd6e3fa11a346339226fe693b80eb87ee4a811fe29963aad`；response `sha256:01168853a4947f90c123d01706cb70b91c1c2606131cdc8c9913ca0cf726996f`；observation set `sha256:98245002d2f1862e3bf48ea8fece869c26e5fc0d033d1d45ec9e4476108de3cd`。
- provider media generation 1 为 `available`，绑定 proxy content hash `sha256:a584d2258b6e72cf9c582fdc6c0c76cdd4a7ed213042422cc2188eabeffc9b7a` 与 preprocess policy hash `sha256:55af868c27ec0cc999637c245690136664049f6230508400b2db387838c56ee7`。

### 自动验证

- Doubao/Qwen/迁移单元测试：18 passed。
- 独立 PostgreSQL Store/VLM/provider-media 集成：47 passed。
- provider-media generation/expiry CAS：2 passed。
- 全新临时 venv 仅安装 `volcengine-python-sdk[ark]>=5.0.45,<6` 后，官方 `Ark` import 与 client 构造成功。
- Ruff 与 BasedPyright：通过。
- 开发库 `autocut` 已升级 provider-media lifecycle；原 Qwen 与三次 Doubao 审计记录均保留。
- 已知基线问题：全量 `tests/pipeline` 收集仍会命中旧 `artifact_cache -> autocut_core` import；architecture 套件另有一个既存 wheel 元数据断言与 `autocut-kernel` 当前 `psycopg` 依赖冲突。两者不由本 diff 引入，已留给后续 package/runtime 收敛任务，不能用于声称全仓测试已通过。

### 决定

**Go for Doubao Ark streaming VLM evidence slice.** 尚未批准整剧生产 pipeline：下一阶段仍需 HTTP Pipeline composition、全剧窗口化代理及可验证 timeline map、ASR/VAD/视觉/字幕证据合取、Story 1–3、Recipe/Render/QC 与高光前端读取链路。

独立对抗审查首轮发现 `[ark]` extra 未声明、SSE 迭代异常丢 response ID 两项 P1；修复并新增 clean-env/default-factory 与 interrupted-stream reconcile 回归后，第二轮结论为 **Go，无残留 P0/P1**。
