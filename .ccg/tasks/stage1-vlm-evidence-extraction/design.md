# 当前 Kernel 架构中的 VLM-first 集成设计

本文是本任务唯一实现设计。`integration-design.md` 只保留审查与迁移背景；发生冲突时以本文为准。

## 1. 架构决定

窗口视频 VLM、Transcript/VAD、场景、字幕 timing 和 A/V 端点不是一条独立流水线。它们进入现有 `autocut_kernel`，分别成为 evidence producer、SemanticChain 输入与 ExactSpanCompiler 的约束。Pipeline Runtime 与 Agent Runtime 仍只通过同一组 Kernel Command 工作。

首版保持当前 v2.1.3 血缘：TranscriptSet、SpeechActivitySet 与其他媒体证据在 StartRun 前完整提交。VLM 负责主要剧情语义；Transcript 原文只按 Event/Candidate 时间片消费，不无界注入 VLM 或 Story Proposal。

## 2. 复用当前组件

| 当前组件 | 集成后的责任 | 禁止行为 |
| --- | --- | --- |
| `media/preflight.py`、`media/types.py` | 扩展为完整 root evidence：FramePtsIndex、AudioSampleBoundarySet、Transcript、VAD、Shot/Scene、VisualValidity、SubtitleCue timing | 另建文件权威或第二套 preflight |
| `store/postgres.py` | 继续拥有 Command claim、Receipt、ArtifactSet、精确读取、CAS；扩展 BlobRef/GenerationAttempt 所需表和方法 | 新建 VLM 专用数据库或旁路 JSON 成功状态 |
| `semantic_chain/*` | 消费已提交的 VLM observations/Event evidence，确定性生成 Narrative/EventCard/Blueprint | 让 Provider 直接创建 EventCard、Recipe 或路径 |
| `physical_edit/exact_span.py` | 从现有视频单流 fixture compiler 演进为 A/V 四端点可行关系与 canonical selection | 增加第二个 Boundary optimizer 或信任 VLM tick |
| `rendering/*`、`pipeline/render_local.py` | 只消费 admitted Recipe，继续本地渲染和 QC | 从 VLM 或旧 Stage 读取物理参数 |
| `agent_runtime/*`、cut_bot adapter | 提交 typed intent/Command，并读取 Kernel outcome | 直接调用 VLM、ASR、ffmpeg 或 Store 写接口 |
| `scenario_registry/*` | 继续作为 fixture/test composition | 变成生产 Source/VLM 数据权威 |

因此不会引入旧 ArtifactBus、旧 Stage、旧 DB client，也不会新建平行 orchestrator。

## 3. 现有命令链的演进

当前 `LocalMediaCommand` 把 preflight、单视频 span 和 recipe 合在一个 fixture 命令内。生产路径按同一 Store/Receipt 机制拆成职责封闭的 Commands：

```text
PrepareMediaEvidenceCommand
  → root MediaEvidence ArtifactSet
GenerateVlmEvidenceCommand
  → VLM request/raw response/observations ArtifactSet
CompileSemanticChainCommand
  → Narrative/EventCard/Blueprint ArtifactSet
CompileExactEditCommand
  → A/V feasible endpoints/Recipe/CompilationReport/Admission
RenderLocalCommand
  → asset/QC/local promotion
```

每个 Runtime 调用的都是这些 Commands。禁止 Runtime 自行串接 provider 或写中间文件来改变业务状态。

## 4. Root MediaEvidence

`PrepareMediaEvidenceCommand` 扩展当前 ffprobe preflight，在同一 root set 中提交：

- SourceManifest、视频 FramePtsIndex、音频 AudioSampleBoundarySet；
- 完整 coverage 的 TranscriptSet 与 SpeechActivitySet；
- ShotBoundarySet、SceneBoundarySet、VisualValiditySet；
- SubtitleCueSet：只要求可见时间、coverage、误差上界和 clearance，不要求 OCR 文本；
- 所有 producer identity、Policy/Calibration refs 与 RootInputValidationResult。

ASR、VAD、scene、subtitle timing 可并行生产，最后统一验证。`failed|partial|indeterminate` 不能伪装成空集合。首版不在 CandidateCatalog 后追加 lazy ASR，因此不改变当前 RunManifest/root 血缘。

## 5. WindowMedia 与 VLM

`GenerateVlmEvidenceCommand` 在 committed root evidence 上生成 Kernel-owned `WindowManifest` 和低码率窗口代理。每个窗口绑定 Source/stream、core range、context overlap、代理 BlobRef/hash、预处理策略和 `ProxyTimelineMap` 或可验证纯平移证书。

VLM 返回只形成 coarse semantic observations：

- provenance 由 Kernel request manifest 派生，模型不能自报；
- provider 时间带 quantization/uncertainty，经 ProxyTimelineMap 后仍只是 coarse interval；
- 任何 VLM 时间都不能成为 FramePtsIndex/AudioSampleBoundarySet endpoint；
- overlap 使用 Kernel-owned core ownership；重复项确定性归并，矛盾进入 ConflictDiagnostics，不按到达顺序或多数票删除。

VLM request identity 绑定完整 prompt/schema/model/provider preprocess/window proxy hashes。外部调用使用现有 Command/Receipt 框架：durable attempt reservation → 单次 invocation → raw immutable blob staging/hash verification → parse/evaluate → DB transaction/CAS commit。超时后的结果不明保持 `indeterminate` 并 reconcile，禁止盲重试。

### 5.1 主 Runtime Adapter：Doubao Ark

生产主适配器是 `doubao-ark-files-responses-stream-v1`：

- 使用火山方舟官方 `volcenginesdkarkruntime` SDK；代理视频通过 Files API 上传，模型通过 Responses API 的 SSE 流式事件消费；SDK `max_retries=0`，每次 Kernel Attempt 最多上传一次并最多提交一次模型请求；
- Files API 的 `file_id` 不是模型请求 ID。它按 `provider_id + proxy content hash + preprocess policy hash` 进入 PostgreSQL lifecycle，并在复用前通过 Files API 重新验证状态与本地保守 TTL；禁止把旧 JSON cache 或未知有效期的 ID 当成权威；
- Responses API 使用 strict `json_schema`。只接受 `response.completed` 的完整输出；`response.incomplete` 即使已有部分 JSON 也失败关闭，流结束但无 terminal event 则进入 `indeterminate`，不得自动补 token 或重提；
- Provider 输出的 frame ID 必须引用当前窗口白名单。Kernel parser 仅可把 `sha256:` 后至少 40 位且唯一匹配的截断前缀规范化为完整白名单 ID；这是版本化、可复算的身份规范化，不是素材选择。短前缀、歧义前缀、完整未知 ID 继续 fail-closed，原始响应 Blob 永不改写；
- 请求参数中的 adapter strategy version、fps、token budget、temperature 全部进入 `VlmRequestIdentity`，改变策略必须生成新的请求身份；
- Responses 请求一旦取得 provider response ID，可以用 retrieve 做只读 reconcile；没有 response ID 的模糊超时保持 `indeterminate`，绝不以另一次 create 猜测恢复；
- API key 只由 Runtime composition 注入，不写入 request payload、Artifact、日志或仓库。

已实现的 Qwen `qwen-video-json-object-schema-prompt-v2` 保留为备用与跨 Provider 契约验证，不是生产默认，也不拥有另一套语义或持久化路径。

`IdentityProxyWindowBuilder` 只用于“提交的 MP4 本身就是 Source”的首个真实测试切片。它用 ffprobe 枚举完整 decoded frame PTS、用 ffmpeg 抽取并哈希确定性样本、把原视频 bytes 写入 Blob Store，并生成真正的 identity translation certificate。任何转码、VFR 重写、裁窗或 PTS reset 都禁止使用该 Builder，必须由后续 `ProxyTimelineMap` producer 给出独立可验证映射。

`IdentityProxyWindowBuilder` 与 Provider 无关；Doubao 和 Qwen 都必须消费同一个 Kernel `WindowManifest` 与不可变代理 Blob。

## 6. 语义链落位

受信 adapter 将 committed VlmObservationSet 投影成现有 `SemanticChainInput` 所需的 `RegisteredFact`、`EvidenceRef` 与 `CatalogCandidateRef`。`SemanticChainBuilder` 仍是纯确定性函数，不读取图片、Transcript raw content、provider client、PTS 或路径。

Stage 2 Candidate enrichment 只按 Event declared range 消费 root Transcript/VAD/Scene evidence；Story Proposal 不读取 raw Transcript。这样 VLM-first 的语义主导权与 v2.1.3 完整 root evidence 可以同时成立。

## 7. 三类证据不是 fallback

旧 ASR→VAD→scene 顺序仅保留为候选枚举/搜索优先级。生产可行性取全部 required constraints 的交集：

```text
VLM coarse semantic interval
  + Transcript sentence/word protected ranges
  + SpeechActivity protected ranges
  + AudioSampleBoundarySet endpoint candidates
  + FramePtsIndex endpoint candidates
  + Shot/Scene/VisualValidity/SubtitleCue constraints
  → exact A/V feasible pairs
  → ExactSpanCompiler canonical selection
  → independent Admission recomputation
```

`physical_edit` 只生成视频 `in/out` 与音频 `in/out` 四端点及客观 BoundaryProof。不存在单一 `final source tick`，也不存在某层命中后跳过其他约束。唯一方案选择继续属于 ExactSpanCompiler。

## 8. 迁移原版能力的方式

原版 prompt 业务内容、strict schema、`canonicalize_vlm_analysis`、scene/VAD/ASR anchor 候选生成和回归样本先登记为 `algorithm_candidate|fixture_only`，再在 `autocut_kernel` 中用整数 tick、封闭 DTO 和无 I/O 纯函数重实现。Kernel 永不 import 原版 package。

## 9. 准入条件

这里的真实运行是生产同构的本地流程：真实 HTTP → 真实 PostgreSQL → 完整授权素材 → 真实 Ark/ffmpeg/ffprobe/ASR/VAD → Artifact/Receipt。`shadow` 只关闭外部发布副作用，不得切换为 fake、fixture 或录制响应；单元测试结果不能替代本链路的 Receipt。

进入真实 VLM smoke 前必须完成：

1. root evidence 完整覆盖及失败关闭；
2. Window core/overlap 与 ProxyTimelineMap；
3. VLM invocation/Blob/Receipt/CAS 状态机；
4. VLM observations 到现有 SemanticChain 的精确投影；
5. A/V 四端点与并列证据约束；
6. SubtitleCue timing producer；
7. VFR、非零 PTS、proxy PTS 重写、overlap 冲突、provider ambiguous timeout、A/V mismatch 和字幕切断阻断测试。

当前已用一段真实授权测试窗口完成：identity Source window → Qwen VLM → committed ObservationSet → SemanticChain。生产 root evidence、转码代理映射、Exact A/V span 与本地 Render/QC 仍是后续 gate；外部发布仍不在本任务范围内。
