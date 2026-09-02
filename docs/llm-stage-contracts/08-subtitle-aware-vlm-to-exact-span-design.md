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

### 6.1 批次暂停、单集重试与断点续跑

以下是目标控制契约。episode 之间没有业务依赖，因此采用**有界并发计算 + 聚合失败关闭**：
单集失败不会取消兄弟集，也不会阻止尚未派发的独立 episode；它只阻止完整批次 Finalizer
和下游 Stage。已成功 Artifact 保持不可变并可被后续显式恢复计划复用。必须把三种动作分开：

- **Child retry**：同一个冻结 Request/输入，创建新的 Attempt，消耗该 child 的 retry/attempt
  budget；不能覆盖旧 Attempt 或 Receipt。
- **Selected recompute**：创建新的、与原 child lineage 关联的 Request/Command（可绑定新策略或
  新输入），拥有独立的 Attempt/预算；原始失败历史保持不可变，不能把新结果改写成旧 Receipt。
- **Batch finalization**：只读取每个 episode 最终选定且完全绑定的成功 child；不修改任何 child
  历史，也不把 inspection/recompute 结果伪装成旧 aggregate。

批次控制状态按以下规则解释：

```text
episode[i] = required child failed | denied | indeterminate
  → continue bounded execution of independent sibling episodes
  → block aggregate finalization and every downstream Stage
optional child failed | denied | indeterminate
  → record OPTIONAL_CHILD_OMITTED when policy allows
  → indeterminate: reconcile first; failed: retry only when failure policy allows
  → selected recompute, if explicitly admitted, uses a new lineage-linked Request and budget
  → if episode[i] succeeds, reuse all compatible sibling successes and close the aggregate
  → if non-recoverable or a closed budget is exhausted, terminalize the original batch as failed/denied
```

selected recompute 路径创建 successor Batch，导入可 exact-hash 复用的成功 episode，只执行
失败或显式选择的 episode，再由 successor Finalizer 重新验证全集闭包。原批次永远不重新打开。

这里的 `episode[i]` 是**持久化 child 状态**，不是进程内变量。状态补充定义如下：

- `not_started`：没有持久化 Claim/Attempt；进程崩溃时保持此状态，不得凭空重试一个可能已经
  产生外部副作用的调用。规范要求 **Claim/Attempt intent 先于任何外部副作用持久化**；
  若实现无法保证 write-ahead 顺序，出现“无 Claim 但有调度/lease/调用迹象”的崩溃窗口时，
  必须按 `indeterminate` 保守处理，而不能按 `not_started` 重发。
- `indeterminate`：已有 Claim/Attempt 或外部调用迹象，但结果未知（包括 lease 过期、超时和
  worker 崩溃）；必须先进入 reconcile。
- `blocked`：`indeterminate` 在规定次数/截止时间内仍不能确认，或策略/Request 级预算禁止新
  Attempt。它是
  child 级的持久化 admission barrier（不是可改写的批次终态），重启后可重现；不会自动发布，
  退出路径只有获准的 recompute、明确人工裁决或取消。取消不修改原 `blocked` 记录，而是追加
  一个 lineage 级的 `CANCELLED_BY_OPERATOR` 事件并把批次投影为独立的 `cancelled`；基础设施不可恢复
  错误追加 `failed` 投影。`cancelled`、确定性策略 `denied` 和基础设施 `failed` 都是批次终态，
  下游必须按投影值和事件 code 双重区分，不能把取消当作策略拒绝。取消后 recompute 出口关闭，
  这是有意的终态语义。当前运行时尚未把该目标状态完整投影为独立枚举，
  在此之前以 `indeterminate` + admission barrier 保守承载，绝不能把它当作成功。

- 已成功 episode 的 Request/Attempt/Receipt/Artifact 是不可变的，恢复时必须复用，不能因为
  后续 episode 失败而全量重跑。
- `indeterminate` 先执行 reconcile；只有无法确认外部结果且 retry policy 明确允许时才创建新
  Attempt，否则保持 admission barrier，等待明确的 recompute/人工裁决，绝不自动发布。
  `failed` 是否可重试由 failure policy 决定；确定性契约/策略拒绝才是 `denied`，并终止原
  child/request lineage。若要修订策略，必须创建显式的新 lineage-linked Request，不能重新
  打开或改写原 `denied` 记录；来源不确定的拒绝必须先归入 `indeterminate` 走 reconcile。
- reconcile 若确认外部调用已成功，必须由 Store 幂等写入一条带
  `reconcile_evidence_hash`、原 Attempt/Provider identity 和完整 source/policy 绑定的正式
  成功 Receipt；若采用“完成原 Attempt 的确定性收尾”路径，也必须写入同一个 evidence hash
  和 Provider identity。幂等键必须是领域隔离的复合键
  `(lineage_id, child_id, attempt_id, provider_identity, reconcile_evidence_hash)`（或证明这些身份已
  被规范化纳入 evidence hash），不得仅以裸 evidence hash 跨 child/Attempt/Provider 复用 Receipt；
  `reconcile_evidence_hash` 必须是 provider result identity/内容摘要的确定性函数，不得包含查询
  方法、查询时间或 worker identity；
  重复 reconcile 只能得到同一 Receipt；
  该 Receipt 才能被 Batch Finalizer 选用，禁止用内存结果或伪造 `succeeded` 字段替代。
- 每个 child 的 Attempt/Retry 计数、最后一次 failure code、reconcile 次数和截止时间都必须
  持久化并跨重启累计。`closed budget` 仅指 child 级 `child_retry_budget` 与 lineage 级
  `selected_recompute_budget`；`reconcile_budget` 和单个 Request 的 `request_attempt_budget`
  明确不属于 closed budget。每个 recompute Request 可以有自己的 `request_attempt_budget`，
  但每次获准的 recompute 还必须原子递减持久化的 lineage 级 `selected_recompute_budget`；后者不能
  因换 Request、重启、换 worker 或更换 idempotency key 重置。任何一个 required child 被判定
  不可恢复，或在仍需要恢复时再次请求但已无可用的
  `child_retry_budget`/`selected_recompute_budget`，**必须**终止原批次：child
  retry 耗尽使用 `RETRY_BUDGET_EXHAUSTED`，lineage recompute 耗尽使用
  `RECOMPUTE_BUDGET_EXHAUSTED`，确定性拒绝使用 `DENIED_NON_RETRYABLE`。本节的批次终态化针对
  lineage 中当前 active、尚未被 `superseded` 的那一代批次（初始批次或任一 successor）；已
  `superseded` 的旧批次不再接收新的失败终态投影。最后一个已获准的
  recompute 会把剩余 lineage 预算降为零，但不会在其结果尚未收敛时提前发出 exhaustion；只有
  后续仍需新的 recompute admission 且剩余预算为零时，才发出 `RECOMPUTE_BUDGET_EXHAUSTED`。
  单个 recompute Request 的 `request_attempt_budget` 耗尽只关闭该 Request，并把 child 保持为
  `blocked`（不再额外递减任何预算，包括 lineage 级 `selected_recompute_budget`，也不终止原批次）；`reconcile_budget` 耗尽
  同样只形成 child barrier。上述批次终态结果不可改写，但允许追加后文定义的 lineage 投影事件。

预算记账必须按以下原子转移执行，避免把“Attempt ordinal”误当成某一种预算：

| 结果 | Attempt ordinal | `child_retry_budget` | `request_attempt_budget` | `preinvoke_recovery_budget` | `selected_recompute_budget` | child/批次结果 |
|---|---:|---:|---:|---:|---:|---|
| 原始 Request 的初始 Attempt | +1 | — | — | — | — | 成功则 `succeeded`，失败则按 policy 进入 `failed` |
| 普通 child retry Attempt（含 transient failure） | +1 | -1 | — | — | — | 成功则 `succeeded`，失败仍 `failed`，可继续按 policy retry |
| 初始原始 Request 的 Attempt 在调用前崩溃，并经 reconcile 确认未调用 | +1 | -1 | — | -1 | — | 预留给随后唯一 recovery Attempt；不得自动开启第二个 recovery 循环 |
| 已在 Claim 事务中预扣预算的 retry/recompute Attempt 在调用前崩溃 | +1 | —（Claim 已扣） | —（Claim 已扣） | -1 | — | 仅记录 `ABANDONED_BEFORE_INVOCATION`；不得再次扣已预留预算 |
| 上述原始 child 初始崩溃预留的 recovery Attempt | +1 | —（崩溃行已预留） | — | — | — | 成功则 `succeeded`，失败按 child policy；再次调用前崩溃按 `preinvoke_recovery_budget` 再扣，耗尽时进入 `blocked` |
| recompute 崩溃 Request 的 recovery | — | — | — | — | +1（reconcile CAS 退还；随后新 admission -1） | 关闭旧 Request；新 Request/command 重新按 admission 与 Claim 规则计费 |
| recompute Request 获准 | — | — | 初始化为该 Request 上限 | — | -1（仅 admission） | 创建 successor/child Request |
| recompute Request 的一次 Attempt | +1 | — | -1（Claim 时） | — | — | 成功则收敛，否则按 Request policy |
| reconcile 查询 | 不新增 | — | — | — | — | 只增加 `reconcile_count`，不消耗 retry/recompute budget |

表中 `child_retry_budget` 表示初始 Attempt 之外的同一 child retry 槽位。所有已获准的 retry/recompute
Attempt 都必须在 Claim 写前事务中预扣其适用预算；只有**未预扣预算的原始初始 Attempt**在
reconcile 确认未调用时，才按保守策略额外占用一个 retry 槽位，该槽位对应随后唯一 recovery Attempt。
`preinvoke_recovery_budget` 是持久化的 child 级崩溃恢复上限，必须是有限非负整数且由 policy
明确初始化；当前首版 policy **固定为 1**，因此每个 child 最多自动获得一次 recovery Attempt。
每次确认的调用前崩溃扣减一次，耗尽后 child 进入 `blocked`，不得自动创建下一轮 recovery，
避免无外部副作用的崩溃循环。未来若将该值提高，必须同时定义每个新增 recovery admission 的
child retry 预算扣减和 frontier 规则，不能沿用本版的“唯一 recovery Attempt”语义。selected recompute admission 不得
重置该计数器或任何已持久化的累计计数。
若原始 child 的 `child_retry_budget=0`，初始 Attempt 的调用前崩溃不得产生 recovery Attempt；
reconcile 只记录 `ABANDONED_BEFORE_INVOCATION` 并将 child 置为 `blocked`，后续必须走显式 recompute。
recompute Request 的 `request_attempt_budget` 一律在其 Attempt Claim 时预扣；崩溃收尾不再重复扣。
若 recompute Request 经 reconcile 确认在 provider 调用前已 abandoned，则其 admission 预留的
lineage `selected_recompute_budget` 必须在同一 reconcile CAS 中**恰好退还一次**；该 Request
自身已消耗的 `request_attempt_budget` 不退还。退款与该 Request 的关闭必须在同一 reconcile CAS
事务中完成；后续 recovery Attempt
需由新的 recompute admission 原子重新取得一个 lineage 预算槽位；不能在旧 successor 上无预算续跑。
这样无外部副作用的崩溃不会吞掉 lineage 重算额度，但仍受 Request/child 的 Attempt 上限约束。
Claim/admission-event 的写前转移必须在调用前同一事务/CAS 中完成；仅初始 Attempt 的崩溃预算扣减与
`ABANDONED_BEFORE_INVOCATION` 在 reconcile 的独立 CAS 事务中完成；外部调用后的 terminal
Receipt 由独立的 CAS 保护收尾事务写入，不能在调用前预创建成功 Receipt。若某转移
涉及 frontier 变化，epoch 更新必须与该转移同一事务完成；
重启不得重新初始化已持久化的计数器。selected recompute 的
`selected_recompute_budget` 递减、原批次 `SUPERSEDED_BY_RECOMPUTE`/`superseded` 投影、successor
Batch 及其 pending Request/command 占位（或已存在 successor 的幂等指向），也必须在同一事务提交；
successor 创建失败时
整体回滚，原批次不得留下孤立的 `superseded` 投影。
- reconcile 后若仍无法确认结果，新 Attempt 只有在复用原 Attempt 的 provider 幂等键，或 producer
  已注册为 side-effect-free 时才允许；否则保持 `indeterminate`/`blocked`。reconcile 本身有
  独立的次数/截止时间上限，但确认后新 Attempt 仍消耗 child 的 Attempt budget，不能用双重计数
  绕过预算。
- reconcile 次数/截止时间属于 `reconcile_budget`，耗尽只把 child 推导为 `blocked`，不自动终止
  原批次；后续必须经显式 recompute、人工裁决或取消形成新的批次级投影。只有
  `child_retry_budget` 或 lineage 级 `selected_recompute_budget` 耗尽，才按上一条规则强制终止原批次。
- reconcile 若确认调用已发生且确定性失败，必须持久化带 evidence hash/Provider identity 的失败
  记录，再按 `failed` 的 failure policy 决定 retry 或终止；不能继续保持无原因的 `indeterminate`。
- 默认触发者是 Pipeline Recovery Controller：启动/lease 过期触发 reconcile，明确可恢复的
  transient failure 自动 retry；selected recompute、策略修订、人工裁决和取消必须由带幂等键的
  显式 operator/Agent command 触发，并经过同一 Admission。相同 command idempotency key 是
  同一命令的幂等重放；新的合法 recompute 必须创建新的 command key，并由新的 canonical
  `request_hash`（至少含新 Request id/parent attempt ordinal）区分，不能被前一个失败 Request 吞掉。
  恢复入口的去重域为 `(lineage_id, episode, effective_policy_hash, command_idempotency_key)`。
  当前 VLM 的 `selected_only` 是执行过滤
  入口，不自动提供上述预算、supersession 或批次收敛保证。
- 批次 Finalizer 仍采用 all-or-nothing：任何目标 episode 没有成功的完整证据集，就不能生成
  可供 Render/Publish 的批次结果。单集 inspection/recompute 结果不能冒充 aggregate。
- 并发窗口内和后续尚未派发的独立兄弟都可以完成，但必须各自持久化终态 child Receipt；
  兄弟失败按同一 failure policy 处理。只有当 Receipt 的 source、episode、
  `semantic_execution_hash`、effective policy 和依赖哈希与续跑目标完全一致时，Finalizer 才能复用
  该兄弟成功结果；Request identity/parent ordinal 仅用于 lineage 与命令去重，不作为语义复用条件；否则
  必须显式标记 superseded/invalidated 并重跑。Finalizer 必须逐 Receipt 决定收敛、取代或失效，
  不能静默丢弃或把 speculative 结果当作批次成功。调度器只维护持久化的 expected episode census、
  每集最新 Attempt/Receipt 和一个有界并发上限；它不建立虚假的 episode 顺序 frontier。
  Admission barrier 位于 aggregate：任一 required episode 没有兼容成功 Receipt 时，完整批次不可提交。
  相同 episode 的 retry/recompute 仍需通过 CAS、预算和幂等检查，避免重复调用；不同 episode 不共享
  这把锁。进程重启后从持久化 child 状态重建待执行集合，成功集只读复用，未收敛集才重新进入调度。

原批次一旦进入 `failed`、`denied`、`cancelled` 或 `superseded` 的终态投影不可逆；这里的不可逆只表示既有
终态结果不会被改写，终态批次仍允许 append-only 追加 lineage 投影事件。child 级 `blocked` 不是
原批次的终态，但它也不能被原地改写。获准的 selected recompute 或人工裁决必须在原批次上追加不可逆的
`SUPERSEDED_BY_RECOMPUTE` / `RESOLVED_BY_ADJUDICATION` 投影（并将原批次投影为 `superseded`），然后创建或指向一个新的
lineage 级 Batch 及 pending Request/command（最终 Receipt 只能在外部调用收敛后产生）；原批次不重开，新 child Receipt 不写回旧批次。`failed`/`denied` 批次仍可由显式、带
幂等键的 operator/Agent command 触发 successor recompute；`cancelled` 批次明确关闭该出口。`required=true` 的
child 才属于发布目标集合，任一不可恢复都会阻断该批次 finalize；`required=false` 仅表示可选/诊断
child，失败时可从 successor batch 的目标集合中显式省略并记录 `OPTIONAL_CHILD_OMITTED`，不阻断
required 目标的 all-or-nothing finalize。任何被纳入发布目标集合的 child 都必须显式标为 required，
禁止用“可选”静默掩盖缺失。Admission 时即须校验 `required` 标志与策略的 optional-omission
许可一致；标志与策略不一致的 Request 直接拒绝，不能等到 child 失败后再归一化。
optional child 若因 `reconcile_budget` 或 Request 预算进入 `blocked`，在策略允许时同样按
`OPTIONAL_CHILD_OMITTED` 处理，不进入 required frontier；若策略将其声明为发布目标，则必须改为
`required=true` 并承担 required child 的阻断语义。

取消是 admission barrier 而不是强杀外部进程：取消命令提交时已在途的 child 允许收敛并持久化
终态 Receipt，供 lineage 审计与未来 successor 的 exact-hash import 使用，但这些 Receipt 不能被
`cancelled` 批次的 Finalizer 选用；取消后不再接纳新 child 或新 Attempt。provider 在取消后返回的
结果仍写入原 Attempt/Receipt（append-only），由 successor import 明确重新接纳，不能写回已取消批次。

策略修订时，admission 必须先冻结一个可复算的 `effective_policy_hash`：使用版本化的
`JCS-v1`（RFC 8785 canonical JSON；UTF-8、键按 UTF-16 code unit 排序、遵循 ECMAScript 数字序列化、
枚举使用契约字符串）对 policy 版本、所有影响执行的参数、默认值展开后的 overrides、failure/recovery
policy 和 producer 配置的**白名单字段**做 domain-separated hash，具体构造为
`SHA-256(ASCII("policy-hash-v1/whitelist-" + decimal(N)) || 0x00 || JCS_bytes)`，其中 `0x00`
是单个 NUL 字节，输出为小写 hex SHA-256。
producer 白名单只允许稳定的
provider identity、模型发布/修订标识、执行域/合规区域、模型/算法版本、策略参数和能力声明，
排除本机路径、时间戳、环境变量、worker host、密钥和临时缓存路径。任何白名单集合增删都必须
递增 `policy-hash-v1/whitelist-N` 版本；跨语言实现必须通过包含非 BMP 键、浮点边界值和 `-0`
的固定向量测试。
该 hash 必须写入 Request、Claim、Attempt、Receipt、successor import 和 admission CAS 决策；复用只
比较持久化的 hash，禁止用运行时当前配置重新推导历史策略。hash 作用域是每个 child Request，
批次级仅保存 child hash 集合的规范化摘要。
`effective_policy_payload` 的字段集合版本化为 `effective-policy/v1`：
`{schema_version, whitelist_version, policy_version, parameters, failure_policy, recovery_policy,
producer:{provider_identity, model_release, algorithm_release, execution_domain, capability_flags}}`；
缺失字段与显式 `null` 不等价，集合语义的数组按规范化 identity 排序，序列语义数组保持原序，
所有 identity 使用契约规定的大小写/Unicode 规范化。批次级摘要先构造
`{"children":[{"effective_policy_hash":"...","episode_index":i}],"schema_version":"batch-policy/v1"}`
（children 按 episode index 升序；同一 index 再按 `effective_policy_hash` 字典序），再使用
`SHA-256(ASCII("batch-policy-hash-v1/whitelist-" + decimal(N)) || 0x00 || JCS_bytes)` 计算，不能由数据库行顺序决定。
首批跨语言固定向量（`whitelist_version=1`，展示 JCS bytes，不含换行）为：

| JCS bytes | `SHA-256(ASCII("policy-hash-v1/whitelist-1") || 0x00 || JCS bytes)` |
|---|---|
| `{"a":0,"😀":1}` | `67ae994c06cc46d6780bc808a52a1e9b335f819be22f0ad42d29b651104d6ada` |
| `{"n":0}`（输入 `-0` 按 JCS 归一化） | `9808edfb7b83e3e680d494e660de7a9f99b049fbcdb1fd32872c2d2dfdb76704` |
| `{"m":1e+21,"n":0.000001}` | `997f81fe8958d20800a108c4314d61a2c7dc6134d6b0b96143de1abaf8a2de93` |
策略修订时，复用判断按 episode 单独进行：只有该 episode 的 source、`effective_policy_hash`、
`semantic_execution_hash` 和依赖哈希仍与新批次目标完全一致，才可复用旧成功 Receipt；策略哈希失配只使该 episode（及依赖它
的下游）失效，不得把不匹配的旧 Receipt 静默配入新批次。`semantic_execution_hash` 是
source/prompt/context/模型输入、`required` 标志和依赖输入的规范化摘要，不包含 Request identity、
parent ordinal 或 command key，用于跨 Request 的语义复用。`required` 必须持久化于 Request，并
纳入该摘要与 canonical `request_hash`。selected recompute 路径的续跑发生在
新批次中，原批次历史保持不可变。

Claim/Attempt intent 必须在调用 provider、探测器或其他有副作用的 producer 之前提交。若进程
在 Claim 之后、调用之前崩溃，reconcile 应确认“未发生调用”并按原 Request 的 retry budget
收敛；“未发生调用”只能由 provider 幂等查询、受信任的本地 invocation ledger/事务记录或
side-effect-free producer 的明确证明支持，不能以“没查到记录”代替。若无法确认，则保持
`indeterminate`，不得直接当作 `not_started` 重试。

确认未发生调用的 crashed Attempt 仍占用一个 Attempt ordinal，并以
`ABANDONED_BEFORE_INVOCATION` 记账；这样重启不会重置 child budget。只有在新的 Attempt
复用原 provider 幂等键或 producer 明确无副作用时，才能继续尝试。

当前实现状态（截至 2026-09-02）：VLM 已提供 `selected_only` 执行入口；VLM 在 probe 的 retry
链未收敛时保留成本保护，但 probe 一旦终态，其他独立 episode 仍继续执行，aggregate 保持关闭。
Media Preflight 已按默认最多 3 集有界并发执行（该运行参数不进入证据身份），并在 Stage adapter
中支持 `selected_only` 过滤；但正式
HTTP successor 仍缺少跨 Run source/VLM binder、媒体 child 持久化 Attempt 预算和 mixed aggregate
Finalizer。因此 Media Preflight 的下一项实现不是重新跑整批，而是新增带 source/policy/episode 精确绑定的
单集重跑与断点续跑入口，并补齐上述 child/batch 收敛规则。

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
