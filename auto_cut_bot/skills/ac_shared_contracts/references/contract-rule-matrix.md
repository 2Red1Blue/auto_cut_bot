# 合同规则落地矩阵 — SKILL.md 固定合同 1–36 × 规则引擎

本矩阵对齐 `edit-autocut-compilations-v3/SKILL.md` "固定合同" 章节的 36 条
合同规则与 `autocut_core/contracts/rules/` 声明式规则引擎的落地状态。

- **已落地**: 已由规则引擎以声明式规则实现, 可在纯数据结构上判定, 列出 rule_id;
- **示范移植**: 旧 `validate_story_artifacts.validate()` 检查组的表化示范 (va_* 规则);
- **文字约定**: 尚未机器化, 仍靠 SKILL.md 文字约定 + 旧脚本顺序断言保障。

引擎入口: `autocut_core.contracts.rules.default_engine()`,
报告类型 `RuleReport.violations` 即现有 `ContractViolation` 列表。

## 已落地规则清单 (15 条内置 + 5 条示范)

| rule_id | 分组 | 判定内容 | SKILL.md 条款 |
| --- | --- | --- | --- |
| `rule_07_script_beat_count` | story_script | Broad Story Script beats 数量 4–14 | rule 7 |
| `rule_06_script_required_roles` | story_script | 按 teaser mode 分支的必备 beat role + 首 beat 结构 | rule 6 |
| `rule_06_script_end_hook` | story_script | `may_be_empty=false` 时末 Beat 必须为 `end_hook` | rule 6 |
| `rule_08_story_granularity_broad` | story_script | Story 产物必须携带 `story_granularity=broad` | rule 8 |
| `rule_04_beat_concrete_content` | story_script | Beat 必须含可观察具体内容, 不得抽象 Logline | rule 4 / 11 |
| `rule_12_thread_kind_required` | series_bible | 每条 Thread 必填 `thread_kind=arc\|coda` | rule 12 |
| `rule_12_coda_beat_limits` | series_bible | typed coda 仅 1–2 个 terminal Beat 且含 `phase=coda` | rule 12 |
| `rule_03_resolved_setup_payoff` | series_bible | arc + resolved 必须含 setup/payoff Beat | rule 3 |
| `rule_22_plan_duration_cap` | story_plan | 播放时长 ≤ 1200s 硬上限 | rule 7 / 22 |
| `rule_21_repeat_ratio_cap` | story_plan | `repeat_ratio ≤ 10%` 硬合同 | rule 21 |
| `rule_20_first_block_contract` | story_plan | 首 Block teaser/start/reuse_mode=none 固定; mode=none 无 Teaser | rule 20 |
| `rule_21_teaser_single_clip` | story_plan | Teaser 恰好 1 Clip; 同 Block 内不得复用 Candidate | rule 20 / 21 |
| `rule_22_full_episode_guard` | story_plan | ≥2 整集型 Clip 或整集播放占比 >50% 阻断 (>40% warning) | rule 22 |
| `rule_23_qc_admission_never` | qc_admission | `dialogue_incomplete`/`same_source_causal_gap`/缺 continuity 永不 Admission | rule 23 |
| `rule_32_render_transition_policy` | render_recipe | single_highlight 恰好一次 0.35s Teaser→正文黑场; mode=none 无 | rule 32 |

### validate() 表化示范 (5 组, 消息与旧实现逐字等价)

| rule_id | 移植自 | 判定内容 |
| --- | --- | --- |
| `va_unique_ids` | `unique_ids()` | 记录 ID 非空且唯一 |
| `va_check_refs` | `check_refs()` | ID 列表引用完整性 |
| `va_thread_beat_accounting` | `thread_beat_accounting_findings()` | Catalog 子弧 Thread Beat 归账 + F4 扩容 |
| `va_abstract_beat_content` | `is_abstract_only()` beat 循环 | Beat 内容不得只有抽象描述 |
| `va_script_role_structure` | validate() 内联 role 检查 | beat role 结构 (teaser mode 分支) |

先以快照固化旧实现行为 (输入→违规输出), 再对新规则做同输入 diff。

## 逐条状态 (SKILL.md 固定合同 1–36)

| 条款 | 主题 | 状态 | rule_id |
| --- | --- | --- | --- |
| rule 1 | 全剧理解先于故事选择, 脚本先于选段 (流程顺序) | 文字约定 | — |
| rule 2 | 模型/后端固定 (qwen3.7-plus/max、DashScope Header、并发对齐) | 文字约定 | — |
| rule 3 | 摄取覆盖 + 叙事覆盖双证明; 整集归账 | 部分落地 | `rule_03_resolved_setup_payoff` |
| rule 4 | 引用真实 Event/Fact ID, 不编造 | 部分落地 | `rule_04_beat_concrete_content`, 示范 `va_check_refs` |
| rule 5 | Treatment 编译合同 (三策略、重试预算、audit schema) | 文字约定 | — |
| rule 6 | Story 中心结构 + teaser mode 分支合法性 | 已落地 | `rule_06_script_required_roles`, `rule_06_script_end_hook` |
| rule 7 | beats 4–14; 可行性三档; 1200s 硬顶 | 已落地 | `rule_07_script_beat_count`, `rule_22_plan_duration_cap` |
| rule 8 | Story 间允许重叠 Event; broad 身份标记 | 已落地 (标记部分) | `rule_08_story_granularity_broad` |
| rule 9 | 未批准 Story 不得进入下游 | 文字约定 | — |
| rule 10 | draft→awaiting_approval; repair 只删不改语义 | 文字约定 | — |
| rule 11 | Beat 五要素 (内容/must-show/观众知识/因果/检索) | 部分落地 | `rule_04_beat_concrete_content` |
| rule 12 | Catalog 发现配额; thread_kind; typed coda | 部分落地 | `rule_12_thread_kind_required`, `rule_12_coda_beat_limits` |
| rule 13 | Evidence 只处理批准且哈希有效的 Story; partially_ready | 文字约定 | — |
| rule 14 | 结构化 ID + 相邻窗口, 不引入向量库 | 文字约定 | — |
| rule 15 | Evidence Packet 三层范围分流 | 文字约定 | — |
| rule 16 | Span tight/scene/context; provenance_tiers; full_source_like | 文字约定 | — |
| rule 17 | 编译器只输出 proposed/needs_video_review | 文字约定 | — |
| rule 18 | Span Candidate 不可变, 无全局 selected/rejected | 文字约定 | — |
| rule 19 | Legal Option Compiler 前置; finalist ≤3; orientation 编译 | 文字约定 | — |
| rule 20 | 首 Block 固定字段; 哈希绑定物化 | 已落地 (结构部分) | `rule_20_first_block_contract` |
| rule 21 | Teaser 单 Clip; 同 Block 不复用; repeat_ratio ≤10% | 已落地 | `rule_21_teaser_single_clip`, `rule_21_repeat_ratio_cap` |
| rule 22 | must-have 覆盖; 1200s; 整集率; continuity | 部分落地 | `rule_22_plan_duration_cap`, `rule_22_full_episode_guard` |
| rule 23 | QC Admission 门禁; 永不放行名单 | 已落地 (名单部分) | `rule_23_qc_admission_never` |
| rule 25 | Junction Edit 受控例外 (reviewed_bridge / right_av_overlap) | 文字约定 | — |
| rule 27 | 每 Story 一份正式 story-qc 报告 | 文字约定 | — |
| rule 28 | Span/基础 Plan 不可变; 派生 Plan 哈希链 | 文字约定 | — |
| rule 29 | Boundary Repair 只一轮; 只向外扩 | 文字约定 | — |
| rule 30 | approved/review/blocked 接纳策略 | 文字约定 | — |
| rule 31 | 正式渲染使用 QC 绑定的有效 Plan | 文字约定 | — |
| rule 32 | Teaser→正文黑场唯一性; mode=none 无转场 | 已落地 | `rule_32_render_transition_policy` |
| rule 33 | 本地原片; 1080×1920/25fps/H.264/AAC | 文字约定 | — |
| rule 34 | Recipe/MP4/报告 SHA-256 绑定链 | 文字约定 | — |
| rule 35 | 正式渲染范围排除项 | 文字约定 | — |
| rule 36 | Filler tail 300s 兜底 | 文字约定 | — |

## 遗留 TODO (按优先级)

1. **SHA-256 绑定链类** (rule 13/28/34): 需要在引擎中引入"多产物 + 哈希"
   payload 约定 (artifacts 传入多产物, 规则比对 input_shas), 建议与
   ArtifactBus 的 bindings() 对接后再落地。
2. **continuity/junction 类** (rule 22 的 gap/对白截断、rule 25/29/32 的
   junction 效果态): 依赖时间码区间运算工具, 先落 `rule_22_same_source_gap`
   (同源相邻 Clip 正向 gap ≤12s)。
3. **Evidence/Span 分流类** (rule 15/16): `rule_15_fact_not_promoted`
   (Fact 关联 Event 不得进 direct 证据)、`rule_16_context_span_no_support`
   (纯 context Span 不得声称支撑 Beat)。
4. **QC 报告聚合类** (rule 24/26/27): findings code 闭合枚举校验
   (`STORY_VIDEO_QC_FINDING_CODES`) 适合做成规则, 需要先迁移枚举到核心层。
5. **Render 兜底类** (rule 36): `rule_36_filler_tail_target` (filler_tail_seconds
   账目 + 300s 目标), 需要 Render Recipe 的 filler 字段约定。
6. **流程顺序类** (rule 1/9/10/19): 属编排时序约束, 适合在 orchestrator
   层以"前置 Stage 状态检查"形式落地, 而非产物规则。
