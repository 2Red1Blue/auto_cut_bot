# 06 LLM 与程序责任边界重设计

## 文档状态

- 状态：目标设计，尚未全部实现。
- 适用范围：`source_prep -> context_prepare -> VLM -> Stage 1 -> Stage 2 -> Stage 3`。
- 不改变的边界：Stage 4 精确 A/V 端点、Recipe、Render、QC 和发布准入均不得由 LLM 决定。
- 当前执行事实仍以 `00`–`05` 和冻结的 runtime authority 为准。

本设计解决的不是“怎样让模型更听话”，而是缩小模型可以犯错的范围。核心原则是：

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

## 二、当前设计的主要浪费与风险

### 1. VLM 同时承担观察图和候选图

当前一次视频调用同时输出实体、事实、事件、连续性和候选假设，还要求模型维护多组跨数组
引用。最近真实失败中的未知 `f049` 和 candidate measurement 越出候选闭包，都是这种耦合
的直接结果：视频理解可能已经可用，但候选层的一处引用错误会让整次视频调用重跑。

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
  -> VLM Observation Extractor (视频模型，小型语义 Schema)
  -> Observation Compiler (程序：ID/引用/区间/事实事件图)
  -> Stage 1 Narrative Draft (文本模型)
  -> Narrative Compiler + Coverage (程序)
  -> Candidate Enricher (文本模型，使用已准入 Narrative Context)
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

### 4.1 VLM Observation Extractor

模型可见输入只包含：

- 当前视频窗口；
- 规范化后的窗口相对时间范围；
- 有预算、无未来剧透的 `WindowContextPack`；
- 本次观察任务和小型 response schema。

不进入模型输入：Job/Command/Receipt、BlobRef、provider idempotency key、request hash、完整
SourceManifest、授权信息、外部 API 原始响应、ASR/VAD、物理剪辑策略。

| 字段/判断 | 责任 | 说明 |
|---|---|---|
| 实体类型、视觉描述、未知人物标签 | VLM | 视频语义观察 |
| 动作、状态、对话行为、事件摘要、开放问题 | VLM | 允许不确定，不要求猜名字 |
| 场景/事件粗粒度时间桶 | 混合 | 程序提供 `T0..Tn` 有界桶；VLM 只选首尾桶，程序映射到窗口 |
| 人物与 Context Pack 角色的疑似匹配 | 混合 | VLM 选角色短别名；程序保留 `likely_match`，不得升级为事实 |
| `local_entity_id/local_fact_id/local_event_id` | 程序 | 按数组 ordinal 或规范化内容生成 |
| schema version、window binding、global ID、hash | 程序 | 只存在于 envelope/编译产物 |
| typed fact/event 语义、subject/object、temporal mode | VLM | 程序不能从摘要猜出这些关系 |
| facts/events 的 ID、规范化与引用闭包 | 程序 | 由 typed atomic observation 编译 |
| fact support 与 event support | VLM 候选 + 程序校验 | 两者独立；仅注册 containment rule 可继承 |
| continuity/causality 采集状态 | VLM/策略 | 区分 `observed_empty/not_evaluated/indeterminate` |
| 数字 confidence | 不直接采用 | 模型输出 `high/medium/low/indeterminate`；数值只来自离线校准 |
| hook/highlight/candidate 完整对象 | 移出本调用 | 避免候选失败导致视频重跑 |

建议模型输出紧凑、typed atomic observation，不生成字符串 ID。每个 event observation 内嵌
其 claims，避免让程序从自然语言 summary 反推 fact kind、subject/object 或 open question。
实体和观察仍可在同一响应内通过整数 ordinal 关联，但该响应必须作为一个原子对象校验和修复。
例如：

```json
{
  "observations": [
    {
      "kind": "confrontation",
      "summary": "一名学生当众质问另一名学生",
      "participant_indices": [0, 1],
      "coarse_interval": {"start_bucket": "T3", "end_bucket": "T5"},
      "temporal_mode": "current",
      "open_question": "被质问者是否会公开回应",
      "claims": [
        {
          "kind": "interaction",
          "subject_index": 0,
          "object_index": 1,
          "summary": "红发学生公开质问黑衣学生",
          "coarse_interval": {"start_bucket": "T3", "end_bucket": "T4"}
        }
      ],
      "certainty": "high"
    }
  ],
  "entities": [
    {"kind": "person", "visual_description": "红发女学生"},
    {"kind": "person", "visual_description": "黑衣男学生"}
  ],
  "window_summary": "冲突公开升级。"
}
```

`participant_indices` 是本响应内的整数 ordinal；程序检查范围后再生成 durable ref。实体数组
和观察数组的顺序冻结为该响应的 canonical ordering。局部 repair 不得单独插入、删除或重排
任一数组；若必须改变 ordinal 集合，只能生成完整新响应并重跑全部引用校验。无法确认时用空
列表或 `indeterminate`，不得发明 ID。

`T0..Tn` 由程序按实际视频投喂/抽帧分辨率生成并冻结，模型不直接声称毫秒精度。Compiled
Observation 同时记录桶范围、桶分辨率和误差界；粗时间 IoU 按该分辨率计算，绝不能与 Stage 4
ExactSpan 的 tick 精度混为一谈。

continuity 和 causality 不能靠程序填 `false/[]`。若本轮 Prompt 未采集该能力，编译产物必须
写 `not_evaluated`；模型看过但无法判断时写 `indeterminate`；只有明确执行了相应任务且没有
观察到关系时才是 `observed_empty`。任何依赖这些信息的 Admission predicate 遇到
`not_evaluated/indeterminate` 都必须按策略 quarantine/indeterminate，不能当作 pass。

从数字 confidence 迁移到 ordinal certainty 也必须版本化。注册校准 profile 可将某模型/Prompt
版本的 ordinal 映射为有误差界的数值；在 profile 可用前，所有依赖现有数值阈值的 predicate
均返回 `indeterminate`，禁止把 `high/medium/low` 随意硬编码成 `0.9/0.6/0.3`。

### 4.2 Candidate Enricher

候选生成应成为 Stage 1 Admission 之后的独立、可缓存、可局部重跑文本语义调用。仓库已有
`candidate_enrichment_draft/compiler` 的短别名研究方向，但它仍是 future lifecycle contract，
依赖已准入 `Stage1Values`，且字段与本目标并不相同。必须先版本化改造其 Schema、provenance
和运行生命周期，再接线；不能把现有研究代码直接提升为 runtime authority。

程序输入给模型：

- 当前窗口或相邻窗口的精简 event/fact 表；
- `E1/F1/...` 短别名及一句摘要；
- 注册的 `candidate_kind`、`narrative_function`、`editing_mode`；
- 每类候选数量和文本预算。

模型只输出：

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
| L1 引用/时间 | alias 解析率、引用闭包率、相对区间合法率、粗时间 IoU |
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

本次 Linux.do 检索未取得内容：浏览器中没有已登录并配置的 `https://linux.do` 标签页。该失败
不应被误报为“Linux.do 没有相关方案”；上面的结论来自当前代码审计和公开官方/论文资料。

## 九、迁移顺序

### P0：先消除真实运行阻断和无意义全量重跑

1. 冻结当前 V23 的单集 request/schema/raw-response/parser fixture 和成本 baseline；
2. 对 Ark direct `json_schema` wire contract 做独立 canary，再统一并真实验证 Stage 1–3；
3. 为现有 validator 输出稳定 error code + JSON path；
4. 版本化改造 Candidate Enrichment，并补齐 Command/request/Attempt/Receipt、provider profile、
   runtime authority、pipeline ordinal、registry、reconcile/resume、Artifact reader、Stage 2 binding、
   recompute fencing 和 debug stage；
5. 以 shadow lifecycle 对照 V23 candidate，验证一致性和局部重跑后，才发布 observation-only VLM；
6. 保留旧 V23 作为 shadow 对照，不直接覆盖已持久化产物。

### P1：收缩输入和输出

1. 引入每请求冻结的 `AliasCatalog`；
2. 交付 exact Attempt -> compiler binding 后，再删除模型输出中的 ID/binding；常量空值改为显式
   collection state，不得补成否定事实；
3. Stage 1 输入改为别名化窗口语义投影，同时版本化拆分 Observation ledger 和 Narrative closure；
4. 同步迁移 Coverage policy/ledger/dependency proof/Admission/Candidate compiler；
5. Stage 2 移除 SourceGrant 明细、授权和物理模板输出；
6. DurationCompiler 通过 fixture 后，再移除 Stage 2/3 数值时长和 tick 输出；
7. Stage 3 仅瘦模型可见 projection；Kernel evaluator 继续用完整 predecessor pool 独立重建；
8. 为每个新 schema 设置独立版本和兼容 decoder，不修改历史响应解释。

### P2：局部 repair、评测和成本门禁

1. 失败对象级 repair 和累计 token/attempt budget；
2. 分层 fixture metrics 与版本对比报告；
3. schema/prompt/context/token 预算器；
4. 单集、单窗口、单节点 resume 的 HTTP 与 Receipt 验证；
5. 真实单集依次跑通 VLM、Candidate、Stage 1–3 后再扩大批量。

### P3：离线优化

在稳定数据集上评估 DSPy、模型切换、prompt 压缩和候选排序；只有指标提升且所有确定性门禁
通过，才把新版本写入 runtime authority。框架级迁移不是当前前置条件。

## 十、验收判断

本设计完成不能只看“LLM 返回合法 JSON”。至少必须证明：

1. 模型请求中不再出现无业务意义的 UUID/hash/Receipt/未选对象；
2. 模型输出中不存在程序可派生的 ID、binding、授权和物理证明；
3. 候选或 Stage 1–3 失败不会触发已成功视频观察的重新计费调用；
4. 同一冻结输入、Prompt、Schema 和 provider profile 可得到可追踪 Attempt；
5. unknown reference、越界时间和物理证据缺失仍 fail-closed；
6. 单集真实运行通过后，扩批只增加数据量，不改变契约；
7. Stage 3 成功仍不等于 Render 或 publish allow。
