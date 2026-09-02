# 08 字幕感知 VLM 到 ExactSpan 的融合设计

## 一、文档状态和结论

- 状态：目标设计，尚未完整接入当前 HTTP Pipeline。
- 当前执行事实仍以 `00`–`05` 与 runtime authority 为准。
- V23 字段迁移必须同时满足 [07 V23 全字段 Parity Matrix](./07-v23-field-parity-and-external-references.md)。
- 本文不定义 Stage 4 Rule ID、Policy 实例值或发布许可；它定义 VLM 富语义与现有
  SenseVoiceSmall/FSMN/Frame/Subtitle/ExactSpan 能力之间的责任和接口。

明确结论：当前短剧存在清晰烧录字幕，真实 Doubao VLM 已表现出高质量剧情理解。正确优化不是把
VLM 降级为纯视觉检测器，也不是让 ASR 重做剧情理解，而是：

```text
Doubao VLM：画面 + 烧录字幕 -> 富语义、剧情事件、候选价值、粗时间区域
SenseVoiceSmall：仅生成候选局部语音/词句时间锚点和句尾保护区，不承担对白语义
FSMN：仅生成语音活动与静音边界，不承担文本、人物或事件语义
Shot/Subtitle：仅生成视觉稳定区和字幕占用/clearance；字幕内容仍由 VLM 识别
FramePtsIndex/AudioSampleBoundarySet：合法视频/音频端点身份
ExactSpanCompiler：在冻结 Policy 下确定性求交并选择最终 A/V span
```

## 二、真实运行证据

PC run `pipeline_run_694567bc4b4e456a98aa939f71f24f84` 的第三次 VLM 原始响应包含：

| 对象 | 数量 | 实际表现 |
|---|---:|---|
| Entity | 8 | 识别 Ivy、Leon、Ronan、Talon、兔女、变色龙学生、椅子和学院 |
| Fact | 48 | 覆盖人物外观、动作、变形、发光印记、场景、字幕和时间模式 |
| Event | 24 | 正确组织梦境、两天前闪回、三次标记、龙降落、变身和人物互动 |
| Candidate | 1 | 正确判断“三名超自然男性宣称 Ivy 是伴侣”为核心 hook |

真实响应正确读出了 `The chosen one`、`Mate. Mine.`、`Two Days Ago` 等烧录字幕，并将它们与
人物动作、表情和剧情阶段结合。说明当前主要瓶颈不是“VLM 不理解视频”，而是：

1. 同一次调用同时承担完整观察图和 Candidate 闭包，字符串引用容易出错；
2. 某些 Attempt 为 24 个 Event 生成超过 Schema 上限的 Fact 引用，出现 `f049` 等未知 ref；
3. Candidate measurement/context 有时引用几乎整个窗口，局部错误会拒绝整次富语义结果；
4. Candidate support 可覆盖 0–241320ms 全集，表达的是故事素材范围而非一个可直接执行的切片；
5. 大量 `uncertainty_ms=0` 只是模型自述，不能证明 provider sampling、代理映射或物理端点零误差。

因此，迁移目标是保留或提高上述语义召回，同时缩小引用失败面，并把“故事范围”和“物理搜索范围”
明确分开。

### 2.1 Ark 音轨能力实测

2026-09-01 使用当前模型 `doubao-seed-2-1-pro-260628`、官方 Ark Files API + Responses
`input_video`、SDK `5.0.47` 做了三组同画面对照：灰色无字画面分别携带 “crimson falcon”、
“silver cactus” 两条 TTS 音轨及一条静音音轨。关闭 thinking 后三次均为 completed，但模型没有一次
返回真实口令：分别判断无语音、把用户 Prompt 错当成音频、在静音视频中捏造 “The quick brown fox…”；
三次 usage 的 `audio_tokens` 均为 `null`。首轮 thinking 调用也把两条有声视频判断为无语音。

脱敏 response id、媒体/响应 hash、stream metadata 和 request profile 已记录在
[Ark 音轨能力 canary manifest](./fixtures/ark-audio-capability-canary-20260901.json)。本次观察支持以下
fail-closed 决定：

```text
audio_track_consumed = false
audio_transcription_authority = none
screen_text_consumed = true  # 由真实剧集字幕 fixture 独立证明
```

这不是说模型永远不支持音频，而是当前 Files→`input_video` 路径不能为音频理解提供生产保证。
由于本次是 SDK direct canary，尚无 Kernel Command/Attempt/Receipt，所以它不直接注册 runtime
capability authority；在正式 canary Command 落地前，runtime 仍安全地保持音频能力为 false。
ProviderCapability 改变前必须用同一三臂 canary 重新证明，不能因模型声称“听到了”而升级能力。

## 三、第一性原理：字幕是语义证据，不是音频时钟

烧录字幕属于视频帧中的 `screen_text`。它能直接支持：

- 对白内容或其语义转述；
- 人物关系、冲突、揭示、悬念和叙事功能判断；
- 标题卡、时间跳转、地点和人物名识别；
- 值得保留的关键对白候选。

但单独的烧录字幕不能自动证明：

- 当前画面中的哪一个人是说话者；
- 字幕显示起止与真实语音起止完全一致；
- 翻译字幕与原声逐字相同；
- 字幕消失处就是安全音频切点；
- VLM 给出的毫秒是 FramePtsIndex/AudioSampleBoundarySet 中的合法端点。

因此不采用“纯视觉不能理解对白”的过度限制，也不采用“字幕准确所以无需 ASR/VAD”的过度放宽。
同一语义可以由多个 EvidenceAtom 支撑，但每个 atom 保留自身 modality 和时间精度。

## 四、两条证据链和汇合点

### 4.1 语义链：VLM 拥有剧情理解

```text
video window + burned-in subtitles + bounded WindowContextPack
  -> VLM Perception
  -> ScreenTextObservation / Entity / Scene / ModelObservedClaim
  -> Event / NarrativeInterpretation / EditorialSignal
  -> EventCard / NarrativeGraph / CandidateCatalog / Story / Blueprint
```

语义链回答：发生了什么、谁参与、为什么重要、属于什么叙事功能、哪些区域值得进一步处理。
VLM 读取画面字幕并拥有其语义解释权。SenseVoice 产生的识别 token 即使存在，也只用于词句边界、
utterance 聚合和 sentence-completeness 物理校验：不进入 VLM Prompt、Stage 1–3、Event/Fact、
Candidate 或 Story，不替代或修正 VLM 的字幕/剧情理解。

### 4.2 物理链：专用 producer 拥有端点和保护区间

```text
selected Candidate/Blueprint material requirement
  -> bounded local evidence window
  -> SenseVoiceSmall word timestamps + utterance grouping
  -> FSMN speech activity ranges
  -> FramePtsIndex / AudioSampleBoundarySet
  -> ShotBoundarySet / VisualValiditySet / SubtitleCueSet
  -> feasible video-in/out and audio-in/out endpoint relation
```

物理链回答：哪里存在词句边界、语音活动、真实帧、稳定镜头、字幕占用和可渲染 sample endpoint。
SubtitleCueSet 只需证明字幕显示区间和 clearance，不识别字幕正文。整条物理链不重新判断字幕含义、
剧情高潮或人物关系，也不能改变 Story。

### 4.3 唯一汇合点：ExactSpanCompiler

```text
Blueprint semantic intent + allowed VLM source regions
                         +
committed local physical evidence + frozen Policy/Calibration
                         ↓
          ExactSpanCompiler canonical selection
                         ↓
       SpanVariant -> Recipe -> independent Admission
```

VLM 的粗时间只限定搜索区域和叙事覆盖目标。`video_endpoint_ref` 只能指向带 stream/timebase 的
`FramePtsIndex` member，`audio_endpoint_ref` 只能指向带 sample clock 的 `AudioSampleBoundarySet`
member；ASR/VAD/Shot/Subtitle/VisualValidity 只提供 proposal、protection、clearance 和可行性约束。
最终选择还必须满足 DialogueGuard、minimum stable region、SubtitleCue clearance 和 A/V pairing。

## 五、目标对象及字段责任

### 5.1 ScreenTextObservation

V23 的 `fact_kind=screen_text` 已经是有价值的生产语义，不是待删除字段。目标结构是在不要求全量 OCR
转写的前提下增强它：

```json
{
  "screen_text_kind": "burned_in_subtitle",
  "text_mode": "verbatim_model_read|normalized_text|semantic_paraphrase|unreadable",
  "quote_status": "model_read_screen_text|paraphrase|unreadable",
  "text": "Mate. Mine.",
  "language": "en",
  "bucket_refs": ["T8", "T9"],
  "readability": "clear|partial|unreadable",
  "semantic_roles": ["dialogue", "reveal"],
  "evidence_atom_refs": [1]
}
```

规则：

- VLM 只返回剧情相关字幕、标题卡、人物名和关键屏幕文字，不输出逐帧全量 OCR dump；
- `verbatim_model_read` 只表示模型声称逐字读到清晰帧上文字，不表示独立 OCR 已验证，也不表示与
  原声音频逐字一致；
- `semantic_paraphrase` 可以用于剧情理解，但不能标成 quote；
- `quote_status` 在 VLM 原始输出中只能是 `model_read_screen_text|paraphrase|unreadable`；当前生产
  架构不使用 ASR 文本生成 `asr_transcript|reconciled` 语义状态，SenseVoice token 只留在物理时序
  producer 的受限证据中；
- 字幕与人物的关联使用独立 `SpeakerAttributionHypothesis`，不能因为镜头对着某人就强绑定 speaker；
- Context Pack 的角色名只能形成 identity assistance ref，不能单独创造字幕或对白 Claim。

`SpeakerAttributionHypothesis` 至少区分：

```json
{
  "state": "unattributed|hypothesized|indeterminate|resolved_by_registered_operator",
  "candidate_entity_refs": [0],
  "basis": ["visible_lip_motion", "subtitle_layout", "turn_taking"],
  "evidence_atom_refs": [0, 1],
  "context_assistance_refs": []
}
```

当前 SenseVoiceSmall/FSMN 没有注册 diarization/lip-sync 权力，因此 ASR/VAD 不能把最显眼人物自动
升级为 speaker。VLM 可以提供丰富 hypothesis，程序只验证候选实体、basis 和引用闭包。

### 5.2 ModelObservedClaim 与 Interpretation

模型观察和剧情解释必须同时保留，而不是二选一：

```json
{
  "model_observed_claims": [
    {
      "claim_kind": "visible_action",
      "summary": "三名男性分别在 Ivy 身上留下发光印记",
      "evidence_atom_refs": [0, 2, 3]
    },
    {
      "claim_kind": "screen_text",
      "summary": "字幕显示三人宣称 Ivy 是他们的伴侣",
      "evidence_atom_refs": [4]
    }
  ],
  "narrative_interpretations": [
    {
      "interpretation_kind": "relationship_claim",
      "summary": "三名超自然男性争夺 Ivy，形成核心关系冲突",
      "claim_refs": [0, 1],
      "epistemic_status": "strongly_supported"
    }
  ]
}
```

`model_observed_claim` 仅是 VLM wire-domain 名称；durable 投影继续兼容当前 `fact`/`vlm_fact`。
Interpretation 可以驱动候选召回和故事理解，但不能成为物理端点或发布许可。

### 5.3 Candidate 的两个范围

当前一个 `support` 同时承担“理解故事所需的范围”和“最终要剪的范围”，导致 Candidate 容易覆盖整集。
目标必须拆开：

| 字段 | 含义 | 是否直接用于端点 |
|---|---|---:|
| `scope_kind` | `local_moment|multi_beat_arc|episode_arc` | 决定是否允许进入局部精切 |
| `semantic_context_event_refs` | 理解 hook/highlight 所需背景，可跨较长范围 | 否 |
| `required_event_refs` | Story 必须覆盖的核心事件 | 间接 |
| `clip_inclusion_event_refs` | 确实需要形成物理素材的稀疏事件集合 | 间接 |
| `focus_regions[]` | VLM 建议重点搜索的一个或多个粗时间桶 | 仅限制搜索范围 |
| `reaction_region_refs[]` | 应考虑保留的反应镜头区域 | 仅限制搜索范围 |
| `physical_search_window` | 程序根据 focus region、Policy 和 Source map 扩张的局部窗口 | 是，作为 Preflight 输入 |
| `selected_span` | ExactSpanCompiler 输出的 A/V tick | 是，唯一物理结果 |

`context_event_refs/arc_context_refs` 不得自动扩大 Candidate physical support；Candidate 可以需要整集背景，
但物理搜索仍应围绕 anchor/payoff/reaction/clip-inclusion 的局部区域执行。`episode_arc` 只能进入
Stage 1/2 叙事，不得直接送入局部 ExactSpan。若一个故事需要三个相隔较远的事件，应生成三个
material requirements/SourceSpanRefs，而不是一个 0–241 秒巨型 clip。

### 5.4 时间和不确定性

- VLM 只选择程序冻结的 `T0..Tn` 时间桶或粗区间；不要求模型证明毫秒精度；
- 保留历史 V23 `start_ms/end_ms` 作为 dual-run 对照，但不进入物理端点；
- provider sampling error、proxy timeline mapping error 和 model localization uncertainty 分开保存；
- `uncertainty_ms=0` 不能由模型自证。程序按 ProviderCapability、采样 Policy 和 ProxyTimelineMap 合成
  conservative semantic bound；
- Stage 4 使用 Source clock 的 integer tick、FramePtsIndex 和 AudioSampleBoundarySet。

## 六、分层准入和局部恢复

| 层 | 失败时保留什么 | 处理方式 |
|---|---|---|
| Root JSON/终态不完整 | 什么都不晋升 | reconcile 或同视频有界重试 |
| Perception/Claim | 只保留完整且引用闭合的独立 item | 必要时同视频重跑目标分区 |
| Event/Narrative | 保留已验收 Perception/Claim | 局部语义 repair 或重跑目标分区 |
| EditorialSignal 非 anchor 字段 | 保留 VLM anchor/role refs 与全部已验收剧情语义 | Candidate Enricher 只修 enum/measurement 等失败字段，不重传视频 |
| EditorialSignal anchor 缺失/越界 | 保留其他已验收剧情语义，原 Candidate blocked | 带视频重跑目标 Editorial section；文本 Enricher 不得猜新 anchor 冒充 VLM parity |
| Local Media Evidence | 保留 VLM/Story | 只重跑对应集、候选和 producer |
| ExactSpan infeasible | 保留 Story 和证据 | 尝试已注册 variant/替代素材；不能返回未验证 VLM 时间 |

真实运行中的 `f049`、measurement closure 或 enum ordering 错误不应抹掉已经准确生成的 Entity、Fact、
Event 和字幕理解。冻结 run `pipeline_run_694567bc4b4e456a98aa939f71f24f84` 作为强制分区恢复
fixture：当观察图通过而 EditorialSignal 的非 anchor 字段（enum、measurement、非核心 role ref）
失败时，观察图与已闭合 anchor/role refs 必须可复用，Candidate Enricher 在不重传视频的情况下只重建
失败字段；anchor 缺失、越界或无法 grounding 时必须带视频重跑目标 Editorial section。分区复用仍需
完整 root bytes、exact Attempt 绑定、dependency hashes 和
防 chimera 校验；初版只允许 exact dependency hash 相同的组合。item 必须保留 attempt、section path、
original ordinal 与 item hash，blocked slot 使用 tombstone，不能从不同完整 VLM Attempt 任意拼接或压缩
同名 ordinal。

## 七、与现有三层边界算法的衔接

保留历史已验证的确定性原则，但不复用旧浮点业务函数：

1. SenseVoiceSmall `output_timestamp=True` 产生版本化、带校准误差界的文件相对逐词时间估计；识别
   token 只用于对齐词/句边界、utterance 分组和 sentence-completeness，不能进入语义链；其时间也只
   用于 dialogue protection/proposal，不是 sample-accurate endpoint；
2. 相邻 word gap 按冻结 Policy 聚合 utterance protected ranges；
3. FSMN-VAD 独立补充无词或 ASR 漏识别的声学活动保护范围，可能覆盖哭声、尖叫、叹气，但 VAD
   不负责事件分类、文本或 speaker；
4. Frame/Shot/VisualValidity 排除白闪、转场、冻帧和短镜头；
5. SubtitleCueSet 与 clearance 防止留下半条字幕或在字幕消失边缘切断；
6. tight/scene/context 只表示叙事上下文宽度，对白、视觉和字幕安全始终是硬约束；
7. ExactSpanCompiler 在完整可行关系中返回 canonical minimum，并由独立 verifier/Admission 重算。

ASR 成功不能短路 VAD、Frame 或 Subtitle 校验；VAD 命中不能证明句子完整；字幕显示结束也不能替代
音频 sample endpoint。

## 八、开源与公开实践的吸收方式

| 项目/实践 | 本设计吸收 | 不照搬的部分 |
|---|---|---|
| [Azure AI Video Indexer](https://learn.microsoft.com/en-us/azure/azure-video-indexer/insights-overview) | face/object/OCR/scene/audio 等 insight 分 producer、带时间实例 | 不把 transcript/OCR 派生 emotion 伪装成纯视觉事实 |
| [Azure Prompt Content](https://learn.microsoft.com/en-us/azure/azure-video-indexer/prompt-overview) | 感知结果先持久化，再生成 scene-coherent prompt sections | 不引入 Azure preset 作为本项目 WindowPolicy |
| [Google Video Intelligence](https://docs.cloud.google.com/video-intelligence/docs/reference/rest/v1/videos/annotate) | label/shot/text/speech/object 等 capability 与时间粒度分离 | detector Feature 不替代 VLM narrative interpretation |
| [TwelveLabs Analyze](https://docs.twelvelabs.io/docs/guides/analyze-videos) | 同一媒体 Asset 可复用分析，chapter/highlight 与自定义 segment 分任务 | 不假设 Ark 具有相同音轨消费和 timestamp 精度 |
| [Incident Lens](https://github.com/rukaiya2000/incident-lens) | typed Scene/Event/Person/Object 与 timecode provenance graph | 示例工程不作为发布 Admission 标准 |
| [UniVTG](https://openaccess.thecvf.com/content/ICCV2023/html/Lin_UniVTG_Towards_Unified_Video-Language_Temporal_Grounding_ICCV_2023_paper.html) | Moment Retrieval、Highlight 和 saliency 使用任务专用指标 | 不用单一 aggregate score 掩盖 rare-event 退化 |
| [VTG-LLM](https://arxiv.org/abs/2405.13382) | 通用 VLM 的时间表达需单独训练和评测 | 不把 VLM 粗时间升级为物理 proof |
| [LongVU](https://arxiv.org/abs/2410.17434) | 已有明确 Candidate query 后可研究 query-guided 帧压缩 | 无 query 的首次富语义提取不做 query-guided 丢帧 |

这些参考支持“富语义、多 producer、可复用媒体、任务专用时序评测”。它们是设计证据和实验候选，
不是框架迁移要求。

## 九、评测和晋升门槛

### 9.1 VLM 语义质量

- Entity/Event precision-recall 与跨窗一致性；
- screen-text capture、readability、exact/paraphrase 分类准确率；
- dialogue semantics 与 speaker attribution 分开评测；
- dream/flashback/title-card 等 temporal mode accuracy；
- hook/highlight top-k recall、setup/payoff、reaction-shot 与 rare-event recall；
- Context-assisted identity/relationship 污染率。

### 9.2 定位和剪辑质量

- Moment Retrieval `R@1@IoU`、mAP；
- Highlight Detection mAP、HIT@1；
- VLM focus region 对人工高光的 recall，不能只优化区间短；
- ASR word/utterance boundary error、VAD protected-range recall；
- SubtitleCue in/out error 和 residual-subtitle rate；
- ExactSpan 吞音、硬切、短镜头、白闪、字幕残留和 A/V sync fixture；
- tight/scene/context 与历史 v4.2 已验证案例的 paired preference。

所有指标按字幕/无字幕、对白/动作、梦境/闪回、稀有事件、provider/profile 分 slice 做 paired
non-inferiority；结构通过率和 token 成本不能替代剪辑质量。

## 十、实施顺序

本节使用 README 中唯一的 Global Phase ID。P1A 与 P1B 在 P0 后并行；P2 不等待 Rich VLM 大迁移。

### Global P0：不改生产 V23，先冻结和证明

1. 保存真实单集三次 Attempt 的 request/schema/raw response/parser/validator baseline；
2. 生成机器 `V23ContractInventory`；
3. 建立真实字幕、speaker ambiguity、梦境/闪回和 Candidate 全窗污染 fixture；
4. 统计 V23 字段的实际下游消费者和失败路径。

### Global P1A：现有 V23 的 CandidateEvidenceWindow 兼容路径

1. 从当前 V23 anchor/support/payoff Event support 编译 bounded window，不等待新 `focus_regions`；
2. 对全窗 Candidate 用 anchor/direct/payoff/clip-inclusion 兼容投影收窄；无法收窄时标记
   `episode_arc|indeterminate`，不送 ExactSpan；
3. 建立 FramePts/AudioSample endpoint identity fencing；其他 producer 不得 mint endpoint。

#### P1A 当前实现状态（2026-09-02）

- 已实现纯 Kernel `compile_v23_candidate_evidence_window()`：只读取 anchor、supporting、payoff
  Event，确定性去重；`context_event_refs` 和 Candidate 自身可能覆盖全窗的 support 不扩大物理窗口；
- 已实现 `V23CandidateWindowCompileDecision`：绑定 Semantic Pack、direct Event dependency、Candidate、
  Request、Source clock、WindowManifest、FramePtsIndex 和 Policy hash；可用独立复算入口拒绝篡改；
- locality 在 mapping uncertainty hull 和 padding/frame-snap 后的最终物理窗口上各检查一次；分离的
  direct regions 路由为 `episode_arc`，过宽、比例过大或帧 lattice 无法覆盖 direct hull 时路由为
  `indeterminate`，均不产生 `CandidateEvidenceWindow`；
- 完整 `FramePtsIndexSet` 中相邻 PTS 的时间间隔表示上一解码帧的 presentation duration，不解释为
  “内部证据缺失”；无法形成非空范围或无法覆盖 direct hull 才失败关闭；
- V3 `plan_candidate_evidence_window()` 保持原样，兼容路径为新增模块，不重解释历史 Artifact。
- 已实现规范化 `V23CandidateDecisionSet` 与严格 codec：对 Semantic Pack 中每个 Candidate
  恰好保留一个按 `candidate_id` 排序的 `eligible|episode_arc|indeterminate` Decision；空 Candidate
  集仍验证 Pack、Manifest、FramePtsIndex、Source clock 与 Policy；缺失、重复、乱序、替换、跨绑定
  篡改、未知字段、浮点/布尔伪整数、重复 JSON key 和超限输入均失败关闭；
- DecisionSet 只保存上游哈希和 Source/Policy 绑定，不复制 SemanticPack、WindowManifest 或
  FramePtsIndex。codec 只恢复不可变值，不授予 Store authority；后续 committed reader 必须重新读取
  精确上游 Artifact 并调用 `verify_v23_candidate_decision_set()` 全量重算比对。
- 已实现 `CompileV23CandidateDecisionSet@1` 的两个封闭输入 scope：`complete` 只接受同一 Job 已提交
  并通过完整闭包重读的 V4 `vlm_semantic_pack_set`；`inspection` 只接受显式
  `V23InspectionSemanticInputsRequest`，以 child idempotency key 和完整 SourceManifest member reference
  重读一个已提交 V4 child。两者在确定性 Command claim 后通过现有 PostgreSQL
  ArtifactSet/Receipt/CAS 事务原子提交；Policy、scope、选择器、完整上游引用和 payload byte cap 全部
  进入 request hash。`complete` 仍复核 SourceManifest 每一集恰好对应一个有序 V4 输入；
  `inspection` 不创建或冒充 aggregate。
- 两种结果使用不可混淆且无前缀重叠的持久化身份：完整结果是 `v23_candidate_decision_set`，单集
  检查结果是 `v23_inspection_candidate_decision_set`；inspection payload 还显式保存
  `result_scope=inspection` 和独立 schema version，精确 reader 返回值也保留
  `result_scope=complete|inspection`。inspection Artifact 只供局部重算、调试和后续候选级证据任务，
  不授予完整批次、渲染或发布权限。`max_payload_bytes` 约束最终持久化 payload；因此 inspection 的
  预算包含 `decision_set/result_scope/schema_version` wrapper 的固定开销，而不是只计算内层 DecisionSet。
- 已实现精确 committed reader：重读 SourceManifest、V4 Pack/Request/Window/FramePts 闭包和最终
  Receipt/ArtifactSet，严格解码 DecisionSet 后独立全量复算；它允许 PostgreSQL `jsonb` 只改变 JSON
  文本排版，但拒绝语义、引用、顺序、哈希、scope、revision 或 producer identity 漂移。
- 已用一次性本地 PostgreSQL 测试库验证首次提交、进程重启后精确重读、同键零 Provider 重放以及
  同键 Policy 漂移冲突。该 Command 是纯派生步骤，重跑不会再次调用 VLM。
- 已实现 `read_committed_v4_semantic_child_inspection()`：对多集 Source 中单独成功的一个 V4 child，
  重读同一 Job 的 Source owner、三成员 ArtifactSet、request/raw-response Blob、V4 Pack 与 Window
  identity，并返回固定 `result_scope=inspection`。它不创建也不伪造 `vlm_semantic_pack_set`，V3 child
  不能进入该入口。其内层值使用独立 `CommittedV4InspectionInput`，不是完整批次使用的
  `CommittedVlmSemanticInput` 子类型，因而不能被只接受完整批次输入的编译入口结构性误接收；该值还
  自校验 request identity、Source/Window、response artifact payload/hash 与 raw-response Blob 的闭包。
- 已用真实双集 PostgreSQL 场景验证：只生成第 2 集 child、batch aggregate 明确不存在，Store 重启后
  inspection 仍可精确恢复，并能编译、提交和精确重读独立的 inspection DecisionSet；同 key 重放不再
  编译或调用 Provider，Provider 总调用次数保持一次，aggregate 在编译前后均不存在。另以重新计算
  member/ArtifactSet hash 的持久化篡改测试验证 ordinal、request identity、Source owner、episode、
  provider request identity 与 raw-response 绑定任一漂移都会拒绝；inspection wrapper 内层 DecisionSet
  即使被替换为结构有效值并同步重算 member/ArtifactSet hash，也会被独立复算拒绝；
  完整批次 reader、旧 V3 bytes 与 V23 DecisionSet 提交回归同时通过。
  `CommittedV4SemanticChildInspection` 与 `V23CandidateDecisionSetStore` 是 Kernel 内部的精确投影接口；
  child idempotency key 是强制字段，仓库内所有构造点和 Store 实现必须显式提供它，旧的无 key 投影不得
  通过兼容默认值继续运行。

实现与测试：

- [`v23_candidate_evidence_window.py`](../../packages/autocut-kernel/src/autocut_kernel/media/v23_candidate_evidence_window.py)
- [`v23_candidate_decision_set.py`](../../packages/autocut-kernel/src/autocut_kernel/media/v23_candidate_decision_set.py)
- [`v23_candidate_decision_set_codec.py`](../../packages/autocut-kernel/src/autocut_kernel/media/v23_candidate_decision_set_codec.py)
- [`compile_v23_candidate_decision_set_command.py`](../../packages/autocut-kernel/src/autocut_kernel/pipeline/compile_v23_candidate_decision_set_command.py)
- [`test_v23_candidate_evidence_window.py`](../../tests/media/test_v23_candidate_evidence_window.py)
- [`test_v23_candidate_decision_set.py`](../../tests/media/test_v23_candidate_decision_set.py)
- [`test_compile_v23_candidate_decision_set_command.py`](../../tests/pipeline/test_compile_v23_candidate_decision_set_command.py)
- [`test_vlm_v4_store_postgres.py`](../../tests/pipeline/test_vlm_v4_store_postgres.py)

当前尚未完成的是把这两个已经进入 V23 Command 的显式 scope 接入 Pipeline Runtime、候选级
SenseVoice/FSMN 子命令和结果回填。selected-only 路径虽然已经获得局部 DecisionSet 的持久化能力，
但仍没有完整批次、渲染或发布权限，因此仍不能把本节表述为真实 pipeline 已跑通。

### Global P1B：分区恢复和 Rich VLM dual-run

1. 引入 `ScreenTextObservation`、`EvidenceAtom` 与对象级 `ContextAssistanceRef`；
2. Claim 使用 section-level canonical 数组，Event 只引用 Claim ordinal；
3. 将 `window_summary`、Candidate 多事件角色、measurements、tags/editing modes 全量保留；
4. Candidate 增加 `focus_regions[]`，Context 不扩大 physical search range；
5. 引入 `scope_kind` 与稀疏 `clip_inclusion_event_refs`，episode arc 不进入局部 ExactSpan；
6. 新旧 Schema 对同一视频 dual-run，不覆盖历史 V23 Artifact。

### Global P2：Candidate-local Media Preflight 与 ExactSpan shadow

1. 根据 Blueprint material requirement 编译 bounded local evidence window；
2. 只对受影响集/候选运行 SenseVoiceSmall、FSMN、Frame/Shot/Subtitle producers；
3. 每个 producer 独立保存 coverage、clock、Policy、Calibration 和 outcome；
4. 接入完整 endpoint relation、DialogueGuard、Subtitle clearance 与 VisualValidity；
5. 生成 tight/scene/context variants 和可重放 certificate；
6. Recipe/Render/QC 本地产出，失败只重跑对应 producer/window；
7. 真实剧集通过 paired evaluation 后才切换 physical runtime authority。

### Global P3：Rich VLM 语义 authority 切换

P1B 的 dual-run 达到 07 全字段 parity 后才切换 Rich VLM/Stage 1–3 authority；它不阻塞 P1A/P2
精切 compatibility lane，也不得重解释历史 V23 Artifact。

### Global P4：离线优化

仅在冻结 fixture 上评估 DSPy、LongVU/query-guided sampling、模型切换和机械冗余压缩；任何方案都
必须同时保持字幕、稀有事件、hook/highlight recall 与下游剪辑质量非劣。

## 十一、禁止行为

- 因 ASR 没进入 VLM Prompt 而禁止 VLM 使用画面烧录字幕；
- 把 `screen_text` 降级成无消费者的 debug 字段；
- 把 VLM 精准理解直接当成 ASR word timing、Frame PTS 或安全切点；
- 为修一个 Candidate 引用而重新计费整集所有已通过语义分区；
- 用 Candidate 的 Context Event union 生成一个整集 physical clip；
- 用模型自填 `uncertainty_ms=0`、confidence 或 `pass` 证明定位安全；
- ASR 成功后跳过 VAD/视觉/字幕检查，或 Evidence 失败后回退到未验证 VLM 毫秒。
