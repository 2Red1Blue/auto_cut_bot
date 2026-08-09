# 合同规则索引（contracts-index）

> 本文件是 v5 合同规则索引，将 v4 SKILL.md（edit-autocut-compilations-v3）
> 的 36 条固定合同规则映射到 `autocut_core/contracts/` 的声明式规则引擎落地。
> 2026-08-05 由阶段 6（规则引擎落地）代理填充。
> 对齐文档：`material_skill_manager/ARCHITECTURE.md` §7.2（合同运行时校验设计）、
> `SKILL_REFERENCES_RESTRUCTURE.md` §4（36 条合同规则迁移映射）与同目录
> `contract-rule-matrix.md`（逐条状态矩阵 + 遗留 TODO）。

## 落地现状（截至 2026-08-05）

- **声明式规则引擎已落地**：`autocut_core/contracts/rules/`
  - `engine.py`：Rule / Finding / RuleReport / RuleEngine（注册、分组过滤、结构化报告）
  - `builtin.py`：**15 条声明式内置规则**（覆盖 SKILL.md Rule 3/4/6/7/8/12/20/21/22/23/32 的相关子项，见下表 rule_id）
  - `legacy_story_artifacts.py`：旧 validate() 5 组检查的表化示范（另 5 条规则）
  - 入口：`default_engine()`；`RuleReport.violations` 可直接并入 `Stage.validate()` 返回值
- **Rule 26（音频边界）**：独立运行时策略源 `autocut_core/contracts/audio_boundary.py`
  （双路 VAD 门禁 + fade_fallback 兜底；策略版本 `audio_boundary_policy=1.4`，
  见 `autocut_core/version.py::SCHEMA_VERSIONS`）
- 其余规则当前仍由 v4 旧脚本内的校验逻辑执行（strangler 迁移期，
  adapter Stage 子进程调用），尚未迁入 `autocut_core/contracts/`。
- 合同类型定义（Artifact / StageStatus / Checkpoint 等）：`autocut_core/contracts/types.py`。
- 等价性测试：`tests/unit/test_contract_rules.py`、`tests/unit/test_validate_rule_equivalence.py`。

## 规则索引表（rule_id ↔ SKILL.md 合同规则映射）

已落地规则定义于 `contracts/rules/builtin.py::BUILTIN_RULES`，摘要与
`Rule.description` 逐字对齐：

| rule_id | 对应 SKILL.md 规则 | 判定内容（摘要） | 状态 |
|---------|--------------------|------|------|
| `rule_03_resolved_setup_payoff` | Rule 3 | arc 且 resolved 的 Thread 必须含 setup 与 payoff Beat | ✅ 已落地 |
| `rule_04_beat_concrete_content` | Rule 4 / 11 | Beat 必须含可观察具体内容，不得是抽象 Logline | ✅ 已落地 |
| `rule_06_script_required_roles` | Rule 6 | 按 teaser mode 分支的必备 beat role + 首 beat 结构 | ✅ 已落地 |
| `rule_06_script_end_hook` | Rule 6 | `ending_hook_intent.may_be_empty=false` 时末 Beat 必须为 `end_hook` | ✅ 已落地 |
| `rule_07_script_beat_count` | Rule 7 | Broad Story Script beats 数量为 4–14 | ✅ 已落地 |
| `rule_08_story_granularity_broad` | Rule 8 | Story 产物必须携带 `story_granularity=broad` 身份标记 | ✅ 已落地 |
| `rule_12_thread_kind_required` | Rule 12 | 每条 Thread 必填 `thread_kind=arc\|coda` | ✅ 已落地 |
| `rule_12_coda_beat_limits` | Rule 12 | typed coda 仅 1–2 个 terminal Beat 且至少一个 `phase=coda` | ✅ 已落地 |
| `rule_20_first_block_contract` | Rule 20 | 首 Block 的 teaser/start/reuse_mode=none 由编译器固定；mode=none 不得出现 Teaser Block | ✅ 已落地 |
| `rule_21_repeat_ratio_cap` | Rule 21 | Partition 最终硬合同 `repeat_ratio ≤ 10%` | ✅ 已落地 |
| `rule_21_teaser_single_clip` | Rule 20 / 21 | Teaser 有且只有一个 Clip；同 Block 内不得复用 Candidate | ✅ 已落地 |
| `rule_22_plan_duration_cap` | Rule 7 / 22 | Story Plan 不设时长下限，只保留 1200 秒硬上限 | ✅ 已落地 |
| `rule_22_full_episode_guard` | Rule 22 | ≥2 条整集型 Clip 或整集播放占比 >50% 阻断（>40% warning） | ✅ 已落地 |
| `rule_23_qc_admission_never` | Rule 23 | `dialogue_incomplete`/`same_source_causal_gap`/缺 continuity 合同的 Plan 永远不得 Admission | ✅ 已落地 |
| `rule_32_render_transition_policy` | Rule 32 | single_highlight 恰好一次 0.35s Teaser→正文黑场；mode=none 不生成 | ✅ 已落地 |
| Rule 26（音频边界） | Rule 24 / 26 | 双路 VAD 门禁 + 一轮 ≤12s 扩边 + fade_fallback 兜底 | ✅ 已落地（`audio_boundary.py::AudioBoundaryPolicy`，策略源而非规则表） |
| 其余规则（1/2/5/9/10/13–19/25/27–31/33–36） | — | 文字约定，尚未机器化；优先级清单见 `contract-rule-matrix.md` §遗留 TODO | ❌ 未迁移 |

### validate() 表化示范规则（`legacy_story_artifacts.py`）

旧 `validate_story_artifacts.validate()`（1346 行）中 5 组纯数据检查的
声明式移植，消息与旧实现逐字等价（等价性见
`tests/unit/test_validate_rule_equivalence.py`）：

| rule_id | 移植自 | 判定内容 | 状态 |
|---------|--------|----------|------|
| `va_unique_ids` | `unique_ids()` | 记录 ID 非空且唯一 | ✅ 已落地（示范） |
| `va_check_refs` | `check_refs()` | ID 列表引用完整性 | ✅ 已落地（示范） |
| `va_thread_beat_accounting` | `thread_beat_accounting_findings()` | Catalog 子弧 Thread Beat 归账 + F4 扩容 | ✅ 已落地（示范） |
| `va_abstract_beat_content` | `is_abstract_only()` beat 循环 | Beat 内容不得只有抽象描述 | ✅ 已落地（示范） |
| `va_script_role_structure` | validate() 内联 role 检查 | beat role 结构（teaser mode 分支） | ✅ 已落地（示范） |

> 注：SKILL.md 的 36 条编号与规则引擎 rule_id 的对应关系以
> `contract-rule-matrix.md` 的逐条状态表为权威源；本表与其保持同步。
> 规则分组（group）：story_script / series_bible / story_plan /
> qc_admission / render_recipe / legacy_validation。

## 维护约定

1. 新增/修改合同规则：先在 `autocut_core/contracts/` 实现校验函数，再更新本索引（编号连续递增，version 标记于本文件）。
2. 本索引与 `edit-autocut-compilations-v3/SKILL.md` 的 36 条规则一一对应；v4 语义描述见 v4 历史文档 `work_ai/docs/docs/11-schema-contracts.md`。
3. `id-schemas.md`（ID 规范索引）同为待建占位：当前 ID 模式权威源为 `autocut_core/schema/ids.py`，v4 描述见 `work_ai/docs/docs/11-schema-contracts.md`。
