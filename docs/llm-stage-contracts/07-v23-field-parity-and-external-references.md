# 07 V23 全字段 Parity Matrix 与外部参考

## 一、它是什么

V23 全字段 parity matrix 是一次 Schema 迁移的防丢失账本，不是要求新版照抄旧 JSON，也不是
简单比较字段数量。它逐项回答：

1. V23 当前字段表达了什么语义；
2. 当前哪些下游读取它；
3. 目标契约是原样保留、增强、等价重命名、程序无损派生、进入 shadow，还是计划删除；
4. 用什么 fixture、指标或闭包证明迁移没有使视频理解和剪辑质量下降。

在本矩阵对应项没有验收前，不允许从生产 VLM Schema 删除字段。新增字段先进入 shadow；有明确
消费者、评测指标和 provider capability 后，才能晋升为生产字段。

## 二、处置类型

| 标记 | 含义 |
|---|---|
| `KEEP` | 语义和生产消费者均保留 |
| `ENHANCE` | 保留旧信息并增加表达能力 |
| `RENAME_EQUIV` | 模型字段改名或改成短 ordinal，但语义必须无损 |
| `PROGRAM_DERIVE` | 从冻结请求或已验收对象确定性生成；模型不再回显 |
| `DUAL_RUN` | 新旧表达同时保存，指标通过后才能切换 |
| `SHADOW` | 保存用于评测/校准，不得驱动 Admission |
| `REMOVE_AFTER_PROOF` | 只有证明无消费者且质量非劣后才删除 |

Parity 采用字典序目标：先满足 semantic coverage、grounding、rare-event recall、候选召回和下游
质量下限，再优化 token、延迟与成本。

## 三、根对象

| V23 路径 | 当前含义/消费者 | 目标处置 | Parity 验收 |
|---|---|---|---|
| `schema_version` | 选择 V4 decoder，阻止历史版本混读 | `PROGRAM_DERIVE`，移入 Attempt/request envelope | compiler 只能从 exact Attempt raw Blob 读取；schema/decoder hash 闭合 |
| `entities[]` | 当前窗口实体；Stage 1、实体合并 | `KEEP+ENHANCE` -> `perception.entities` | entity P/R、跨窗一致性、V23 kind/display/support 不丢 |
| `facts[]` | 视频支持的可见事实；Coverage、Stage 1 | `KEEP+ENHANCE` -> typed `model_observed_claims` | claim P/R、subject/object、support faithfulness 不下降 |
| `events[]` | 动作、互动、变化、反应、揭示、转场；Stage 1/Candidate | `KEEP+ENHANCE` -> `semantic_graph.events` | event P/R、角色和 Fact closure 完整 |
| `window_summary` | 窗口摘要及其证据引用；Stage 1 | `KEEP` | summary quality 与模型选择的 claim/event refs 同时保留 |
| `continuity` | wire 已预留窗口首尾、跨窗结构；当前 V23 Prompt 强制布尔值为 `false`、数组为 `[]`，能力实际未采集 | `ENHANCE` -> boundary snapshots + collection state | 旧空值只做 wire compatibility，不作为“观察为空”的 parity gold；unknown 不变成 false/empty |
| `candidate_hypotheses[]` | hook/highlight 与多事件角色；CandidateCatalog | `KEEP+ENHANCE` -> `editorial_signals`，后续跨窗归并 | anchor/support/context/payoff、measurements 和 support 全量 parity |

## 四、共享 Support 结构

下列结构由 Entity、Fact、Event、Continuity Temporal Segment 和 Candidate 复用。目标版本可以把重复
support 规范化成 first-class `EvidenceAtom`，但不能删除 grounding。

| V23 路径 | 当前含义 | 目标处置 | Parity 验收 |
|---|---|---|---|
| `support.support_kind` | 固定 `video_observation` | `PROGRAM_DERIVE` producer/modality class | producer 和请求允许的 capability 可复算；对象是否实际借助 Context 必须由模型返回闭合 assistance refs，不能从请求是否携带 Context 推断 |
| `support.interval_ms.start_ms` | 窗口相对开始毫秒 | `DUAL_RUN`，目标使用冻结时间桶/粗区间 | 与 V23 粗定位 IoU 非劣；不得当作物理端点 |
| `support.interval_ms.end_ms` | 窗口相对结束毫秒 | `DUAL_RUN` | 半开区间、范围和源映射全部有 fixture |
| `support.interval_ms.uncertainty_ms` | 模型声明的合并时间误差，当前上限 `5000` ms；真实 run 大量返回 `0`，不具测量意义 | `DUAL_RUN -> REMOVE_MODEL_NUMERIC`；目标由程序生成 `sampling_error_ms`、`model_localization_uncertainty_ms`、`timeline_mapping_error_ms`，再按 Policy 合成 conservative bound | 分项误差及合成值覆盖率校准；VLM 生产路径不得以全零 sampling/localization bound 晋升，ExactSpan 不消费模型自报误差 |
| `support.confidence` | 模型原始数值自评；当前 Coverage/material-support 直接消费 | `DUAL_RUN`：ordinal + raw score shadow + calibrated score | 原子切换完成后 raw 不再驱动 Admission；注册 profile 后 calibrated score 才可使用 |
| `EvidenceAtom.modality` | V23 无一等字段 | 新增 `SHADOW -> KEEP` | 只允许 `visual/audio/screen_text` 等可观察模态；逐模态归因准确率达到下限 |
| `EvidenceAtom.observable_description` | V23 分散在 summary/support | 新增 `SHADOW -> KEEP` | claim->atom faithfulness 与人工盲评通过 |
| `EvidenceAtom.bucket_refs` | V23 interval 的目标 grounding | 新增 `DUAL_RUN` | support temporal localization 达标 |

Context 不属于 Evidence modality。它只能通过独立的 `ContextAssistanceRef` 表示“模型在解释某个观察对象时
参考了哪个已冻结 Context 条目”。纯 Context ref 永远不能单独建立 Entity、Claim 或 Event，也不能满足
grounding/Admission；它只能辅助身份消歧、关系解释或 narrative hypothesis。程序只能验证 ref 是否属于本次
`WindowContextPack`，不能宣称模型实际使用了未显式返回的 Context。

## 五、Entities

| V23 路径 | 当前含义/消费者 | 目标处置 | Parity 验收 |
|---|---|---|---|
| `entities[].local_entity_id` | 当前响应短 ID 与交叉引用 | `PROGRAM_DERIVE` ordinal/alias -> durable ID | alias 映射闭合；ordinal 重排对抗测试 |
| `entities[].entity_kind` | `person/object/location/screen_text_source` | `KEEP+ENHANCE` | 四类完全覆盖；新增类型不能改变旧值解释 |
| `entities[].display_label` | 图和调试中的短显示名 | `KEEP` 但降权 | 未知人物使用视觉短标签；Context 名称不伪装成 observed |
| `entities[].visual_description` | 跨窗识别与人工诊断 | `KEEP+ENHANCE` | 相似人物、换装、遮挡 fixture 的 identity recall |
| `entities[].support` | 实体出现证据 | `RENAME_EQUIV` -> evidence atom refs | 每个生产 Entity 至少一个闭合 evidence atom |

目标新增但先 shadow：entity state、goal hypothesis、identity hypothesis、unresolved identity、场景内
appearance cues。Goal 不能进入 observed Entity，而属于 narrative hypothesis。

## 六、Facts / Model Observed Claims

| V23 路径 | 当前含义/消费者 | 目标处置 | Parity 验收 |
|---|---|---|---|
| `facts[].local_fact_id` | 响应内 Fact 引用 | `PROGRAM_DERIVE` | alias/ordinal 到 durable claim ID 可复算 |
| `facts[].fact_kind` | presence/state/action/change/relation/scene/appearance/screen_text/temporal | `KEEP+ENHANCE` | 九类 parity；新增类型先 shadow |
| `facts[].subject_ref` | Fact 主体 | `RENAME_EQUIV` 为 entity ordinal/alias | owner 和引用存在性闭合 |
| `facts[].object_ref` | 可空对象 | `RENAME_EQUIV` | null 与有对象语义不变；禁止默认补对象 |
| `facts[].summary` | 原子事实内容 | `KEEP` | 与 evidence atom 的 entailment/faithfulness 达标 |
| `facts[].support` | 独立 Fact evidence | `RENAME_EQUIV` | 不得因 Event support 相近而无条件复制 |

`model_observed_claim` 是目标 **VLM wire-domain** 名称，用来避免把模型自述误称为经过独立验证的事实；
本轮不把 durable `fact`/`vlm_fact` 对象类型原地改名。投影层继续生成现有 `fact` 和 `vlm_fact` 兼容对象。
若未来要改 durable object type，必须在同一 cutover 中升级 Stage 1、Narrative Graph、Candidate
measurement、owner policy、引用闭包与历史 decoder，不能只改 Schema 名称。

## 七、Events

| V23 路径 | 当前含义/消费者 | 目标处置 | Parity 验收 |
|---|---|---|---|
| `events[].local_event_id` | 响应内 Event 引用 | `PROGRAM_DERIVE` | ordinal/alias 映射与跨 Attempt 防 chimera |
| `events[].event_kind` | action/interaction/state_change/reaction/reveal/transition | `KEEP+ENHANCE` | 六类召回不下降；新增类先 shadow |
| `events[].summary` | 事件语义 | `KEEP`，Event 是 canonical owner | Narrative/Editorial 只引用，不复制改写事实 |
| `events[].participant_refs` | 参与实体 | `RENAME_EQUIV` 为 entity ordinals | 引用闭包、角色相似和数组重排 fixture |
| `events[].fact_refs` | Event 的事实依据；当前 V23 必须恰好一个 | `RENAME_EQUIV` 为 claim ordinals | 放宽为多个前必须有多 Claim fixture；每个被引 Claim 都与 Event support overlap |
| `events[].cause_event_refs` | wire 已存在，但当前 V23 Prompt 强制 `[]`，未评估因果 | `ENHANCE` 为 causality hypotheses + collection state | 旧 `[]` 不作 negative gold；not_evaluated/indeterminate/observed_empty 分开 |
| `events[].effect_event_refs` | wire 已存在，但当前 V23 Prompt 强制 `[]` | `ENHANCE` | 旧 `[]` 不作 negative gold；启用后需互逆/无环校验，推测不升级为 Fact |
| `events[].open_question` | 事件产生的未解问题 | `KEEP`，Event canonical owner | Narrative 和 Candidate 只能引用，不另造冲突文本 |
| `events[].temporal_mode` | present/flashback/flashforward/dream/unknown | `KEEP` | mode 与 supporting evidence/summary 闭合 |
| `events[].support` | 当前必须精确复制唯一 `fact_ref` 的 support | `RENAME_EQUIV` 为独立 Event evidence | 解除复制限制后仍须与每个直接引用 Claim 逐一 overlap；禁止只验证 ref 存在或无规则继承 |

## 八、Window Summary

| V23 路径 | 当前含义/消费者 | 目标处置 | Parity 验收 |
|---|---|---|---|
| `window_summary.summary` | 信息密集窗口摘要 | `KEEP` | 摘要覆盖/事实一致性与长度预算 |
| `window_summary.dominant_temporal_mode` | 窗口主时间模式 | `KEEP` | supporting Event 引用与枚举闭合 |
| `window_summary.fact_refs` | 模型选择的摘要 Fact grounding | `RENAME_EQUIV` 为 claim ordinals | 必须由模型选择；程序不能用“全部 Facts”代替 |
| `window_summary.event_refs` | 模型选择的摘要 Event grounding | `RENAME_EQUIV` | summary evidence closure |
| `window_summary.confidence` | 摘要自评；当前 Coverage 直接消费 | `DUAL_RUN` ordinal/raw/calibrated | 原子切换完成后未校准值不再驱动 Admission |

## 九、Continuity

这一组是 **wire-present / capability-not-evaluated**：当前 V23 Prompt 将四个布尔值固定为 `false`，
将 entry/exit refs 与 temporal segments 固定为 `[]`。这些值只证明旧 decoder 的结构兼容，不能证明
模型观察到“不连续”或“无时间片段”，因此不得作为新能力的 negative golden fixture。

| V23 路径 | 当前含义/消费者 | 目标处置 | Parity 验收 |
|---|---|---|---|
| `starts_mid_event` | 预留语义；当前固定 `false` | `ENHANCE` -> entry boundary snapshot | 旧值不计质量 parity；启用后 unknown 不得补 false |
| `ends_mid_event` | 预留语义；当前固定 `false` | `ENHANCE` -> exit boundary snapshot | 同上 |
| `continues_from_previous` | 预留语义；当前固定 `false` | `ENHANCE` -> direction + continuation hypothesis | 旧值不计质量 parity；只在 overlap evidence/候选实体内判断 |
| `continues_into_next` | 预留语义；当前固定 `false` | `ENHANCE` | 旧值不计质量 parity；collection state 和 contradiction refs 完整 |
| `entry_state_fact_refs` | 预留结构；当前固定 `[]` | `RENAME_EQUIV` 为 claim ordinals | 启用后 boundary owner/ref 闭包；旧空数组不是 observed_empty |
| `exit_state_fact_refs` | 预留结构；当前固定 `[]` | `RENAME_EQUIV` | 启用后状态变化守恒；旧空数组不是 observed_empty |
| `temporal_segments[]` | 窗口内时间模式片段；V23 当前固定空 | `ENHANCE` | 未采集写 not_evaluated，不写 observed_empty |
| `temporal_segments[].mode` | 片段时间模式 | `KEEP` | 五种 mode parity |
| `temporal_segments[].summary` | 时间片段摘要 | `KEEP` | evidence-grounded |
| `temporal_segments[].support` | 片段区间和置信 | `RENAME_EQUIV` | EvidenceAtom + 时间桶 parity |

目标 boundary snapshot 还应包含 entity/scene refs、未完成动作或对白行为、overlap bucket refs、
continuation hypothesis 和 contradiction refs。

## 十、Candidate / Editorial Signals

| V23 路径 | 当前含义/消费者 | 目标处置 | Parity 验收 |
|---|---|---|---|
| `local_candidate_id` | 响应内 Candidate ID | `PROGRAM_DERIVE` | ordinal/alias 闭合 |
| `candidate_kind` | highlight/hook | `KEEP` | 两类判定与空候选召回 |
| `anchor_event_ref` | 候选核心事件 | `RENAME_EQUIV` 为 Event ordinal | 不允许后续文本模型重新猜窗口内 anchor |
| `supporting_event_refs` | 直接支撑事件 | `RENAME_EQUIV` | V23 多事件角色无损 |
| `context_event_refs` | 理解所需背景事件 | `RENAME_EQUIV` | 不能和 support 混用，也不能扩大 Candidate physical search window |
| `payoff_event_refs` | 回报/揭示事件 | `RENAME_EQUIV` | hook 为空、highlight 闭合规则保留 |
| `open_question` | Hook 的具体未解问题 | `KEEP`，优先引用 Event open question | 不能出现已在窗口回答的问题 |
| `reason` | 为什么值得剪 | `KEEP` | 与 Candidate kind 和 evidence 一致 |
| `anchor_summary` | anchor 文本摘要 | `PROGRAM_DERIVE` 候选；切换前 `DUAL_RUN` | 证明从 Event summary 无损派生后才能删除模型字段 |
| `payoff_or_open_question` | payoff 或悬念摘要 | `KEEP/RENAME_EQUIV` | 不得因压缩丢失 setup-payoff 语义 |
| `dialogue_excerpt` | 对白语义摘记；真实 Doubao 输出可准确读取烧录字幕 | `ENHANCE` 为 dialogue act/paraphrase + `quote_status` | 允许 `model_read_screen_text` 直接进入语义链；`asr_transcript/reconciled` 必须绑定相应 producer。字幕不能单独证明 speaker、音频逐字或物理端点 |
| `editing_modes[]` | dialogue/action | `KEEP` | Canonical enum/order；provider 无音轨时 dialogue 需 evidence 限制 |
| `narrative_functions[]` | hook/setup/escalation/confrontation/reveal/reversal/payoff/aftermath | `KEEP` | 八类 parity 与多标签召回 |
| `tags[]` | `dialogue/action/emotion/suspense/conflict/reveal/reversal/visual_spectacle/character_moment/relationship_moment` | `KEEP` | 十类 exact literals parity；读取 telemetry 证明消费者 |
| `measurements[]` | 候选多维语义评分 | `KEEP+ENHANCE` | 六维、最小充分 evidence closure 和排序质量保留；禁止用闭包内全部 Fact/Event 填满引用 |
| `support` | Candidate 自身时间支持；当前可合法退化为全窗 envelope | `RENAME_EQUIV` 为 scope + 稀疏 inclusion refs + focus regions | 必须分别覆盖 anchor/direct/payoff；报告 support/episode duration ratio、clip-inclusion selectivity，Context refs 不扩窗，episode-arc 不进入 ExactSpan |

目标 Editorial Signal 增加 `scope_kind=local_moment|multi_beat_arc|episode_arc`、
`clip_inclusion_event_refs`、`arc_context_refs` 和 `focus_regions`。程序只从 anchor、direct support、payoff
和 clip-inclusion 的粗证据编译 `candidate_evidence_window`；一个故事包含多个远距离事件时生成多个
material requirements/SourceSpanRefs，不生成一个整集物理 clip。

### Measurements

| V23 路径 | 当前含义 | 目标处置 | Parity 验收 |
|---|---|---|---|
| `measurement_kind` | `hook_strength/reveal_strength/emotional_payoff_strength/dialogue_salience/action_salience/visual_salience` | `KEEP` | 六类 exact literals 均保留；未适用显式 not_evaluated |
| `value` | 模型原始分数 | `SHADOW` raw score + ordinal + calibrated score | V23 排序对照；原子切换完成后 raw 不再驱动 Admission |
| `confidence` | 对 measurement 的自评 | `SHADOW/DUAL_RUN` | calibration/reliability curve |
| `fact_refs` | measurement Fact evidence | `RENAME_EQUIV` 为 claim ordinals | 必须在 Candidate closure 内 |
| `event_refs` | measurement Event evidence | `RENAME_EQUIV` | 必须在 Candidate closure 内 |

### Confidence 的原子切换边界

当前 Coverage evaluator 会直接比较 Summary、Entity、Fact、Event 的 raw confidence，Candidate
material-support evaluator 也会直接比较 Candidate support 与 measurement confidence。因此不能先改输出类型、
后改消费者。切换必须在同一个 authority version 中原子完成：

1. 注册并冻结 calibration profile 与适用 provider/model/prompt/schema 范围；
2. 发布 calibrated/ordinal policy schema；
3. 同时升级 Coverage 与 material-support evaluator；
4. 通过同一批 V23/rich dual-run fixture；
5. 最后才允许 calibrated score 驱动 Admission。

切换前继续走现有 raw confidence 路径；目标 authority 已启用但 calibration profile 缺失、过期或不兼容时
必须 fail closed，不允许静默回退到模型 raw score。

## 十一、目标新增字段的晋升条件

| 新字段组 | 初始状态 | 晋升条件 |
|---|---|---|
| `scenes`、shot language、reaction shots | shadow | Stage 3/ExactSpan consumer + scene/reaction fixture |
| V23 `fact_kind=screen_text` / `screen_text_source` | keep | 字幕语义 recall、文本 faithfulness 和 Stage 1/Candidate consumer 保持非劣 |
| 结构化 `ScreenTextObservation` 增强 | dual-run/shadow -> keep | text role、readability、verbatim-model-read/paraphrase、粗 bucket、ASR-screen agreement、speaker-attribution precision 达标 |
| entity state / relationship observations | shadow | Stage 1 consumer + identity/relationship P/R |
| goal/motive/causality hypotheses | shadow | evidence closure + 不进入 Fact 的类型门禁 |
| emotional turns、setup/payoff links | shadow | Candidate/Stage 2 consumer + rare-event recall/faithfulness |
| contradictions/unresolved | shadow | taint consumer + unknown 不被默认化的回归测试 |
| `EvidenceAtom` | dual-run | claim->atom support P/R、modality accuracy、temporal localization 达标 |
| SectionValidationResult/Artifact | dual-run | dependency hash、跨 Attempt、防 chimera 和状态传播 fixture 通过 |

## 十二、机器生成的 Contract Inventory

手写 parity matrix 解释语义和迁移决策，但不能代替可执行的完整契约清单。Phase 0 应由实际代码生成
`V23ContractInventory`，并绑定 exact `schema_sha256`、`prompt_sha256`、`parser_sha256`。Inventory 至少逐路径记录：

- type、required、nullable、const、enum、pattern、minimum/maximum、min/max length 与
  `additionalProperties`；
- 数组 min/max items、canonical order、unique/cardinality 规则；
- Prompt 与 parser 承担的条件约束、null/empty 区别和跨字段闭包；
- 当前 V23 的关键条件：`event.fact_refs` 恰好一个、Event support 精确复制该 Fact support、
  `cause/effect_event_refs=[]`、Continuity 四布尔固定 `false` 且 refs/segments 固定 `[]`、
  `uncertainty_ms <= 5000`；
- Hook 的 payoff/open-question 条件、Highlight 的 payoff closure、Candidate measurement refs 必须属于
  Candidate 事件/Claim 闭包，以及 Candidate support 必须覆盖 anchor/supporting/payoff Events；
- closed enum 的 exact literal、canonical ordering、禁止重复引用和所有数值边界。

CI 在 schema、Prompt 或 parser 任一 hash 改变而 Inventory 未产生可审查 diff 时直接失败。这样“全字段 parity”
指向真实运行契约，而不是一份可能随着代码变化而过期的人工表。

## 十三、必须先建立的 Fixture 与指标

1. V23 原始 request/schema/raw response/parser 的冻结单集 baseline；
2. Entity、Fact、Event、Summary、Continuity、Candidate、Measurement 的逐字段 golden fixture；
3. 快速动作、微表情、反应镜头、铺垫/回收等 rare-event fixture；
4. Context Pack 剧透/身份污染、相似人物、换装、遮挡、窗口 overlap fixture；
5. 无音轨能力、无对白、字幕密集、标题卡、聊天界面和 screen-text unreadable fixture；
6. root 截断、尾分区截断、section blocked/indeterminate、引用闭包错误 fixture；
7. full rerun 禁止复用旧 section、dependency hash mismatch、canonical equality、ordinal 重排和
   taint 传播 fixture；
8. V23 与 rich-sectioned contract 的成本、信息覆盖、候选召回和 Stage 1–3 下游质量对照；
9. 固定任务指标：Moment Retrieval 使用 `R@1@IoU` 与 `mAP`，Highlight Detection 使用 `mAP` 与
   `HIT@1`，Summarization 使用 F1/事实一致性；
10. 所有指标采用 paired non-inferiority，比对同一冻结样本，并分别为 rare-event、modality、provider、
    长短窗口设 margin 和最低召回线；aggregate 通过不能掩盖某一 slice 退化。
11. 冻结真实 run `pipeline_run_694567bc4b4e456a98aa939f71f24f84`：保留 8 Entity/48 Fact/24 Event
    与字幕剧情理解；验证 enum/ref/measurement 错误只阻断 Editorial section，不重做已通过观察图；
12. 对该 run 验证 Candidate `0..241320` 被路由为 episode arc 或稀疏 focus regions、全零
    `uncertainty_ms` 被程序重算、局部 ASR/VAD window ratio 受 Policy 限制；
13. 增加字幕密集、花字/遮挡、字幕延迟、无音轨但有字幕、ASR 与字幕冲突、旁白/画外音、同屏多人
    speaker attribution、字幕跨 shot 和 ExactSpan 不截断对白/字幕/稳定镜头 fixture。

## 十四、外部项目与参考资料

### A. 视频理解和视频索引实践

| 项目/资料 | 相似经验 | 本项目吸收 | 不可直接照搬 |
|---|---|---|---|
| [Azure AI Video Indexer insights](https://learn.microsoft.com/en-us/azure/azure-video-indexer/insights-overview) | 多模型输出 faces、objects、OCR、scenes/shots、audio effects、topics、emotions 等分类 JSON，并带时间 instances | Rich semantic categories、独立 producer、temporal evidence | 其 emotion/topic 常依赖 transcript/OCR；timed insight 用于索引，不等价于本项目物理安全端点 |
| [Azure Prompt Content](https://learn.microsoft.com/en-us/azure/azure-video-indexer/prompt-overview) | 先索引，再按 scene 生成 prompt-ready sections，后续无需重新索引视频 | 冻结 VLM/感知 Artifact，Stage 1–3 只读投影 | Azure preset 与本项目 WindowPolicy 不等价 |
| [Google Video Intelligence](https://docs.cloud.google.com/video-intelligence/docs/reference/rest/v1/videos/annotate) | label/shot/face/speech/text/object/person 是独立 Feature，各自具有适用的输出粒度和 feature-specific config；frame/shot mode 不能外推到全部 Feature | 版本化 modality operator、EvidenceAtom、不同时间粒度 | 专用 detector 输出不能当通用 VLM narrative interpretation |
| [TwelveLabs Analyze](https://docs.twelvelabs.io/docs/guides/analyze-videos) | 复用 Asset 多次分析，支持 structured JSON、自定义 timestamped segments、chapter/highlight | 媒体 Artifact 复用、独立可缓存语义任务、丰富 editorial signals | 不能假设 Ark 同样消费音轨或具有同等时间精度；其 segment 也不自动成为 ExactSpan proof |
| [Gemini Video Understanding](https://ai.google.dev/gemini-api/docs/video-understanding) | 官方公开音频/视觉处理、约 1 FPS、快速动作可能漏细节 | ProviderCapability、采样 profile、rare-event fixture | Gemini 能力不能外推到 Doubao Ark |
| [Incident Lens](https://github.com/rukaiya2000/incident-lens) | 开源示例把视频变成 typed Scene/Event/Person/Object 与 timecode provenance 图 | Typed temporal graph、claim/evidence 关系 | playable timecode 主要是 provenance/UI，不是剪辑安全证明；示例项目不是发布标准 |

### B. 结构化输出、编排和评测

| 项目 | 可吸收能力 | 使用边界 |
|---|---|---|
| [PydanticAI](https://pydantic.dev/docs/ai/core-concepts/output/) | Native/Tool/Prompted output capability、typed validation、有限 retry | 不需要为此替换现有 typed decoder/Command runtime |
| [Instructor](https://python.useinstructor.com/concepts/validation/) | 精确 validation error、bounded reask | 不与 PydanticAI/Guardrails 重叠引入 |
| [Outlines](https://dottxt-ai.github.io/outlines/reference/generation/json/) / [LMQL](https://lmql.ai/docs/latest/language/constraints.html) | 本地可控 logits 的 constrained decoding | Ark 原生 Schema 路径不叠加 |
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence) | checkpoint、durable resume、局部失败恢复 | 现有 Store/Command/Receipt 已承担该职责，不做框架迁移 |
| [DSPy](https://dspy.ai/) | 基于 fixture/metric 的离线 Prompt 优化 | 不允许生产运行时自行改 Prompt |
| [Microsoft GraphRAG](https://microsoft.github.io/graphrag/index/default_dataflow/) | LLM 抽取语义，程序负责数据流、表、图算法和 cache | 不引入其完整 RAG 查询体系 |
| [Graphiti](https://github.com/getzep/graphiti) | temporal knowledge graph、关系有效期和事件记忆 | 数据模型可参考，不能替代本项目 Evidence/Admission |

### C. 视频时序和富语义研究

| 项目/论文 | 可吸收结论 |
|---|---|
| [TRACE](https://arxiv.org/abs/2410.05643) | 支持把事件时间、显著性与文本描述作为结构化事件序列任务交错建模；它不证明可抽取语义 cause/effect 引用图 |
| [TimeExpert](https://openaccess.thecvf.com/content/ICCV2025/papers/Yang_TimeExpert_An_Expert-Guided_Video_LLM_for_Video_Temporal_Grounding_ICCV_2025_paper.pdf) | 单一 Video-LLM 内按任务路由专家，说明 temporal grounding 子任务应有专门指标；不能据此推出模型与确定性程序的 authority separation |
| [UniVTG](https://openaccess.thecvf.com/content/ICCV2023/papers/Lin_UniVTG_Towards_Unified_Video-Language_Temporal_Grounding_ICCV_2023_paper.pdf) | moment retrieval、highlight 和 saliency 有共同但不同的监督形式 |
| [LongVU](https://arxiv.org/abs/2410.17434) | 帧冗余压缩可用于长视频预算研究；query-guided selection 只适合已有明确 query 的下游任务。无 query 的全量 Rich Extractor 不得直接使用，除非 shadow ablation 对 query-free baseline 及 rare-event recall 分 slice 非劣 |
| [Video-RAG](https://arxiv.org/abs/2411.13093) | 多模态辅助 evidence 可提升理解；需与纯视觉观察分开生产和评测 |
| [VTG-LLM](https://arxiv.org/abs/2405.13382) | VLM 时间表达需要专门训练，不能把通用模型粗时间当物理切点 |

上述项目是“设计证据与实验候选”，不是依赖选型。每项吸收都必须回到本项目的 frozen fixture、provider
capability 和独立 Admission；论文指标不能替代真实剧集的 paired evaluation。

## 十五、Linux.do 检索状态

本轮依照 `linuxdo-search` skill 先检查 Linux.do。MCP 返回
`mode=required, available=false, tabs=[]`，进一步搜索返回“未找到配置对应的
`https://linux.do` Chrome 标签页”。这是浏览器传输/登录会话不可用，不是站内无结果。

因此本文件的外部资料来自官方文档、开源仓库和论文，没有把公开搜索结果伪装成 Linux.do
账号可见内容。保持一个已登录的 Linux.do Chrome 标签页后，可再补充社区实战经验。

## 十六、完成标准

Parity 完成不是“新 Schema 字段更多”，而是：

1. V23 每个字段都有明确、可追踪的处置；
2. 所有 `PROGRAM_DERIVE/RENAME_EQUIV` 都有无损或闭包证明；
3. 所有 `REMOVE_AFTER_PROOF` 都有无消费者 telemetry 和质量非劣证据；
4. Rich VLM 的新增字段有具体消费者与指标；
5. V23 与新契约在同一冻结视频上的 semantic coverage、grounding、rare-event recall、Candidate
   recall 和 Stage 1–3 质量达到预设下限；
6. 新契约通过真实单集 shadow 后才能进入 runtime authority，不能原地重解释历史 V23 Artifact。
7. Context-only assistance 永远不能满足 observed Entity/Claim/Event 的 grounding；Event、Candidate 和
   Measurement 对所有引用对象的 existence、closure 与 temporal overlap 均 fail closed。
8. `V23ContractInventory` 与 schema/Prompt/parser exact hashes 一致；任何条件、枚举、cardinality 或
   consumer cutover 漏记都视为 parity 未完成。
9. `video_endpoint_ref` 必须指向带 stream/timebase 的 `FramePtsIndex` member，`audio_endpoint_ref` 必须
   指向带 sample clock 的 `AudioSampleBoundarySet` member；ASR/VAD/Shot/Subtitle/VisualValidity 只能
   作为 proposal/protection/clearance/feasibility evidence。Shot edge 必须吸附到 FramePts member，
   ASR/VAD edge 必须解析到 AudioSampleBoundary member；VLM interval 只能用于 semantic envelope。
