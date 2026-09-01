# 06 LLM 与程序责任边界重设计

## 文档状态

- 状态：目标设计，尚未全部实现。
- 适用范围：`source_prep -> context_prepare -> VLM -> Stage 1 -> Stage 2 -> Stage 3`。
- 不改变的边界：Stage 4 精确 A/V 端点、Recipe、Render、QC 和发布准入均不得由 LLM 决定。
- 当前执行事实仍以 `00`–`05` 和冻结的 runtime authority 为准。

本设计解决的不是“怎样让模型更听话”，也不是单纯缩短 Prompt 或响应，而是在保持 VLM 富语义
产出的前提下缩小模型可以犯错的范围。核心原则是：

> 模型描述发生了什么、为什么重要、哪个已知候选更符合叙事意图；程序决定对象是谁、
> 时间在哪里、引用是否存在、约束是否闭合、能否剪、怎样重跑以及能否发布。

## 一、第一性原理判定规则

一个字段只有同时满足下列条件，才适合直接让 LLM 产生：

1. 不能用稳定规则、查询或可复算算法得到；
2. 本质是语义理解、概括、解释、偏好或创意选择；
3. 出错后可以被证据约束、局部修复或安全拒绝；
4. 不直接授予物理剪辑、授权、持久化或发布权力。

下列任一条件成立时，字段必须由程序产生：

- 是 ID、hash、revision、schema 版本、input binding、Receipt 或 provenance；
- 可由已有对象闭包、策略模板、排序、计数、算术或数据库查询确定；
- 涉及 source grant、授权范围、时间基、帧/采样、精确端点或 A/V pairing；
- 是模型每次都被要求原样回显的输入；
- 在当前 Prompt 中恒为 `[]`、`false`、常量或固定枚举值；
- 错误会让系统错误落库、越权使用素材或产生可发布产物。

还有一类是混合责任：程序先冻结可选集合并分配短别名，模型只在集合内选择，程序再解析
别名并验证闭包。模型不能自由发明引用。

### 语义丰富度不是优化目标的牺牲项

视频剪辑质量高度依赖 VLM 对整集内容的理解，因此不能为了降低 token 而把响应退化为几条摘要。
应删除的是机械冗余和伪证明，不是有下游价值的语义信息：

- 删除：长 ID/hash 回显、固定常量、重复 support、可由闭包派生的字段、授权和物理参数；
- 保留并增强：人物、场景、动作、状态变化、关系、情绪、对白行为、事件、因果假设、冲突、
  铺垫/回收、叙事 Beat、悬念、高光、反应镜头、视听语言和不确定性；
- 每个新增字段必须声明消费者。Stage 1–3、Candidate、ExactSpan 候选生成或调试评测都不消费的
  字段只能进入 shadow 试验，不能无限扩张生产 Schema；
- 优化采用字典序目标：先满足 semantic coverage、grounding、rare-event recall 和下游质量下限，
  在质量非劣前提下再优化 token、延迟和成本；不能用“语义/token”牺牲低频但关键的铺垫、
  微表情、反应镜头和独立 support。

## 二、当前设计的主要浪费与风险

### 1. VLM 富语义输出缺少分区验收

当前一次视频调用输出实体、事实、事件、连续性和候选假设，这个信息范围本身有价值；问题是
所有分区共用一次全有或全无的语义验收，并要求模型维护多组跨数组字符串引用。最近真实失败
中的未知 `f049` 和 candidate measurement 越出候选闭包，说明视频理解可能已经可用，但候选层
的一处引用错误仍会让整次视频调用重跑。

V23 还要求模型输出本轮固定为空的因果数组、固定 continuity 值、固定 schema version 和
本地短 ID。这些不是智能判断，只增加 Schema、输出 token 和失败面。

### 2. Stage 1 输入使用长持久化身份

`allowed_refs` 与窗口对象本身重复；大量引用反复携带窗口 manifest hash。模型真正需要的是
“窗口 2 的事件 4”，不是 64 位 hash。`input_binding_sha256` 也不应由模型回显来证明响应
属于本次请求，应由调用 envelope 和 Attempt 绑定证明。

### 3. Stage 2 让模型重复推导确定性约束

`required_fact_refs` 已可从 selected obligations 精确派生；source allowlist、authorization
purpose、policy-required physical requirements 应来自 SourceGrant 和策略模板。让模型重复
输出这些字段不会增加安全性，只会增加拒绝概率。

模型可以表达“这个故事需要完整对白和反应镜头”，但不能证明这些素材物理上存在。证明责任
属于后续 Media Evidence、CandidateCatalog 和确定性 feasibility evaluator。

### 4. Stage 3 上下文污染最严重

当前构造器会把完整 Source/VLM request record、Stage 1/2 成员、未选 proposal、diagnostics、
policy、UUID、revision 和 hash 带入模型上下文。大部分信息只用于审计或 Kernel 验证，与当前
被选故事的编辑设计无关。

这既浪费 token，也让模型在多个版本和未选方案之间误关联。Stage 3 还让模型回显 story、
proposal、closure、teaser 和物理策略，重复程序已知事实。

### 5. 结构化输出被误当成语义证明

Provider 原生 JSON Schema 能约束字段、类型和枚举，但不能证明引用存在、人物真的出现、时间
有效或剧情解释有视频证据。Schema 是第一道语法门，不是 Admission。

当前还有一项独立 P0：VLM 已采用 Ark 接受的 direct `json_schema` wire shape，而 Stage 1–3
仍走 nested shape，且尚未真实调用验证。责任重构不能掩盖这个 provider 契约问题。

## 三、目标 DAG

```text
source_prep (程序)
  -> context_prepare (程序)
  -> VLM Rich Semantic Extractor (视频模型，分区富语义 Schema)
  -> Sectional Semantic Compiler (程序：分区验收、ID/引用/区间/事实事件图)
  -> Stage 1 Narrative Draft (文本模型)
  -> Narrative Compiler + Coverage (程序)
  -> Candidate Enricher (文本模型，融合 VLM editorial signals 与已准入 Narrative Context)
  -> Candidate Compiler (程序)
  -> Stage 2 Proposal Draft (文本模型)
  -> Feasibility + Portfolio Optimizer (程序)
  -> Stage 3 Editorial Intent Draft (文本模型)
  -> Blueprint Compiler + Semantic Admission (程序)
  -> Media Preflight / Exact Span / Render / QC (程序和专用感知模型)
```

这里的 ASR、VAD、shot detector 或 embedding model 属于“版本化感知算子”，不是自由 LLM：
其输入输出固定，不能自行改变流程或授予发布许可。

## 四、逐阶段字段归属

### 4.1 VLM Rich Semantic Extractor

模型可见输入只包含：

- 当前视频窗口；
- 规范化后的窗口相对时间范围；
- 有预算、无未来剧透的 `WindowContextPack`；
- 本次富语义抽取任务和有界、分区的 response schema。

不进入模型输入：Job/Command/Receipt、BlobRef、provider idempotency key、request hash、完整
SourceManifest、授权信息、外部 API 原始响应、ASR/VAD、物理剪辑策略。

ProviderCapability 必须用 canary 证明实际模态，而不是根据 `input_video` 名称猜测，至少记录
`audio_track_consumed`、`audio_language`、`audio_sampling`、`video_fps/frame_sampling`、
`screen_text/OCR`。未验证音轨能力时，声音、音乐、静默、对白行为均为 `not_evaluated`，不能
标成 observed；可以由独立版本化 ASR/audio classifier 补充，但它们仍是独立 evidence source。

VLM 输出建议分成五个具有明确消费者的语义区，而不是删减成单一 observation 数组：

| 分区 | VLM 应返回的有效信息 | 主要消费者 |
|---|---|---|
| `perception` | evidence atoms、人物/物体/地点、场景、屏幕文字、镜头与已验证模态的声音线索 | 实体解析、Media 候选、Stage 1 |
| `semantic_graph` | typed facts、events、状态变化、人物互动、关系观察、独立 evidence support | Stage 1、Candidate、Coverage |
| `narrative_interpretation` | 情绪变化、人物目标/阻碍、冲突升级、铺垫/回收、反转、开放问题、因果假设 | Stage 1、Stage 2 |
| `continuity` | 窗口首尾状态、跨窗未完事件、人物/场景延续、未知和冲突 | 跨窗口合并、Coverage |
| `editorial_signals` | hook/highlight/reaction/reveal/transition 信号、叙事功能、为什么值得剪、粗时间支持 | Candidate Enricher、Stage 2/3 |

逐字段责任如下：

| 字段/判断 | 责任 | 说明 |
|---|---|---|
| 实体类型、视觉描述、未知人物标签 | VLM | 视频语义观察 |
| 场景地点、环境、时间氛围与场景变化 | VLM | 不等于精确 shot boundary |
| 动作、状态、对白行为、事件摘要、开放问题 | VLM | 对白行为可描述意图，不伪造 ASR 原文 |
| 人物目标、情绪变化、关系变化、冲突与反转 | VLM | 明确区分观察与解释 |
| shot language、反应镜头、音乐/静默等视听信号 | VLM | 为编辑选择提供语义，不产生物理端点 |
| 屏幕文字/标题卡/聊天界面/烧录字幕观察 | VLM 或独立 OCR | 返回文本或 unreadable、区域、类型和粗时间；未经独立验证不是 verified quote |
| 铺垫、回收、悬念、hook/highlight 和叙事功能 | VLM | 保留为 `editorial_signals`，不是最终 CandidateCatalog |
| 场景/事件粗粒度时间桶 | 混合 | 程序提供 `T0..Tn` 有界桶；VLM 只选首尾桶，程序映射到窗口 |
| 人物与 Context Pack 角色的疑似匹配 | 混合 | VLM 选角色短别名；程序保留 `likely_match`，不得升级为事实 |
| `local_entity_id/local_fact_id/local_event_id` | 程序 | 按数组 ordinal 或规范化内容生成 |
| schema version、window binding、global ID、hash | 程序 | 只存在于 envelope/编译产物 |
| typed fact/event 语义、subject/object、temporal mode | VLM | 程序不能从摘要猜出这些关系 |
| facts/events 的 ID、规范化与引用闭包 | 程序 | 由 typed atomic observation 编译 |
| fact support 与 event support | VLM 候选 + 程序校验 | 两者独立；仅注册 containment rule 可继承 |
| continuity/causality 采集状态 | VLM/策略 | 区分 `observed_empty/not_evaluated/indeterminate` |
| 数字 confidence | 不直接采用 | 模型输出 `high/medium/low/indeterminate`；数值只来自离线校准 |
| canonical CandidateCatalog、物理剪辑端点 | 程序/后续节点 | VLM 只提供富语义信号和粗 support |

当前 V23 字段的取舍不是“整组删除”，而是：

| 当前字段 | 判断 | 目标处理 |
|---|---|---|
| `schema_version` | 必要但不需要模型生成 | 移入权威 envelope，decoder 由 request/schema hash 选择 |
| `entities[]` | 核心，保留并增强 | 增加状态变化、场景内身份线索；Context 角色匹配单列为 hypothesis |
| `facts[]` | 核心，保留 | 变成 typed atomic claims，保留 subject/object 与独立 support |
| `events[]` | 核心，保留 | 保留 participants、temporal mode、open question；支持内嵌 claims |
| `cause_event_refs/effect_event_refs` | 有价值，但当前固定为空不合理 | 改为 causality hypothesis + collection state，不能伪装成已证实因果 |
| `window_summary` | 有价值，保留 | VLM 输出 summary 及其短 claim/event ordinal refs；程序只映射和验闭包 |
| `continuity` | 核心，必须增强 | 返回首尾状态和未完事件；区分 unknown/not evaluated/observed empty |
| `candidate_hypotheses[]` | 信息有价值，保留语义 | 重命名/投影为 `editorial_signals`；canonical candidate 在后续归并 |
| `anchor_summary/reason/payoff_or_open_question` | 部分重复 | 保留不重复的“为什么值得剪”和悬念/回收，摘要从 event 投影 |
| `dialogue_excerpt` | 有价值但易被当成逐字事实 | 改为 dialogue act/paraphrase；未经语音证据不得标成 verified quote |
| `editing_modes/narrative_functions/tags` | 有明确下游价值 | 保留注册枚举，允许一项信号携带多种叙事功能 |
| `measurements[]` | 六类信号均有价值 | 保存 ordinal、raw model score 和 evidence；只有 calibrated score 可驱动 predicate |
| 每层 `support` | 防幻觉所必需 | 保留独立 evidence；删除的只是逐字重复复制，不删除 grounding |
| `display_label` | 保留但降权 | 未知人物使用稳定视觉短标签；Context 名称另作 identity hypothesis |
| `dominant_temporal_mode` | 保留 | 由 VLM 选择并引用 supporting events；程序验证枚举与闭包 |

当前 Schema 还缺少但值得通过 fixture 试验后加入的字段包括：`scenes`、人物 state/goal、
relationship observations、emotional turns、setup/payoff links、shot language、audio-visual cues、
screen text、unresolved identities、contradictions 和 context-assisted interpretations。它们必须先有
消费者和评测指标，再从 shadow 字段晋升为生产必选字段。

#### 富输出下的防幻觉分层

防幻觉不靠少返回，而靠把“看到的”和“解释的”分开：

- `perception/semantic_graph.observed`：必须有视频时间桶和可描述的视觉/视听证据；
- `context_assisted_interpretations`：只能解决名字、身份或关系解释，必须引用少量 Context alias，
  不能凭 Context 单独创造视频事件；
- `hypotheses`：人物动机、隐含关系、因果、未来回收等不能直接观察的内容，允许丰富，但必须是
  `likely/possible/indeterminate`，不能进入事实闭包；
- `contradictions/unresolved`：主动输出身份冲突、看不清、听不清、窗口缺上下文等未知，不能用
  空数组代表没有问题。

grounding 必须是一等对象，而不是在每个业务对象里复制一段 support：

```json
{
  "evidence_atoms": [
    {
      "modality": "visual",
      "bucket_refs": ["T3", "T4"],
      "observable_description": "红发学生面向黑衣学生持续发言并做出质问手势",
      "context_assisted": false
    }
  ]
}
```

claim、event、interpretation 和 editorial signal 只引用 evidence atom ordinal。程序能验证 modality、
bucket 和引用闭包，但仍不能证明模型观察绝对真实，因此权威名称应是 `model_observed_claim`，
不能叫 `verified_fact`。独立 detector/ASR/OCR 可以生成另一类 evidence atom，并保持 producer 分离。

为避免来源字段膨胀，provenance 放在语义对象层而不是每个 scalar 字段：同一 event 的 summary、
participants 和 narrative interpretation 不重复抄来源。只有 context-assisted 或 hypothesis 对象
额外携带 `context_refs/evidence_refs`。程序根据 section 和对象类别派生默认 evidence class。

进入 Stage 1 的事实只能来自已验收 observed claims；interpretation/hypothesis 可以作为候选语义
信号，但不能未经显式规则升级为 fact。这样可以让 VLM 大胆返回有用理解，同时不给推测授予
事实或物理剪辑权力。

五个分区必须有唯一 canonical owner，其他分区只能引用并追加解释，不能复制同义事实：

| 概念 | canonical owner |
|---|---|
| evidence atom、entity、scene、screen text、可观察视听 cue | `perception` |
| typed claim/event、observed state/relationship change、event open question | `semantic_graph` |
| motive、tension、causality hypothesis、setup/payoff interpretation | `narrative_interpretation` |
| boundary snapshot、continuation/contradiction hypothesis | `continuity` |
| hook/highlight/reaction/reveal/transition 的编辑价值 | `editorial_signals` |

例如 `open_question` 由 event 所有，narrative 区只能引用该 event 解释其 tension；editorial signal
只能引用 event/interpretation，不再改写事实摘要。

每个生产字段还必须在 `FieldConsumerRegistry` 登记：具体 consumer artifact/函数/predicate、
是否 hard dependency、缺失或 indeterminate 行为、eval metric、retention/provenance。仅用于 debug
或评测的字段进入 shadow sidecar，不能借“调试有用”成为生产必填字段；上线后用读取 telemetry
验证字段确实被消费。

建议模型输出紧凑、typed atomic observation，不生成字符串 ID。每个 event observation 内嵌
其 claims，避免让程序从自然语言 summary 反推 fact kind、subject/object 或 open question。
实体和观察仍可在同一响应内通过整数 ordinal 关联，但该响应必须作为一个原子对象校验和修复。
例如：

```json
{
  "perception": {
    "evidence_atoms": [
      {
        "modality": "visual",
        "bucket_refs": ["T3", "T4"],
        "observable_description": "红发学生面向黑衣学生持续发言并做出质问手势"
      }
    ],
    "entities": [
      {"kind": "person", "visual_description": "红发女学生", "evidence_atom_indices": [0]},
      {"kind": "person", "visual_description": "黑衣男学生", "evidence_atom_indices": [0]}
    ],
    "scenes": [
      {"setting": "学院公共区域", "coarse_interval": {"start_bucket": "T2", "end_bucket": "T6"}}
    ]
  },
  "semantic_graph": {
    "events": [
      {
        "kind": "confrontation",
        "summary": "一名学生当众质问另一名学生",
        "participant_indices": [0, 1],
        "coarse_interval": {"start_bucket": "T3", "end_bucket": "T5"},
        "temporal_mode": "current",
        "open_question": "被质问者是否会公开回应",
        "evidence_atom_indices": [0],
        "claims": [
          {
            "kind": "interaction",
            "subject_index": 0,
            "object_index": 1,
            "summary": "红发学生公开质问黑衣学生",
            "coarse_interval": {"start_bucket": "T3", "end_bucket": "T4"},
            "evidence_atom_indices": [0]
          }
        ],
        "certainty": "high"
      }
    ]
  },
  "narrative_interpretation": {
    "items": [
      {
        "interpretation_kind": "emotional_turn",
        "summary": "公开冲突使紧张关系升级",
        "evidence_event_indices": [0],
        "evidence_claim_indices": [0],
        "certainty": "high",
        "collection_state": "observed"
      }
    ]
  },
  "continuity": {"collection_state": "indeterminate"},
  "editorial_signals": [
    {
      "kind": "hook",
      "anchor_event_index": 0,
      "supporting_event_indices": [],
      "context_event_indices": [],
      "payoff_event_indices": [],
      "narrative_function": "escalation",
      "reason": "公开冲突快速建立人物矛盾",
      "evidence_atom_indices": [0]
    }
  ]
}
```

`participant_indices` 是本响应内的整数 ordinal；程序检查范围后再生成 durable ref。实体数组
和观察数组的顺序冻结为该响应的 canonical ordering。局部 repair 不得单独插入、删除或重排
任一数组；若必须改变 ordinal 集合，只能生成完整新响应并重跑全部引用校验。无法确认时用空
列表或 `indeterminate`，不得发明 ID。

`editorial_signals` 必须无损保留 V23 已有的 anchor/support/context/payoff 多事件角色和自己的
evidence closure。Candidate Enricher 只负责跨窗口归并、结合 Stage 1 叙事上下文排序和补充，
不能根据摘要重新猜测被 VLM 删除的窗口内角色关系。

每项 salience/strength 同时保存：用于生产语义的 `ordinal_assessment`、仅作 shadow/校准训练的
`raw_model_score`，以及只有绑定注册 CalibrationProfile 后才允许 predicate 使用的
`calibrated_score`。未校准 raw score 不获得 Admission 权力。

`T0..Tn` 由程序按实际视频投喂/抽帧分辨率生成并冻结，模型不直接声称毫秒精度。Compiled
Observation 同时记录桶范围、桶分辨率和误差界；粗时间 IoU 按该分辨率计算，绝不能与 Stage 4
ExactSpan 的 tick 精度混为一谈。

continuity 和 causality 不能靠程序填 `false/[]`。若本轮 Prompt 未采集该能力，编译产物必须
写 `not_evaluated`；模型看过但无法判断时写 `indeterminate`；只有明确执行了相应任务且没有
观察到关系时才是 `observed_empty`。任何依赖这些信息的 Admission predicate 遇到
`not_evaluated/indeterminate` 都必须按策略 quarantine/indeterminate，不能当作 pass。

continuity 至少包含进入/离开两个 boundary snapshot：direction、entity/scene refs、未完成动作
或对白行为、人物状态、overlap bucket refs、continuation hypothesis、contradiction refs 和
collection state。跨窗 merger 只能在 overlap evidence 和有限实体候选集合内判断；VLM 的连续性
叙述本身不能直接升级为 durable relation。

从数字 confidence 迁移到 ordinal certainty 也必须版本化。注册校准 profile 可将某模型/Prompt
版本的 ordinal 映射为有误差界的数值；在 profile 可用前，所有依赖现有数值阈值的 predicate
均返回 `indeterminate`，禁止把 `high/medium/low` 随意硬编码成 `0.9/0.6/0.3`。

#### 分区验收，而不是减少信息

一次 VLM 调用仍可以返回完整五区信息，但验收必须经过三层门：

1. 完整 bytes、finish reason 和 root JSON decode；
2. 每区 structural schema；
3. 区内及跨区 business validation。

只有第一层完整通过，才允许分区复用。`root_parse_failed`、截断、refusal 或无法定位完整 section
raw bytes 时，所有分区都不得声称 `ready`。每区分别设置自适应 byte/token/item budget，并用
fixture 覆盖尾部分区截断；事件密度或人物数增加时可提高预算或分调用，未返回不能解释为不存在。

分区不是固定的整区依赖链，而是每个 item 显式绑定 dependency refs：视听 reaction signal 可只
依赖 perception，叙事 hook/payoff 依赖 event/interpretation，continuity 的 scene/entity 与
unfinished-event 分量分别验收。一个无关 fact 错误不能 taint 独立 reaction shot。

- 原始响应始终作为一个不可变 Blob 保存，不能丢掉任何模型信息；
- 每个分区产生独立 `SectionValidationResult`：`ready/indeterminate/blocked`、collection state、
  completeness/cardinality、error severity/path、validator version、dependency section hashes、
  canonical section hash 和允许的 consumer；
- `ready` 要求该区所有 required 字段、引用和依赖闭合；`indeterminate` 表示结构有效但必需观察
  未采集或证据不足；`blocked` 表示结构、引用、owner 或依赖冲突；
- dependency `blocked` 时依赖 item 必须 blocked；dependency `indeterminate` 时依赖 item 至多
  indeterminate；不依赖该对象的 item 可以独立 ready；
- `editorial_signals` 引用错误时，保留同一 Attempt 已通过的其他分区，随后由 Candidate Enricher
  基于这些数据重建候选；
- `semantic_graph` 失败时可以保留 perception，但 Stage 1 不准继续，直到必需 graph items 闭合；
- “可复用部分语义产物”不等于允许 partial Story/Recipe/publish，发布链仍按完整故事 all-or-nothing。

跨 Attempt 禁止随意拼装新旧 section：

- full VLM rerun 产生新的原子响应，所有 section 一起重新编译；旧 ready section 仅供审计/对照；
- failed-section rerun 必须只请求目标 section，并使用旧已准入 section 编译出的冻结 AliasCatalog；
  新 SectionArtifact 绑定 `base_section_hash` 和全部 `dependency_section_hashes`；
- 只有依赖 hash 完全一致，或 compiler 证明 canonical dependency section 完全相等，才允许组合；
  否则整体重编译依赖闭包，禁止把新 ordinal 指向旧响应中同位置的另一人物。

这样既保留 VLM 尽可能丰富的首次理解，也避免某个候选引用错误导致所有有效视觉语义被一起
淘汰和重新计费。

### 4.2 Candidate Enricher

候选生成应成为 Stage 1 Admission 之后的独立、可缓存、可局部重跑文本语义调用。仓库已有
`candidate_enrichment_draft/compiler` 的短别名研究方向，但它仍是 future lifecycle contract，
依赖已准入 `Stage1Values`，且字段与本目标并不相同。必须先版本化改造其 Schema、provenance
和运行生命周期，再接线；不能把现有研究代码直接提升为 runtime authority。

程序输入给模型：

- 当前窗口或相邻窗口已验收的 event/fact 与 `editorial_signals`；
- Stage 1 已准入的 beat、thread、obligation 和跨窗口上下文；
- `E1/F1/...` 短别名及一句摘要；
- 注册的 `candidate_kind`、`narrative_function`、`editing_mode`；
- 每类候选数量和文本预算。

模型只输出对 VLM 原始信号的跨窗口归并、补全和排序：

- anchor/support/context/payoff 的短别名选择；
- hook/highlight 类型；
- 为什么值得讲、开放问题和简短叙事功能；
- 序数级语义评价，如 `low/medium/high`。

程序负责：候选 ID、support union、evidence closure、别名解析、重复合并、数值评分校准、
CandidateCatalog 和 capability 派生。候选 schema 失败只重跑该文本节点，不重传视频。

### 4.3 Stage 1 Narrative Draft

| 字段/判断 | 责任 |
|---|---|
| beat 分组、摘要、叙事 phase | LLM |
| story thread 标题、premise | LLM |
| 语义 obligation 描述与成功标准 | LLM |
| 跨窗口实体是否可能同一人 | LLM 在候选集合内判断 |
| `beat_id/obligation_id/thread_id/merge_id` | 程序 |
| input binding、schema version、Graph member ID | 程序 |
| event/fact/entity 引用存在性和 owner | 程序 |
| 依赖图、覆盖账本、冲突诊断、Admission | 程序 |

输入必须改为别名化的语义投影，不再传 `allowed_refs` 重复全集，也不让每个引用携带完整
manifest hash。模型看到 `W2.E4`，Kernel 在 envelope 内维护 `W2.E4 -> durable ref` 冻结表。

“保留所有原始观察”与“要求每个原始 fact 都成为故事内容”必须区分。目标版本应引入两个
互不替代的账本：

- `ObservationCompletenessLedger`：覆盖全部 observations/facts/events，保留 owner、来源、
  confidence/taint、冲突和未分配原因；
- `NarrativeSelectionClosure`：只要求被选择的 claim、required obligation 及其依赖事实形成
  故事闭包。

当前 runtime authority 使用 `strict_global`，Coverage/Admission 和 Candidate compiler 都依赖
完整 Fact/Event universe。上述拆分是版本化安全语义变更，不能靠瘦 Prompt 偷换。迁移必须同步
Coverage policy、ledger、dependency proof、Admission、Candidate compiler，并证明未选的冲突、
低置信或错误实体合并不能污染选中故事；新策略上线前继续执行现有 `strict_global`。

实体合并宜单独执行：程序先用同集、相邻窗口、视觉描述和 Context Pack 映射构造有限 pair
候选，LLM 只返回 `same/different/indeterminate` 和极短理由。`indeterminate` 不自动合并。

### 4.4 Stage 2 Proposal Draft

| 字段/判断 | 责任 |
|---|---|
| title、narrative claim、audience hook | LLM |
| thread/obligation/candidate 的选择 | LLM 在别名集合内选择 |
| genre、teaser 和叙事风格 | LLM 选择注册类别并补一句意图 |
| 时长档位 | LLM 可选 `short/standard/long` |
| 精确 `min/max seconds` | 程序按档位与 JobPolicy 展开 |
| required facts | 程序由 selected obligations 闭包派生 |
| source allow/deny、authorization purpose | 程序由 SourceGrant 投影 |
| physical requirements | 程序由 editing profile/policy 模板投影 |
| minimum usable seconds | 程序按模板与时长策略计算；模型只能表达相对重要性 |
| proposal/requirement ID、portfolio selection | 程序 |
| 结构闭包、硬约束与 physical feasibility | 程序/版本化感知证据 |
| semantic desirability/diversity signal | LLM、版本化 embedding/分类器或离线人工标注 |
| 最终 portfolio optimizer 与 Admission | 程序对具名、版本化 predicate/score 的确定性计算 |

模型不能判断“对白物理完整”“字幕已清除”或“有 12 秒安全素材”，因为 Stage 2 没有对应
Media Evidence。它只能声明编辑意图，不能自证素材满足意图。

删除模型的数值时长字段前，必须先交付版本化 DurationCompiler：注册
`short/standard/long -> min/max`、pacing 分配、`tight -> max_gap`、舍入、总时长守恒、
infeasible/indeterminate 行为和 strategy hash。新 compiler 通过 fixture 前继续保留当前数值字段，
不能用未注册常量替代模型输出。

### 4.5 Stage 3 Editorial Intent Draft

模型输入只包含当前已选 proposal 的精简 Context Manifest：

- proposal 的语义目标；
- 必须覆盖的 obligation；
- 可选 candidate/event 的短别名、摘要和可用性等级；
- 注册的 narrative role/function、pacing 档位；
- 预算范围。

不得输入完整 VLM request record、未选 proposal、Receipt/UUID/hash/revision、全部 diagnostics、
完整 predecessor pool 或原始 policy JSON。

| 字段/判断 | 责任 |
|---|---|
| beat 的顺序、叙事角色、功能、摘要 | LLM |
| obligation/candidate 分配和备选语义组合 | LLM 在别名集合内选择 |
| pacing、continuity 优先级、teaser 表现意图 | LLM |
| story/proposal ID、输入绑定 | 程序 |
| required fact/material closure | 程序 |
| physical requirement 和 source constraint | 程序从 Stage 2 复制 |
| beat/evidence ID、ordinal | 程序 |
| story/beat 精确秒数、tick、time_base | 程序从节奏档位和总预算编译 |
| `precedes` | 通常由 beats 数组顺序派生 |
| `adjacent/max_gap` | 模型可表达语义意图；程序编译数值约束 |
| semantic feasibility、Admission | 程序 |

模型不再输出 rational `tick/time_base`。如果它表达“反应镜头紧接揭示镜头”，程序将
`tight` 等注册间隔档位编译为具体约束。

### 4.6 Stage 4、Render、QC 和发布

这些阶段不使用自由 LLM 决定：

- source tick、frame/sample endpoint；
- ASR/VAD/字幕保护区；
- A/V pairing、canonical minimum；
- Recipe、FFmpeg 参数、媒体 QC；
- copyright/source restriction、publish decision。

LLM critic 可以在 shadow/offline 环境评价叙事流畅度，但不能成为唯一准入器，也不能覆盖
确定性失败。

## 五、请求与输出契约

### 5.1 模型可见对象与权威 envelope 分离

```json
{
  "model_visible": {
    "task": "select narrative beats",
    "alias_catalog": [{"alias": "E1", "summary": "..."}],
    "context": {},
    "constraints": {}
  },
  "authority_envelope": {
    "schema_version": "...",
    "input_binding_sha256": "...",
    "alias_catalog_sha256": "...",
    "prompt_version": "...",
    "schema_sha256": "...",
    "provider_request_identity": "..."
  }
}
```

第二部分写入 Attempt/Blob/Receipt，不要求模型读取或回显。删除响应内 binding 前，新 compiler
必须只能读取 exact responded/reconciled Attempt 的 raw Blob，并闭合 request payload hash、
schema hash、alias catalog hash、provider request ID 和 decoder version；禁止调用者裸传任意
`raw_response: bytes`。做到以后，“模型正确抄回 hash”才不再被当成绑定证明。

### 5.2 结构化输出模式

按 provider capability 选择一种模式：

1. 原生 strict JSON Schema；
2. tool/function output；
3. prompted JSON + 本地严格 decoder；这只是生成后验证，不是 constrained decoding。

优先级由经过 fixture 验证的 provider capability registry 决定，不可因为兼容 API 自称支持就
自动启用。Registry 至少分别记录 `generation_constrained`、`schema_subset`、
`posthoc_validation_only` 和 refusal/stream 支持。Ark 当前应继续使用已验证的 direct response
format，并先修复、实测 Stage 1–3 的 wire shape。

无论哪种模式，响应仍必须经过版本化 typed decoder/validator 和业务 validator；是否采用
Pydantic 是实现选择。暂不引入 Instructor、PydanticAI、Guardrails 三套重叠框架；可吸收其
“精确错误路径 + 有预算重试”机制。

### 5.3 Schema 设计准则

- 每次调用只有一个语义职责；
- 模型 schema 不含 hash、UUID、revision、Receipt 和常量回显；
- 层级尽量不超过四层；
- 动态引用优先短 alias/ordinal，不把完整 durable ref 做成巨大 enum；冻结 alias 集较小时直接
  生成动态 enum/Literal，超过策略阈值才退回外部 membership validator；
- schema hash 纳入 request/cache key；
- 跨记录不变量由程序验证，不尝试全部编码进 JSON Schema；
- provider schema 上限是 ceiling，不是目标尺寸；
- 每次发布 prompt/schema 前记录实际输入 token、输出 token 和首次 schema 编译延迟。

## 六、失败分类与局部重跑

| 失败类型 | 处理 |
|---|---|
| 429/5xx/连接中断 | 相同 request identity，指数退避+jitter；不改 Prompt |
| provider 结果未知 | reconcile provider response；不能盲目再付费调用 |
| `max_output_tokens` 截断 | 缩小职责、分块或提高有界预算；不把半份 JSON 当普通 repair |
| safety refusal/content filter | 进入显式 policy 分支或终止，不伪装为 Schema 错误 |
| native strict 仍返回非法结构 | provider capability incident；降级已验证模式或切换 provider |
| prompted/tool 模式 JSON/Schema 错误 | 至多一次精确 reask |
| 文本阶段未知 alias/引用越界 | 只修失败对象或重跑当前文本节点，不重跑视频 |
| VLM 人物/事实/时间/support 错误 | 携带同一视频重新调用 VLM，或失败/quarantine；禁止 text-only repair |
| 语义证据不足 | 输出 `indeterminate`，不以重复调用伪造确定性 |
| source/hash/时间越界 | 确定性失败，回到 source/window/evidence 阶段 |
| 同一语义错误重复出现 | 视为 Prompt/Schema 缺陷，进入 fixture 与新版本；停止盲重试 |

纯 canonical JSON/已注册排序问题由程序做不改变语义的规范化。文本阶段的 repair 输入只包含：
原响应、精确 JSON path、actual、expected 和仍有效的 alias catalog；输出采用封闭 typed patch，
而不是任意 JSON Patch。策略冻结允许修改的 path 和对象类型。Patch 必须绑定
`base_response_hash`、`validator_error_set_hash`、`alias_catalog_hash`，merge 后重新运行完整
Schema 和所有跨字段 validator。涉及 ordinal 数组结构或无法安全 merge 时，放弃 patch，生成
完整新响应。

VLM 的事实、人物、时间和 support 错误不适用 text-only patch：修复必须重新携带同一视频和冻结
上下文调用视频模型，或直接失败/quarantine。默认一次针对性 repair；仍失败则 fallback、
quarantine 或显式终止。禁止用业务默认值修成成功。

三类调用身份必须分开：

- transport retry：保持同一 immutable semantic request/request hash，只新增 Attempt ordinal；
- semantic repair：新 `RepairCommand` 和新 provider request hash，并绑定 parent Attempt、原响应
  hash、validator error set 和 alias catalog；
- model/provider fallback：新 model/provider profile 和新 request hash，作为派生 Command 记录，
  不能伪装成原请求的普通 Attempt。

单集、单窗口和单阶段重跑必须复用冻结输入 Artifact 和 Context Pack。只有上游 identity 或该
节点 prompt/schema/policy、repair payload 或 model/provider profile 变化时才产生新派生 Command；
候选失败不能使观察提取失效。

模型可见的精简 projection 不能替代 Kernel 的完整 predecessor 重建。独立 evaluator 仍必须从
committed predecessor pool 重算 alias projection hash、taint、source owner、physical requirement
和所有 Admission predicate，不能只相信发给模型的 Context Manifest。

## 七、评测与 Prompt 优化

先有评测集，再改 Prompt。禁止以“真实流程终于跑通一次”代替质量验证。

| 层级 | 指标 |
|---|---|
| L0 结构 | first-pass schema pass、截断率、repair 次数、response bytes |
| L1 引用/时间 | alias 解析率、引用闭包率、粗时间 IoU、support temporal localization、modality attribution accuracy |
| L2 语义 | entity/event precision-recall、跨窗口一致性、候选召回与排序、abstention rate |
| L3 编译 | Narrative/Portfolio/Blueprint compile pass、coverage、feasibility |
| L4 下游 | ExactSpan/Render/QC pass、人工偏好、返工率 |
| L5 运营 | p50/p95、token/cost、cache hit、retry/fallback 分布 |

每个 Prompt/Schema 版本使用同一冻结 fixture corpus 做对照。选择性预测必须报告 coverage、
risk-at-coverage 和 certainty calibration，防止模型用大量 `indeterminate` 换取虚高 precision。
fixture 至少覆盖 Context Pack 未来信息泄漏、alias 顺序扰动、窗口边界/重叠、相似人物、字幕
密集/无对白、长尾剧种、repair 后质量及不同 provider/model slice；报告 cost-quality frontier，
关键对比给出 bootstrap confidence interval。entity/event matching、candidate top-k 和标注者一致性
必须有版本化定义。

EvidenceAtom 还必须评测 claim -> atom 的真实支持关系，而不仅是引用存在：报告 evidence-support
precision/recall 或盲人工 faithfulness、atom modality 归因准确率、支持片段时间覆盖，并设置生产
晋升下限。合法但与 claim 无关的 atom 不能被算作 grounded。

跨 Attempt 组合必须有对抗 fixture：full rerun 禁止复用旧 section、dependency hash mismatch
必须拒绝、canonical-equality 允许路径、entity ordinal 重排，以及 blocked/indeterminate taint
传播矩阵。设计上的 hash 约束必须由这些回归用例变成实现门禁。

不能预设“一次五区 Rich Extractor”一定优于多阶段。必须对照至少三种方案：单次五区、视频模型
只做 perception+graph 后由文本模型 enrichment、按 provider capability 拆成两次视频任务。比较
observed claim precision、rare-event recall、跨区矛盾率、section/root 截断率、下游质量、成本和
延迟；解释性/编辑任务不得反向诱导 perception 把推测写成观察。

LLM judge 只能补充主观指标，不能与生成模型共同构成唯一裁判。具备稳定 gold/dev set 后，
可以在离线分支使用 DSPy 一类工具优化 Prompt；生产运行时不得自行改 Prompt。

统一 trace 至少记录：

```text
job_id, stage, episode/window, attempt, parent_attempt_id,
model/version, provider_request_identity, response_id,
prompt_version, schema_hash, alias_catalog_hash, context_pack_hash,
input/output_artifact_hash, validator_version,
finish_reason, validator_errors, retry_class, repair_action, degradation_path,
input/output/cached/reasoning tokens, latency, cache_hit
```

原始请求和响应继续不可变保存并脱敏。文件 debug 是诊断镜像；数据库 Attempt/Blob/Receipt 才是
恢复权威。`error.json` 后续应保存脱敏后的稳定 failure code 和精确 validator path，不能只有
异常类名。

## 八、外部方案的吸收边界

- Microsoft GraphRAG：吸收“LLM 做实体/关系/claim 抽取，程序做 workflow、表、community
  detection 和 cache”的职责分层；不引入其完整 RAG 查询体系。
- PydanticAI / Instructor：吸收 provider capability、结构验证、错误路径和有限 output retry；
  不同时叠加多个验证框架。
- LangGraph：吸收 checkpoint、幂等恢复和失败节点局部重跑语义；现有 Store/Command/Receipt 已
  承担该职责，不为此重写编排器。
- Outlines/LMQL：仅在自托管且可控制 logits 的本地模型使用约束解码；Ark 原生 Schema 路径不
  再叠加。
- DSPy：只用于离线 eval 驱动的 Prompt 优化，不进入生产控制面。
- Video temporal grounding：吸收“语义、显著性、时间边界由不同任务/算子处理”的分工；VLM
  给语义候选，专用证据与 ExactSpan Compiler 决定物理端点。

Video-RAG 一类工作会把 ASR/OCR 等辅助文本送入视频模型。当前生产设计仍选择不把 ASR/VAD
送入 VLM Prompt，以保持“视觉语义观察”和“物理音频证据”的独立性；这是一项工程取舍，不是
断言辅助文本没有语义价值。应在离线 fixture 上做三臂 ablation：`video-only`、
`video+ContextPack`、`video+ContextPack+去时间戳 ASR 语义文本`。即使第三臂语义指标更好，
ASR timestamp 也不得升级为 VLM 的物理剪辑证明，是否进入生产需另立版本和污染测试。

### 同类系统和使用经验

公开实践支持“富信息、多分区、独立 evidence producer”，而不是把视频压成一条摘要：

- Azure AI Video Indexer 运行多种模型并输出分类 JSON，覆盖 faces、objects、observed people、
  OCR、scenes/shots/keyframes、audio effects、topics 和 emotions；每类 insight 都带时间实例。
  可吸收：富语义按 canonical category 存储、每类保留时间 evidence。不能照搬：其很多 emotion、
  topic 来自 transcript/OCR，不能伪装成豆包纯视频视觉结论。
- Azure 的 Prompt Content 先按 scene 和其他 insight 切成 coherent sections，再把已索引的 objects、
  faces、OCR、audio effects 等转成 prompt-ready content，支持后续 LLM 分析而无需重新索引视频。
  这与“VLM/感知产物持久化，Stage 1–3 只读冻结投影”一致。
- Google Video Intelligence 把 label、shot、face、speech、text、object、logo、person 分成显式
  Feature，并允许 frame/shot/segment 粒度与 model version/threshold 配置。可吸收：不同 modality
  和粒度由版本化算子承担，不让一个自由 Prompt 同时自证全部能力。
- TwelveLabs 支持复用已上传 Asset 执行多个 analysis task，也支持 structured JSON、可定制
  timestamped segment 类型和字段，以及 chapter/highlight 等任务。可吸收：同一媒体 Artifact 上
  运行可独立缓存、重跑的语义任务；不能据此假设 Ark 也消费音轨或具有同样 timestamp 精度。
- Gemini 官方明确其视频路径同时处理音频和视觉，但也公开默认约 1 FPS，快速动作可能漏细节。
  这个经验说明 provider 的采样与模态能力必须进入 Profile 和误差预算，不能仅由 API 名称推断。
- TimeExpert、TRACE 等 temporal grounding 工作把 timestamp、saliency、description/causal event
  建模视为不同任务或专家。可吸收：VLM 富语义信号可以很多，但物理时间和显著性校准仍需独立
  evaluator/专用模型。
- 开源 Incident Lens 展示了一个相近工程模式：视频理解后生成 typed Scene/Event/Person/Object、
  temporal edges 和可回放 timecode provenance，再落入知识图。它是可参考实现，不是经过生产
  认证的标准，适合借数据模型，不适合直接作为发布门禁。

这些实践共同指向：保留丰富语义是正确的，但必须按 modality、canonical owner、时间 evidence、
producer version 和 consumer 分区；“一个大 Prompt 返回一个全能对象”不是唯一方案，也不能默认
质量最好。

参考：

- [PydanticAI structured output](https://pydantic.dev/docs/ai/core-concepts/output/)
- [Instructor validation](https://python.useinstructor.com/concepts/validation/)
- [Instructor retry](https://python.useinstructor.com/concepts/retrying/)
- [Microsoft GraphRAG dataflow](https://microsoft.github.io/graphrag/index/default_dataflow/)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Outlines JSON generation](https://dottxt-ai.github.io/outlines/reference/generation/json/)
- [DSPy](https://dspy.ai/)
- [UniVTG](https://openaccess.thecvf.com/content/ICCV2023/papers/Lin_UniVTG_Towards_Unified_Video-Language_Temporal_Grounding_ICCV_2023_paper.pdf)
- [Video-RAG](https://arxiv.org/abs/2411.13093)
- [LongVU](https://arxiv.org/abs/2410.17434)
- [Azure AI Video Indexer insights](https://learn.microsoft.com/en-us/azure/azure-video-indexer/insights-overview)
- [Azure Video Indexer Prompt Content](https://learn.microsoft.com/en-us/azure/azure-video-indexer/prompt-overview)
- [Google Video Intelligence annotate features](https://docs.cloud.google.com/video-intelligence/docs/reference/rest/v1/videos/annotate)
- [TwelveLabs analyze videos](https://docs.twelvelabs.io/docs/guides/analyze-videos)
- [Gemini video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- [TRACE](https://arxiv.org/abs/2410.05643)
- [TimeExpert](https://openaccess.thecvf.com/content/ICCV2025/papers/Yang_TimeExpert_An_Expert-Guided_Video_LLM_for_Video_Temporal_Grounding_ICCV_2025_paper.pdf)
- [Incident Lens](https://github.com/rukaiya2000/incident-lens)

本次 Linux.do 检索未取得内容：浏览器中没有已登录并配置的 `https://linux.do` 标签页。该失败
不应被误报为“Linux.do 没有相关方案”；上面的结论来自当前代码审计和公开官方/论文资料。

## 九、迁移顺序

### P0：先消除真实运行阻断和无意义全量重跑

1. 冻结当前 V23 的单集 request/schema/raw-response/parser fixture 和成本 baseline；
2. 建立 V23 全字段 parity matrix：原样保留、等价重命名、程序无损派生或有指标证明删除；
3. 对 Ark direct `json_schema`、视觉/音频/screen-text 实际 capability 做独立 canary，再统一并真实
   验证 Stage 1–3；
4. 为现有 validator 输出稳定 error code + JSON path；
5. 版本化改造 Candidate Enrichment，并补齐 Command/request/Attempt/Receipt、provider profile、
   runtime authority、pipeline ordinal、registry、reconcile/resume、Artifact reader、Stage 2 binding、
   recompute fencing 和 debug stage；
6. 以 shadow lifecycle 对照 V23 candidate，验证信息覆盖、分区验收和局部重跑后，才发布新的
   rich-sectioned VLM contract；
7. 保留旧 V23 作为 shadow 对照，不直接覆盖已持久化产物。

### P1：重构责任、投影和富语义分区

1. 引入每请求冻结的 `AliasCatalog`；
2. 引入 first-class `EvidenceAtom`、`FieldConsumerRegistry` 和带 dependency hashes 的
   `SectionValidationResult/SectionArtifact`；
3. 交付 exact Attempt -> compiler binding 后，再删除模型输出中的 ID/binding；常量空值改为显式
   collection state，不得补成否定事实；
4. Stage 1 输入改为别名化窗口语义投影，同时版本化拆分 Observation ledger 和 Narrative closure；
5. 同步迁移 Coverage policy/ledger/dependency proof/Admission/Candidate compiler；
6. Stage 2 移除 SourceGrant 明细、授权和物理模板输出；
7. DurationCompiler 通过 fixture 后，再移除 Stage 2/3 数值时长和 tick 输出；
8. Stage 3 只移除模型可见 projection 中的审计噪声和未选对象，保留当前故事所需的丰富语义；
   Kernel evaluator 继续用完整 predecessor pool 独立重建；
9. 为每个新 schema 设置独立版本和兼容 decoder，不修改历史响应解释。

### P2：局部 repair、评测和成本门禁

1. 失败对象级 repair 和累计 token/attempt budget；
2. 分层 fixture metrics 与版本对比报告；
3. schema/prompt/context/token 预算器；
4. 单集、单窗口、单节点 resume 的 HTTP 与 Receipt 验证；
5. 真实单集依次跑通 VLM、Candidate、Stage 1–3 后再扩大批量。

### P3：离线优化

在稳定数据集上评估 DSPy、模型切换、机械冗余压缩和候选排序；只有语义覆盖与质量不下降、
成本收益成立且所有确定性门禁通过，才把新版本写入 runtime authority。框架级迁移不是当前
前置条件。

## 十、验收判断

本设计完成不能只看“LLM 返回合法 JSON”。至少必须证明：

1. 模型请求中不再出现无业务意义的 UUID/hash/Receipt/未选对象；
2. 模型输出中不存在程序可派生的 ID、binding、授权和物理证明；
3. perception、semantic graph、narrative、continuity、editorial signals 都有明确字段、消费者和
   fixture 覆盖，不能以“省 token”为由删除有效信息；
4. 候选或 Stage 1–3 失败不会触发已成功 VLM 分区的无意义重新计费调用；
5. 同一冻结输入、Prompt、Schema 和 provider profile 可得到可追踪 Attempt；
6. unknown reference、越界时间和物理证据缺失仍 fail-closed；
7. 单集真实运行通过后，扩批只增加数据量，不改变契约；
8. Stage 3 成功仍不等于 Render 或 publish allow。
