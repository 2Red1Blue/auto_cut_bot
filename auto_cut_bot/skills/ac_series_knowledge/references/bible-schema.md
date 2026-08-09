# Series Bible v1.4 合同

## 当前 Registry v9 分级准入门禁（Registry schema 1.3）

- Registry 模型响应在正式落盘前执行纯本地 contract；缓存命中后执行同一
  contract；进入 Chapter Assignment 前再次执行同一 contract。不得存在“新响应
  校验、旧缓存绕过”或“Batch 校验、单独 Assignment 绕过”的路径。
- contract 一次收集并报告全部跨记录问题，不得遇到首个问题即停止。当前 findings
  包括 canonical name 冲突、alias 与他人 canonical name 冲突、alias 多 owner、
  未知 Event/人物引用，以及正式 Thread 中持续人物的 relationship 未闭合。同场
  Event 拆分数量本身不构成全剧关系义务。
- 纯本地 deletion-only repair 只允许删除不存在对象的可选
  `story_threads.open_question_ids`；未知 Event/人物引用不得猜测或删除。
- relationship closure 修复只在 Registry 的其他合同均有效时运行。模型只能在
  具有真实共享 Event/Fact 证据的已登记人物之间作类型化判断；本地层只导入证据
  有效、严格减少未闭合人物集合的 relationship delta，不编造关系语义。
- relationship `no_supported_relationship` 使用
  `review_status=not_required_weak_evidence|completed|failed` 表达“弱证据无需二审”、
  “已完成二审”和“二审失败”，不得再让 `evidence_reviewed=false` 同时表示未审与
  无需审。满足准入状态后才允许对
  对应人物执行 Identity Audit。合并必须有跨集同人 Event 证据，且同场 canonical
  共现或 subject 已拥有 relationship 时禁止合并；隔离只允许泛化角色标签，并且
  其全部 Event 已被其他人物覆盖、不会使任何 Story Thread 失去全部人物。审计无法
  证明上述条件时不得静默合并或删除人物。
- 证据修复和 Identity Audit 仍无法闭合、且错误可局部归因时，Partial Admission
  把人物、歧义 Event 与依赖 Thread 保留到 `series-registry-quarantine.json`；
  `series-registry.json` 只发布 admitted 核心图。`series-registry-admission.json`
  记录 `ready|partially_ready|blocked` 与逐人物状态，
  `series-registry-validation.json` 证明正式核心图没有 quarantined ID 泄漏。未知
  Event/人物、canonical/alias 冲突和无任何可用 Story Thread 仍全局 `blocked`。
- 明确改变实体身份的限定词（例如 `Alex (AI)`）只隔离对应 Event；`(hologram)`、
  `(mentioned)` 等呈现或叙述标签不单独触发身份隔离。未登记的 identity-changing
  限定词不得被静默吸收到 canonical individual。
- Registry prompt 禁止把“白衣女子”“黑衣人”等可变化的服装、外观或镜头内临时
  描述当作跨人物共享 alias；这类描述只能出现在身份说明或证据文本中。
- 当前 Series Registry cache stage 为
  `story-first-series-registry-v9-typed-coda-v1`。旧 Registry cache/recovery
  signature 不得跨该门禁复用；可从 Registry 重跑并复用已通过当前哈希与 Schema
  验证的 Window Analysis、Event Cards、Episode Digests 和 Chapter Digests。
  Registry 业务 `schema_version` 为 1.3。Assignment 与 Bible 必须同时验证 core、
  Admission、Quarantine 哈希；旧的无 Admission Registry 不得进入正式下游。

## Assignment 归账与依赖门禁（Bible schema 1.4）

- `excluded_episodes` 固定解释为**整集级排除**，不是未被 Thread Beat 使用的
  Event 收纳区。未引用的次要 Event 无需归账。
- `assigned_episode_ids ∩ excluded_episode_ids` 必须为空；
  `assigned_episode_ids ∪ excluded_episode_ids` 必须等于本章 episodes。
- Batch 在新模型响应和缓存命中后都执行 Event、集号、Registry Thread 与整集归账
  合同。可修复的账目冗余在落盘前 canonicalize，严重冲突进入语义重试。
- Assignment canonicalization policy 为
  `series-assignment-accounting-v5-typed-coda`。它保留 v4 的哈希绑定 Registry
  Quarantine 归账与 v3 的全局依赖图收口，并新增 `thread_kind` / coda phase
  硬校验；不得在本地改写 Thread 类型、status 或 phase。逐章门禁允许删除重复依赖、本章内
  self/cross-thread/later 的合同非法 `requires_beat_ids` 边；以及同集已有证据合法
  Beat 时删除 `reason_type=insufficient_evidence` 的冗余整集 exclusion。跨章未知引用
  原样留给 Bible assembler 的全局裁决，不在单章上下文猜测目标。
- 所有 Assignment 通过内容和逐集归账门禁后，既有 Bible assembler 在扁平化 Beat
  之前执行唯一的全局依赖图裁决：保留全部合法边，删除 unknown/ambiguous、self、
  cross-thread、later、duplicate 和 cycle 边。只有全局真实 Beat ID 冲突时才稳定保留
  首个 ID、按 chapter namespace 重命名后出现者，并只改写可唯一解析的依赖；无法
  唯一解析的引用删除并审计，不得猜测。
- 除 ID 冲突及对应唯一引用外，上述操作均为 deletion-only；不得修改 Beat 的 Event、
  Thread、episode、summary 或其他叙事内容。`series-assignment-repairs.json` schema 1.1
  分别记录 chapter repairs 与 `global_dependency_graph` 的原始/有效 SHA-256、修复明细，
  修后立即以 strict 模式复验。
- 只要同一响应还存在证据错集、未知 Event/Thread、漏集或整集排除冲突等内容/账本
  错误，不得局部应用依赖或 exclusion 修复；完整错误集合进入语义重试。Batch 首次
  响应、缓存命中、filtered rerun 与 Bible assembler 必须调用同一 canonicalizer，
  禁止分层规则漂移。
- `non_narrative`、`recap_only`、`credits_or_placeholder`、
  `corrupted_or_unavailable` 与合法 Beat 冲突时不得自动选择一方。
- 取消“未覆盖集自动补 insufficient_evidence”。未归账必须重试后停止。
- 唯一新增的本地补账是 `registry_quarantined_dependency`：只有某集全部已知
  Event 都被当前已验证 Quarantine 覆盖、且该集没有 Beat 或其他 exclusion 时才能
  确定性补入。正式 exclusion 的 `event_ids` 固定为空，完整隔离证据只保存在
  Quarantine；只要该集仍有一个 admitted Event 就不得自动排除。
- Assignment Thread Beat、普通 exclusion 与正式 Bible 引用任一 quarantined ID
  均硬失败。动态 Assignment Schema 从合法 Event enum 中剔除 quarantined Event。
- Series Assignment stage 为 `story-first-series-assignment-v6-typed-coda`，
  动态 response schema revision 为 `v5_typed_coda`；请求签名绑定 Registry
  Admission、Quarantine 摘要和 Thread 类型，正式 Bible `schema_version` 为 1.4。

## v1.4 变更摘要（typed coda）

- Registry `story_threads[].thread_kind` 为必填枚举：普通因果线使用 `arc`；仅全剧
  末端尾声、框架揭幕、杀青/全剧终或最终后果可使用 `coda`。不得再根据 Beat 数量或
  terminal phase 猜测 coda，也不得把素材不足的短 arc 标成 coda。
- `thread_kind=arc` 且 `status=resolved` 时仍必须同时存在 setup 与 payoff；装配器
  不再把不完整 resolved Thread 静默降级为 `partially_resolved`。
- `thread_kind=coda` 全局只能包含 1–2 个 Thread Beat，phase 只能是
  `payoff|consequence|coda`，且至少一个 Beat 必须显式使用 `phase=coda`。Event 摘要
  可以包含 reveal 细节，但结构 phase 不得因此改成 reveal。
- Bible 原样保留 `story_threads[].thread_kind`，Broad Subarc Compiler 只允许显式
  typed coda 生成 coda Option。Registry、Assignment 和 Broad Catalog 的动态 schema、
  stage version 与请求签名全部变化，旧模型缓存不得跨版本复用。

## v1.3 变更摘要（2026-07-29 稳定性审计）

- **历史 schema 线**：Series Bible v1.3 稳定性审计取代 v1.2；Registry 在 Partial
  Admission 时升至 1.2。当前生产合同已分别为 Bible 1.4 / Registry 1.3；旧版本只能
  只读复现，无法跨当前 schema strict 校验复用。
- **id kebab-case regex 强约束**：`characters[].id` 必须 `^char-[a-z0-9-]{2,40}$`；
  `relationships[].id` 走 `^rel-…`；`story_threads[].id` 走 `^thread-…`；
  `open_questions[].id` 走 `^q-…`；`facts[].id` 走 `^fact-…`。挡住
  `char_xxx`（下划线）/ 无前缀 / 混大小写等历史漂移。
- **`entity_type` 必填**：`individual | group | creature | unknown`。挡群体/
  NPC/龙群/未消歧实体和真正的主角一起挤进"主要人物"。
- **`identity_evidence` 必填**：`{episode:int, quote:≥10字}`。每个人物身份声明
  必须挂一条原剧证据；空评论/占位 quote 会被 schema 拒。
- **`language` 顶层必填**（`zh|en`），并注入本地渲染器；模型自由文本字段严格
  跟随此语言。挡中/英随机漂移。
- **`aliases[].minLength=2`** + assemble 期 uniqueness lint：同一 canonical_name
  经 NFKC/casefold/去 diacritic 归一后全剧唯一；任意角色的 aliases 不得等于
  他角色的 canonical_name 或 aliases。挡 `Aldric` vs `Aldrich` 分裂与
  `Lucien` 被列成 Raegar 别名两类历史 bug。
- **`metadata` 必填**（Bible 顶层）：
  ```
  {pipeline_version, skill_version, generated_at, model_id, seed,
   prompt_template_hash, input_manifest_hash, output_language,
   determinism_class}
  ```
  每次 assemble 由本地代码写入，模型不能干预。审计跨 run 差异第一现场。
- **`main_characters` 派生字段**（Bible 顶层）：由本地 `_compute_importance` +
  `_derive_main_characters` 按 `3·event + 2·rel + 4·thread` 客观打分排序，
  只取 `entity_type=individual`，数量上限 `min(12, ceil(episode/6)+3)`。模型
  不再输出这个字段。
- **关系闭合 lint**：进入正式 Story Thread 且具备持续证据的 `individual` 必须至少
  出现在一条 relationship 中；同场 Event 数量不能单独触发。无法闭合时只隔离该
  人物及依赖 Thread，除非已经没有任何可用核心 Thread。
- **确定性 CI**：`test_series_bible_determinism.py` 用 `references/fixtures/*`
  连续跑 5 次 assemble，byte-identical 才通过；改动 event 时
  `input_manifest_hash` 必须变。

## 目标

Series Bible 用 Event、Episode Digest 和 Chapter Digest 表达全剧人物、关系、事实、
未解问题以及逐集 Story Thread 推进。它必须回答两个不同问题：

- `ingestion_coverage`：素材、视频窗和逐集摘要是否都被读取。
- `narrative_coverage`：每一集是否已归入至少一条全局故事线，或被明确排除。

摄取完整不等于叙事完整。禁止再用“42/42 Episode Digest”代替“42 集均进入故事线”。

## 两阶段语义任务

### 1. Series Registry

`series_registry` 读取紧凑的全剧 Chapter Digest、逐集 local thread 更新索引和 Event
锚点，输出：

- `schema_version="1.3"`
- `language: "zh"|"en"`（新增：定义所有 free-text 字段的强制语言）
- `characters[]`（每人必须给出 `entity_type` + `identity_evidence`；见下）
- `relationships[]`
- `facts[]`
- `story_threads[]`（每条必填 `thread_kind: "arc"|"coda"`）
- `open_questions[]`
- `unresolved_identity_conflicts[]`

Registry 负责把不同集、不同章中命名不一致的 local `thread_key` 归一为稳定全局
`thread_id`。它不输出逐集 Thread Beat，也不计算 Coverage。它也不输出
`main_characters` —— 这个字段由本地 assembler 依据打分派生。

**Registry 合同要求**（模型输出前必须逐条自检；违反任一条都会被
本地 schema-strict 拒绝或 assemble 期硬 fail）：

1. 所有 id 严格匹配对应 regex：`char-<slug>` / `rel-<slug>` / `thread-<slug>`
   / `q-<slug>` / `fact-<slug>`，`<slug>` 为 `[a-z0-9-]{2,40}`。不允许下划线、
   点号、大写字母，也不允许省略前缀。
2. 每个 character 必须声明 `entity_type` 四选一：`individual`（具名个体）
   `group`（群体、组织、部落、军队等复数实体）`creature`（非人角色、动物、
   神兽）`unknown`（身份未消解）。禁止把群体/龙群/百姓塞进具名个体一栏。
3. 每个 character 必须给出 `identity_evidence: {episode, quote}`，`quote` 长度
   ≥ 10 字，episode 必须真实存在。禁止空引用或占位符。
4. `canonical_name` 全剧唯一（本地会经 NFKC + casefold + 去 diacritic 归一后
   去重）；不同角色之间 `aliases` 严禁与另一角色的 canonical_name 或 aliases
   相同。历史上出现过 `Aldric` vs `Aldrich`、`Lucien` 被列为 Raegar 别名两
   类问题，都由此挡住。
5. 所有 free-text 字段（`series_summary` / `characters[].identity` /
   `story_threads[].title` / `open_questions[].question` 等）必须严格使用
   `language` 声明的语言，禁止中英混排。
6. 每条 Story Thread 必须显式声明 `thread_kind`。普通贯穿因果线使用 `arc`；
   `coda` 只保留给末端尾声/框架揭幕/杀青或最终后果，不能作为短线逃生舱。
7. Registry **不输出** `main_characters` 与 `entity_importance`。这两个字段
   由本地 assembler 依据 `evidence_event_ids` / relationships / story_threads
   引用计数客观派生（`3·event + 2·rel + 4·thread`），排序取
   `entity_type=individual` 前 N（N = `min(12, ceil(episode/6)+3)`）。

### 2. Chapter Assignment

`series_assignment` 每章单独运行。输入全局 Registry、该章 Episode Digest、Chapter
Digest 和本章 Event 子集，输出：

```json
{
  "schema_version": "1.0",
  "chapter_id": "chapter-025-030",
  "episodes": [25, 26, 27, 28, 29, 30],
  "thread_beats": [],
  "excluded_episodes": []
}
```

每个 Thread Beat：

```json
{
  "id": "thread-trial-ep030-turn",
  "thread_id": "thread-trial-power",
  "episode": 30,
  "phase": "setup|escalation|turn|reveal|payoff|consequence|coda",
  "importance": "required|supporting|optional",
  "summary": "该集对全局故事线造成的具体推进",
  "event_ids": ["event-..."],
  "requires_beat_ids": ["thread-trial-ep029-escalation"]
}
```

`required` 表示缺失后会破坏子故事的因果、揭示、转折或兑现；它会成为后续 Catalog、
Script、Evidence 和 Story Plan 的硬义务。

Assignment 必须服从 Registry 的 `thread_kind`。arc 按普通因果线恢复；coda 只能
生成 1–2 个 `payoff|consequence|coda` Beat，且完整 Assignment 集合中至少一个使用
`phase=coda`。错误返回语义重试，不允许本地改写 phase。

## 本地确定性装配

语义模型不直接生成正式 `series-bible.json`。运行
`assemble_series_bible.py` 合并 Registry 与全部 Chapter Assignment，本地派生：

- `metadata`（v1.4 审计栏：pipeline_version / prompt_template_hash / input_manifest_hash / … ）
- `main_characters`（由客观打分排序取前 N，只含 `entity_type=individual`）
- `entity_importance`（每个角色的 score + 事件/关系/故事线引用数分解）
- `story_threads[].thread_kind`
- `story_threads[].event_ids`
- `setup_event_ids`
- `escalation_event_ids`
- `reveal_event_ids`
- `payoff_event_ids`
- `thread_beat_ids`
- `episode_ids`
- 摄取覆盖和叙事覆盖

正式 Bible 使用 schema `1.4`，pipeline version 为 `series-bible-v1.4`。CLI 支持
`--model-id / --seed / --generated-at`
把审计信息透传进 metadata（缺省从 registry 的 `_backend_meta` 回读）。

## 覆盖合同

```json
{
  "coverage": {
    "ingestion_coverage": {
      "source_count": 42,
      "episode_count": 42,
      "window_count": 42,
      "episode_digest_count": 42,
      "missing_episode_ids": []
    },
    "narrative_coverage": {
      "covered_episode_ids": [1, 2, 3],
      "unassigned_episode_ids": [],
      "excluded_episodes": []
    }
  }
}
```

每一集必须满足且只满足其一：

1. 至少属于一个 Thread Beat。
2. 进入 `excluded_episodes`，并给出
   `non_narrative|recap_only|credits_or_placeholder|corrupted_or_unavailable|insufficient_evidence`
   类型和具体说明。

本地 canonicalizer 可在完整哈希绑定隔离覆盖时使用第六种
`registry_quarantined_dependency`。该类型不是模型对“证据不足”的自由判断，且
`event_ids` 必须为空。

未被任何 Beat 引用的 Event 不需要另行归账。若本集已有 Thread Beat，不得因为仍有
等待、反应、过场或被主 Beat 包含的 Event 而把整集写入 `excluded_episodes`。

正式装配时 `unassigned_episode_ids` 必须为空。要求 100% 集数归账，不要求 100%
Event Card 都进入故事线。

## 硬校验

1. 所有 Event 引用必须来自 `event-cards.jsonl`。
2. 每个 Thread Beat 的 `event_ids` 必须至少包含一个真实 Event；空数组不是
   "待补充"状态，而是 Series Assignment 合同错误。
3. Thread Beat 的 Event 必须属于其声明 Episode。
4. 每个 `thread_id` 必须来自全局 Registry。
5. `requires_beat_ids` 的最终有效值必须存在、属于同一 Thread、不得指向更晚集或
   自身，且全图必须无环。逐章 canonicalizer 删除本地可证明的非法边；跨章引用由
   assembler 在完整 Assignment 集合上删除/消歧并审计。strict 模式不修改输入并
   对相同问题硬阻断。
6. 同一 Episode 不得同时"已分配"和"明确排除"。
7. `thread_kind=arc` 且 `status=resolved` 的 Story Thread 必须至少含 `setup` 和
   `payoff`；装配器不得静默降级 status。
8. Registry 中每条全局 Story Thread 必须至少有一个 Assignment Beat。
9. 无法消歧的人物默认保持阻断；只有当前 evidence-gated Identity Audit 证明满足
   合并或隔离门禁时才可执行对应修复，否则保留 `unresolved_identity_conflicts`。
10. Coverage 只由本地代码计算，模型不得提供或修改。
11. **v1.3 命名唯一性**：全剧 `canonical_name` 归一后唯一；aliases 不与他角色
    的 canonical_name 或 aliases 相同。
12. **v1.3 身份证据**：`characters[].identity_evidence.episode` 必须为已知的
    源集。
13. **关系闭合与分级准入**：进入正式 Thread 且具备持续证据的 individual 必须
    闭合 relationship；局部失败人物和依赖 Thread 进入 Quarantine，正式 Bible
    引用 quarantined ID 时硬 fail。
14. **v1.3 metadata 不可缺**：Bible 顶层必须带 `metadata` 且各字段全部通过
    schema pattern（timestamp、hash 前缀等）。
15. **Quarantine 整集归账**：`registry_quarantined_dependency` 必须由当前
    Quarantine SHA-256 证明该集全部 Event 已隔离；存在 admitted Event、携带隔离
    Event ID 或缺少哈希绑定时均硬 fail。
16. **typed coda**：`thread_kind=coda` 只能包含 1–2 个终局 Beat，phase 仅允许
    `payoff|consequence|coda`，并且至少包含一个 `phase=coda`。

正式 Series Bible `schema_version=1.4`；Series Registry `schema_version=1.3`；
Series Assignment cache stage 为
`story-first-series-assignment-v6-typed-coda`，per-job response schema revision
为 `v5_typed_coda`；Assignment JSON 的业务 `schema_version` 仍为 1.0。
旧 Assignment cache 不跨新的 Registry Admission 请求签名复用；本次变更不新增模型
阶段，仍由既有 Assignment 恢复逐集账本。

## 分层归纳

默认每集一个 Episode Digest，每 6 集一个 Chapter Digest。Chapter 只是上下文缩片，
不成为新的事实来源。Registry 建立稳定全局词表；Assignment 恢复逐集账本；正式 Bible
中的每个 Thread Beat 最终仍可回溯到 Event ID。
