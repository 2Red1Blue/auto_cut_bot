# Story Catalog 与 Story Script 合同

## 目录

1. Story Catalog
2. Story Script 生命周期
3. 顶层合同
4. Story Beat 合同
5. 具体性与证据规则
6. Retrieval Requirements
7. 素材可行性预检
8. 审批视图
9. 下游稳定接口

## 1. Story Catalog

发现所有真实、有证据且具独立观看价值的故事子弧，不设固定输出数量或候选数量
配额。每个候选故事说明：

- 中心人物、关系和 Story Thread。
- `source_thread_beat_ids` 定义的连续故事子弧。
- `subarc_start_beat_id`、`subarc_end_beat_id` 和不可跳过的
  `required_bridge_beat_ids`。
- 中心冲突或中心问题。
- 开始状态和结束状态。
- 可形成的局部 Payoff。
- 同线 Hook 可能性。
- 必要 Fact 和证据 Event。
- 建议 Highlight/Hook Candidate。
- 初步预计可用原片秒数。
- 与其他故事的重叠。

### Broad Story granularity（v4.20.2+）

- 先运行本地 Broad Subarc Option Compiler，由 Coverage
  Compiler 确定推荐覆盖集，再为每个推荐 Option 建立一个
  `stories.minItems=maxItems=1` 的独立模型任务。每个动态 Schema 分支把
  `subarc_option_id` 与 Option 的
  `story_thread_ids`、`source_thread_beat_ids`、起止 Beat、required bridges、
  evidence Events 和本地计算的原片秒数锁成常量；Highlight/Hook Candidate
  分别使用类型枚举。全部 shard 通过后由本地 assembler 按推荐 Option 顺序聚合，
  从请求结构上消除重复/漏选。Catalog 固定携带
  `story_granularity=broad`；Legacy 或缺少标记的旧 Story 产物必须从 Broad
  Catalog 重跑。

Broad 普通 Story 使用 4–8 个 Thread Beat，依赖闭合后硬顶 12；1–2 Beat 只允许
typed `coda`，三 Beat 只允许 `compact_resolution`。Catalog 联合覆盖全部
`importance=required` Beat，且非 coda Beat 覆盖率不低于 85%。42 集 7–10 个
Story 是软目标；覆盖硬合同优先，不得按数量配额复制、裁剪或改名。

按故事完整性、独立清晰度、高光相关性、素材充足度、因果清晰度、
Hook 同线性和背景成本评分。不要让高光强度单独决定排序。


- 为合格候选分配 `primary_story_ids` 和 `reserve_story_ids`。
- 为每个 Primary 分配从 1 开始的稳定 `production_slot`。
- 用中心问题、Local Payoff、Story Thread 和 Event 重叠检查近重复。
- 默认只为 Primary Story 生成完整 Story Script；Primary 在 Script 有界修复耗尽后
  形成正式 rejection 时，才允许 `story-portfolio-replenishment.json` 中类型化晋级的
  Reserve 接替空槽并完整重走 Treatment → Script → Preflight。
- Promotion 生效后，被替换 Story 只保留正式 rejection 审计，不再是活跃 Script
  目标；每个 `production_slot` 在 Script Index 与 Approval 中只能有一个活跃 Story。
- Broad 按 required / payoff / 非 coda / 全量 Beat 覆盖优先选择 Primary，
  主体重叠与 Story 数量作为惩罚项；未入选候选进入 Reserve。
- 不得复制、改名或人为拆分故事凑数。

## 2. Story Script 生命周期

严格按以下状态推进：

```text
Story Catalog
  → Story Portfolio / Primary + Reserve
  → Story Treatment Options / deterministic editorial strategies
  → story_script_draft / status=draft
  → 本地素材可行性预检
    ├─ Teaser/must-have/Payoff 结构性可覆盖
    │    → feasibility.status ∈ {feasible, partial}
    │    → story_script / status=awaiting_approval
    └─ 结构性缺失
         → feasibility.status=not_feasible
  → SHA-256 绑定的人工决定
```

禁止把模型草稿直接送审。禁止让模型自行决定最终素材覆盖状态或估算结果。
Portfolio 与前置合并规则集中在 [story-portfolio.md](story-portfolio.md)。

Story Script 是 Editorial Blueprint，描述"怎样把故事讲清楚"。不要在其中写入
`source_start`、`source_end`、最终 Clip 数量、转场或渲染参数。

## 3. 顶层合同

使用 schema `1.6`。Script 请求上下文为 `1.6`，在既有 Story/Thread/Event/
Candidate 与 Timeline Segment 外新增确定性的 `direct_evidence_contract`：逐项给出
`Thread Beat → allowed_direct_event_ids → Event source ranges → overlapping Timeline
Segment refs`，并附 Candidate 的物理范围。`direct_evidence_contract` 自身为 `1.2`，
额外把 Event 按与 Preflight 相同的 envelope/gap/Timeline Segment 规则编译为
`physical_event_units`；`allowed_direct_event_ids` 明确是可选证据菜单而非完成清单，
Candidate 同时声明物理 unit 与直接揭示的 Fact，Teaser Candidate 另给出只包含
Highlight 自身 Event 的最安全 direct evidence。
该合同只描述合法身份与物理原子单位，明确禁止本地代码生成最终 Editorial Beat；
拆分后的语义、因果顺序和叙事功能仍由模型重写。正式 Story Script schema 保持
`1.6`，不新增模型自报字段。
Broad Catalog 额外带 `story_granularity=broad`、
`subarc_option_catalog_sha256` 与每 Story 的 `subarc_option_id`；Legacy 或无标记
Story 产物不再支持，必须从 Broad Catalog 重跑。要求以下控制字段：

- `story_promise`：观众看完本故事可获得的明确叙事承诺。
- `primary_story_thread_id`：Treatment Compiler 从现有 Thread Beat 中确定的
  本 Story 主线；不是新的全局 Story Thread。
- `treatment_options_sha256`：逐字绑定 `story-treatment-options.json`。
- `portfolio`：绑定 Portfolio SHA-256、Primary 角色和生产槽位。
- `central_question`：贯穿本单元的核心问题。
- `start_state` / `end_state`：可比较的状态变化。
- `local_payoff`：本故事实际兑现的答案、关系变化或情绪结果。
- `target_duration`：v4.13+ 撤除硬下限，只保留 1200 秒硬上限。字段保留供
  下游读取（`minimum_seconds` / `preferred_minimum_seconds` /
  `soft_target_seconds` 常量为 0）。禁止靠重复或整集填长。
- `required_fact_ids`：为了理解和兑现故事必须交代的事实。
- `intentional_mystery_fact_ids`：允许暂时隐藏、但必须被显式管理的事实。
- `selected_thread_beat_ids`：当前 Story Script 实际承担的 Thread Beat。
- `required_thread_beat_ids`：由 Catalog 子弧起点、终点、Bridge 和 Bible 中
  `importance=required` 的节点确定；模型不得删除。
- `omitted_thread_beats`：未选择的 supporting/optional 节点及类型化原因。
- `evidence_event_ids`：覆盖所有 Beat、must-show 和 Hook 的直接证据 Event。
- `feasibility`：由本地预检生成，模型草稿中不得伪造。
  其中 `treatment_viability` 固定记录已选 Treatment、可行性、
  稳定失败码、已编译备选项和顺叙安全回退项。

Approval 前还必须通过三类 Fact 硬门禁：Must-show direct Event
不得属于同 Beat 隐藏的 Fact；同一 Fact 不得在同 Beat 同时
`required_before` 与 `introduced`；`required_before` 必须由更早 Beat
引入。对应稳定错误码为 `beat_fact_visibility_conflict`、
`fact_required_and_introduced_same_beat` 与
`required_fact_not_previously_introduced`。

Broad Script 的 `selected_thread_beat_ids` 必须覆盖 Catalog 子弧 80%–100%，
所有 required Beat 必须选择；允许一个 Editorial Beat 的
`retrieval_requirements.thread_beat_ids` 同时承担多个 Thread Beat。Editorial
Beat 数量为 4–14。

使用以下 Portfolio 绑定：

```json
{
  "portfolio_sha256": "64位sha256",
  "role": "primary",
  "production_slot": 1
}
```

逐字复制生成上下文中的绑定。Portfolio 内容或生产槽位变化后，重新生成受影响脚本。

Beat 开场结构由所选 Treatment 决定。所有策略至少包含：

- 一个 `orientation` 或 `setup`。
- 一个 `escalation`。
- 一个 `payoff`。
- Hook 非必需（v4.13+）：确无合法同线 Hook 时把 `ending_hook_intent.may_be_empty`
  置为 true 并省略 `end_hook` Beat，Story Script 仍合法；有合法 Hook 时以
  `end_hook` 结束。

同时要求因果角色中至少存在 `cause`、`escalation` 和 `payoff`。

### Treatment 与高光前置合同

完整读取 [story-treatment.md](story-treatment.md)。Story Script 必须从已编译
Options 中选择且只选择一个，并逐字复制
`treatment_option_id/strategy/mode/reprise_policy`：

- `chronological_compression`：第一 Beat 是 `mainline` 正文，不包含
  `teaser_intent`，Highlight ID 为空。
- `cold_open_no_reprise`：第一 Beat 是 `future_preview teaser_intent`；
  `reprise_beat_ids=[]`，正文不得物理重放 Teaser 原片范围。
- `cold_open_delayed_reprise`：第一 Beat 同样是单 Highlight Teaser；全部
  `explanation_beat_ids` 必须早于 reprise，二者之间至少有一个
  `thread_role=primary` 的升级/转折/兑现 Beat，重放只发生在声明的
  `reprise_beat_ids`。

冷开场仍只允许一个 `type=highlight` Candidate 与一个连续 atomic Clip；
Teaser 直接证据必须同源、相邻 stitch，硬上限 15 秒。`no_reprise` 的重放判断
使用最终 Span 的物理 source range，而不是 Event ID 相等。

## 4. Story Beat 合同

每个 Beat 必须包含：

```json
{
  "id": "beat-001",
  "role": "teaser_intent|orientation|setup|escalation|turn_or_reveal|payoff|end_hook",
  "dramatic_purpose": "本 Beat 在全故事中的功能",
  "narrative_description": "给编辑和审核人的编排说明",
  "concrete_story_content": "原片中必须实际发生的具体事件",
  "must_show": [
    {
      "id": "show-001",
      "description": "缺少后就不能认定 Beat 成立的内容",
      "observable_via": "visual|dialogue|action|screen_text|reaction|mixed",
      "evidence_event_ids": [],
      "evidence_fact_ids": []
    }
  ],
  "must_not_reveal_fact_ids": [],
  "required_before_fact_ids": [],
  "introduced_fact_ids": [],
  "resolved_question_ids": [],
  "viewer_state_before": [],
  "viewer_state_after": [],
  "emotional_change": {
    "from": "",
    "to": ""
  },
  "causal_role": "context|cause|escalation|reveal|payoff|consequence|hook",
  "event_ids": [],
  "candidate_suggestions": [],
  "retrieval_requirements": {},
  "temporal_position": "mainline|earlier_context|future_preview|parallel",
  "thread_role": "primary|integrated_support|independent_secondary",
  "must_have": true
}
```

预检后追加：

```json
{
  "estimated_source_duration_seconds": {
    "minimum": 0,
    "maximum": 0
  },
  "evidence_status": "covered|partial|missing|conflicting|needs_video_review",
  "material_risks": [],
  "physical_evidence": {
    "physical_ranges": [],
    "source_count": 0,
    "atomic_event_count": 0,
    "physical_union_duration_seconds": 0,
    "physical_envelope_duration_seconds": 0,
    "internal_gap_seconds": 0,
    "timeline_segment_count": 0,
    "compaction_status": "atomic|split_regeneration_required|continuity_required|continuity_fallback"
  },
  "continuity_required": false
}
```

## 5. 具体性与证据规则

执行以下硬规则：

- 写出能被画面、对白、动作、反应或屏幕文字观察到的具体事件。
- 禁止仅写“矛盾升级”“女主反击”“关系破裂”“发现背叛”“留下悬念”。
- 让每个 `must_show` 至少绑定一个 Event 或 Fact；找不到时保留空证据，
  并让预检标为 `partial` 或 `missing`。
- 一个 `must_show` 只表达一个可观察动作、对白、反应或屏幕文字义务。确实需要
  多个 direct Event 才成立时，`evidence_event_ids` 必须完整列出；下游按 AND
  覆盖，不能把任一 Event 命中当成整项成立。
- 优先让每个 Editorial Beat 由可独立剪辑的 direct Event 组成。多个 Event
  跨较大物理 gap 或多个 Timeline Segment 时拆成保持因果顺序的多个 Beat；
  只有原片确属同一 Segment 的连续表演才声明 `continuous_scene`。
- 不得把 `must_not_reveal_fact_ids` 同时写入本 Beat 的
  `introduced_fact_ids`。
- 让每个非 Teaser Beat 的 `required_before_fact_ids` 已在此前 Beat 引入。
- 让顶层 `required_fact_ids` 在整个脚本中至少引入一次。
- 只引用输入上下文存在的 Character、Relationship、Thread、Thread Beat、Fact、
  Question、Event 和 Candidate ID。
- 每个 `selected_thread_beat_id` 必须至少被一个 Editorial Beat 的
  `retrieval_requirements.thread_beat_ids` 引用。
- Editorial Beat 同时引用 Thread Beat 与 direct Event 时，该 Event 必须属于
  `direct_evidence_contract.thread_beats[].allowed_direct_event_ids`；真实但属于另一
  Thread Beat 的 Event 仍是身份错挂，不能因为 ID 存在就通过。
- `selected_thread_beat_ids` 与 `omitted_thread_beats` 必须无重叠并完整归账
  Catalog 的 `source_thread_beat_ids`。
- `required_thread_beat_ids` 必须全部进入 selected，禁止用“时长不足”省略必需桥接。
- 禁止编造对白、旁白、动作和剧情。引用对白时只引用 Event 中已有证据，
  不在 Story Script 中自由续写。
- 让 Payoff 解决一个局部问题。不要用新的悬念代替 Payoff。
- 让 Hook 属于同一 Story Thread 或其直接后果线。

## 6. Retrieval Requirements

为每个 Beat 填写：

```json
{
  "search_intent": "要从原片召回什么",
  "character_ids": [],
  "relationship_ids": [],
  "story_thread_ids": [],
  "thread_beat_ids": [],
  "fact_ids": [],
  "event_ids": [],
  "candidate_ids": [],
  "continuity": "continuous_scene|causal_chain|montage_allowed",
  "lookback": "same_episode|earlier_episodes|whole_series"
}
```

让该对象成为后续 Story Evidence Retrieval 的稳定输入：

- 使用 `search_intent` 做语义召回。
- 显式 `event_ids`、`candidate_ids`、`thread_beat_ids` 是功能证据，可进入
  preflight 可剪时长和后续 Legal Option。
- `character_ids`、`relationship_ids`、`fact_ids`、`story_thread_ids`
  扩展出的 Event 是 context-only recall；它们可进入 Evidence 的
  scene/context 层，但不得计入 Script 功能时长。
- whole Thread 与 Fact/实体扩展不得进入 `must_show` 功能覆盖；物理压缩诊断只
  使用显式 Event、Candidate 与 Thread Beat 的功能证据。
- 使用 `continuity` 约束连续原片与跨场拼接。
- 使用 `lookback` 控制补人物、关系和背景时允许回看的范围。

不要在此阶段选定最终时间码。

## 7. 素材可行性预检


预检执行：

1. 校验所有 ID 和 must-show 证据。
2. 分离功能证据、direct atomic evidence 与 context-only recall：显式 Event、
   Candidate、Thread Beat 进入功能覆盖和整体时长；Fact、Character、
   Relationship、whole Thread 只扩展上下文。Thread Beat 展开的 Event 不进入
   单个 Editorial Beat 的原子物理包络。
3. 单 Beat 的 direct atomic evidence 只读取 `beats[].event_ids`、
   `must_show[].evidence_event_ids`、`retrieval_requirements.event_ids` 与显式
   Candidate 的 Event/range；仅对这些证据合并同源重叠区间。功能时长仍可使用
   Thread Beat Event，避免丢失整体素材可用量。
4. 根据 direct atomic Event/range 的物理包络、内部 gap、Timeline Segment 数
   和原子 Event 数生成
   `physical_evidence`。多 Event 且包络超过 60 秒、内部 gap 超过 12 秒，或跨
   多 Segment 的宽 Beat 标记 `split_regeneration_required`；同一 Segment 的
   `continuous_scene` 标记 `continuity_required`；无证据安全拆法的宽单元标记
   `continuity_fallback`，不得静默硬切。
5. `split_regeneration_required` 写入稳定失败码
   `beat_physical_compaction_required`；direct Event 错挂 Thread Beat 写入
   `beat_event_thread_beat_mismatch`。这两类属于 compile-only failure：锁定当前
   Treatment，并与普通/Treatment 语义重试独立计数；即使 fallback 用掉一次普通
   重试，仍保留完整两轮 compile repair，不消耗其他 Treatment Option。
   普通 schema、Treatment、Fact 或因果错误仍只有一次结构纠错。
6. compile-only 重写反馈包含 Event 物理 range、Timeline Segment refs、每个 Event
   的合法 Thread Beat、每个 Thread Beat 的合法 Event、must-show AND group 与
   Candidate ID。compile repair 的动态 strict Schema 只允许返回失败 Beat 的
   replacement，禁止整份 Script 重写。模型可以拆分失败 Beat、在 replacement 内
   移动 must-show，或删除普通冗余 Event 引用；本地只按原顺序合并模型给出的完整
   replacement Beat，不自行生成 Editorial Beat 语义。
7. 首个 compile-only 失败会冻结 preservation contract：Treatment、selected/required
   Thread Beats、required Fact、intentional mystery、中心问题、起止状态、Local
   Payoff，以及原 must-show ID 和 Event/Fact AND 证据不得删除或弱化。未失败 Beat
   保存逐 Beat hash 并必须原样保留；旧失败 Beat 的 explanation/reprise 引用只可
   映射到该 replacement 内的新 Beat。合并后重新执行完整 validator/preflight；
   超过 Beat 上限、替换非失败 Beat、引用外部 replacement ID 或修改冻结合同均拒绝，
   invalid fragment 不写正式 Script/cache。若连续两次的失败 Beat、Event 分组和错误
   签名完全不变，立即以 `no_progress` 停止；两轮仍失败则以 `exhausted` 停止，均保持
   `not_feasible`，不得下放给 Span/Plan 猜测拆分。HTTP/网络失败没有产生语义响应，
   不消耗普通或 compile repair 预算。
8. 使用明确记录的上下文扩边秒数和可用率估算上下界。
9. 标记每个 Beat 的覆盖状态和视频复核风险。
10. 汇总 Highlight、Hook、缺失 Beat 与视频复核风险；估算原片时长上下界仅作
   观测输出（v4.13+ 时长下限已撤除，成片时长兜底由 Render 阶段 filler tail
   处理，详见 SKILL.md rule 36）。
11. 生成 `story-feasibility.json` 与 `story-script-preflight.json`
   script_preflight` 直接消费）。
12. 将脚本状态从 `draft` 改为 `awaiting_approval`。

Feasibility method 为
`functional-evidence-duration-v4-direct-atomic-compaction`，并在顶层保存
`split_regeneration_beat_ids`、`continuity_required_beat_ids` 与
`story-first-story-script-v18-broad-contract-guided-authoring`；旧 Script cache 因
`story-script-treatment-retry-v5-contract-checklists`，该 policy 进入
请求签名；Treatment attempt audit 为 `1.4`，Script context schema 为 `1.6`，
正式 Script schema 仍为 `1.6`。

解释可行性状态（v4.13+，时长下限撤除后只保留三档结构判定）：

- `feasible`：must-have Beat、Teaser 契约与 Payoff 均可覆盖，且无
  partial/需人工视频复核状态。
- `partial`：结构可覆盖，但存在部分证据或边界需要人工视频复核。
- `not_feasible`：Teaser 合同失败、must-have Beat 缺失/冲突或 Payoff 不可用。

把估算当成审批前风险判断，不要把它当成最终成片时长。最终时长只能由后续
Story Plan 的精确 Clip 计算；render 阶段还有 filler tail 兜底。

## 8. 审批视图

同时展示：

- Logline、Story Promise、中心问题、开始/结束状态和 Local Payoff。
- 每个 Beat 的具体故事内容、must-show、观众状态、因果位置和证据。
- 每个 Beat 的素材状态、预计原片时长和主要风险。
- 整体预计可用原片时长（观测用，不再作为门禁）。
- Highlight/Hook 建议。
- 缺失 Beat 和待视频复核 Event。

禁止批准 `not_feasible`。批准 `partial` 时要求审核人显式接受风险并填写说明。

## 9. 下游稳定接口

让后续阶段只依赖已批准脚本中的以下字段：

- `beats[].must_show`
- `beats[].retrieval_requirements`
- `selected_thread_beat_ids`
- `required_thread_beat_ids`
- `omitted_thread_beats`
- `beats[].event_ids`
- `beats[].candidate_suggestions`
- `beats[].temporal_position`
- `beats[].viewer_state_before/after`
- `beats[].must_not_reveal_fact_ids`
- `beats[].evidence_status`
- `beats[].physical_evidence`
- `beats[].continuity_required`
- `feasibility.split_regeneration_beat_ids`
- `feasibility.continuity_required_beat_ids`
- `feasibility.continuity_fallback_beat_ids`
- `local_payoff`
- `ending_hook_intent`

如果后续素材检索无法覆盖 must-have Beat，回退为素材缺口或 Story Script 修订。
禁止在 Story Plan 中静默删除 Beat。
