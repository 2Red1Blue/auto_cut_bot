# 历史集成草案（已被审查否决）

> 本文保留用于解释方案演进，不再作为实现依据。当前唯一实现设计为 `design.md`。本文中的 `window_start + relative_tick`、候选后 lazy ASR、tier fallback、单一 final source tick 和独立 BoundaryAlignmentCompiler 均已废弃。

## 决定

保留原版最强的两项能力，但在新 Kernel 中重建为小而封闭的组件：

1. **窗口视频 VLM**，不是纯关键帧 VLM。短窗口代理视频保留动作、对白、转场和节奏信息；VLM-first 的语义质量以此为主。
2. **三层边界对齐**，不让 VLM 决定最终物理切点。VLM 只给候选区间和 cue；最终边界按 `ASR anchor → VAD → visual scene` 的顺序确定。

OCR 不进入该方案。

## 新运行时的数据流

```text
EpisodeSourceSet
  → MediaEvidence（source hash / time-base / PTS index）
  → WindowMediaSet（source-range ticks + 可重建低码率代理）
  → VlmEvidenceCommand
       prompt+schema version → provider video request → raw response
       → strict VlmObservationSet（仅 window-relative evidence）
  → SemanticChain / CandidateCatalog
  → CandidateDialogueEvidenceCommand（只处理候选附近的 ASR）
  → BoundaryAlignmentCompiler
       ASR anchor → VAD safe point → visual stable scene
  → ExactSpanCompiler → Render/QC
```

## 一、窗口视频 VLM

`WindowMediaSet` 的每个 window 固定绑定：source content hash、time-base、source start/end tick、代理 BlobRef/hash、提取版本和编码参数。首版只生成并消费**同一个**受验证的低码率代理版本，避免旧实现“480p 已生成但 VLM 实际用了 720p”的漂移。窗口长度、重叠、分辨率、码率属于 versioned `WindowSamplingPolicy`，进入 request hash。

模型只返回 window-local 观察：`window_id`、`relative_start_tick/end_tick`、`cue_text`、闭合集合中的 `fact_kind`、`evidence_window_ids` 和受限说明。Kernel 以 `source_tick = window_start_tick + relative_tick` 作唯一转换并验证它仍在 source MediaEvidence 中。这样修复旧实现“物理窗口从 0 播放、却要求模型写原片绝对秒”的歧义。

每次调用原子提交：`window_media_set`、`vlm_request_manifest`、`vlm_raw_response`（受保护 BlobRef/hash）和 `vlm_observation_set`。Prompt 完整 hash、schema hash、模型、参数、窗口/代理 hash 都进入 Command key；不保留未版本化 prompt override。

## 二、三层边界对齐

VLM 先提出语义候选；只对候选边缘附近做 ASR/音频处理，不为整剧先做昂贵 ASR。

| 优先级 | 证据 | 作用 |
| --- | --- | --- |
| 1 | `TranscriptAnchorSet` | 依据 VLM cue 的词级时间戳避开吞字、句中断裂 |
| 2 | `SpeechActivitySet`（VAD） | 在静音/非语音安全区内有限搜索 |
| 3 | `VisualBoundarySet`（scene/stable shot） | 在稳定镜头/切点中选择 canonical boundary |

`BoundaryAlignmentCompiler` 输出 `BoundaryProof`：原候选、每层尝试点、拒绝原因、最终 source tick、策略版本与输入 evidence hashes。若 tier 1 不可用，才进入 tier 2；若 tier 2 不可用，只有 visual policy 明确允许且没有对白风险证据时才用 tier 3。三层均无法证明安全则 candidate `denied`，不得渲染。

## 三、迁移与验收

只移植为新包内无 I/O 算法/测试向量：原版 prompt 的业务内容和 strict schema、`canonicalize_vlm_analysis` 的准入规则、场景/VAD/ASR anchor 的排序与最大位移原则、以及真实回归样本。

禁止直接 import：ArtifactBus、Stage、旧 DB client、旧 batch runner/provider/cache、prompt override 与 float-second Recipe。

实施顺序：WindowMediaSet → VLM port/parser → 原子 VlmEvidenceCommand → 三类证据 producer → BoundaryAlignmentCompiler → real-video live smoke。只有真实代理、真实 VLM、持久化 observations、候选级 ASR 和 exact span 全部连通，才称真实 VLM 验真通过。
