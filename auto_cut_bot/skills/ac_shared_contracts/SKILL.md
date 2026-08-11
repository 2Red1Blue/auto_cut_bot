---
name: ac_shared_contracts
description: 共享合同 — 跨模块的合同规则定义、声明式规则引擎、Schema 版本策略、ID 规范和 Pipeline 运行模式。不拥有任何 Stage，被所有 6 个 Stage 技能共同引用。用于查询合同规则落地状态、理解跨 Skill 的校验链和产物依赖关系。Pipeline automation skill for auto cut bot.
metadata:
  auto_cut_bot:
    emoji: "📜"
    always: false
version: 1.0.0
status: active
shared: true
stages: []
triggers:
  - "合同规则"
  - "contract rule"
  - "规则引擎"
  - "rule engine"
  - "Schema 版本"
  - "ID 规范"
  - "pipeline 模式"
  - "auto mode"
  - "interactive mode"
  - "产物依赖"
  - "哈希绑定"
  - "contract-rule-matrix"
anti_triggers:
  - "视频切窗" → 使用 ac_source_prep
  - "生成 Story Script" → 使用 ac_story_generation
  - "QC 检查" → 使用 ac_qc
tools:
  - db_query  # schema discovery + raw SQL
---

# shared_contracts — 共享合同

跨模块的合同规则定义、声明式规则引擎、Schema 版本策略和 ID 规范。不拥有任何 Stage，被所有 6 个 Stage 技能共同引用。是 36 条固定合同规则（v4 SKILL.md "固定合同" 章节）的落地状态权威源。

## 参考文档

| 文档 | 用途 |
|------|------|
| [references/contracts-index.md](references/contracts-index.md) | 36 条合同规则索引，rule_id 映射、落地现状、维护约定 |
| [references/contract-rule-matrix.md](references/contract-rule-matrix.md) | 逐条规则落地状态矩阵（已落地 / 文字约定 / 遗留 TODO） |
| [references/editorial-knowledge/](references/editorial-knowledge/) | 编导知识库（7 种类型适配器 + 通用合同 + 开场策略） |

## 规则引擎

`autocut_core/contracts/rules/` 已落地：15 条内置规则（覆盖 Rule 3/4/6/7/8/12/20/21/22/23/32）+ 5 条示范规则（旧 validate() 移植）+ Rule 26 策略源（`audio_boundary.py`）。入口：`default_engine()` → `RuleReport.violations`。约 18 条规则仍为文字约定。

## 合同规则速查

| 分组 | 已落地 | 文字约定 |
|------|--------|---------|
| story_script | rule_04, rule_06, rule_07, rule_08 | rule 5, 9, 10, 11 |
| series_bible | rule_03, rule_12 | rule 1 |
| story_plan | rule_20, rule_21, rule_22 | rule 13-19 |
| qc_admission | rule_23 | rule 24, 25, 27-30 |
| render_recipe | rule_32 | rule 31, 33-36 |
| cross-cutting | rule 2（模型/后端固定） | — |

## Pipeline 运行模式

- **Interactive**（默认）：逐节点复核，人工决策节点停下并打印下一步命令。`python3 scripts/run_pipeline.py /absolute/job --mode interactive --stage-from source_windows --stage-to story_render`
- **Auto**（全自动）：按 feasibility 自动决定审批、QC 只接受 approved 或 auto-safe review、opt-in flag 默认全开。每个决策 append 到 `pipeline-auto.log`。`python3 scripts/run_pipeline.py /absolute/job --mode auto --stage-from story_approval --stage-to story_render`

## 产物依赖链

跨 Stage 的 SHA-256 绑定规则：

- Story Script / Treatment 变化 → Evidence、Span、Plan、QC、Render 缓存全部失效
- Span Bundle / Approval 变化 → 必须重新物化 Story Plan
- Story Plan 变化 → 旧 Proxy、QC 报告失效
- 源文件 / VAD 策略变化 → 旧音频结论失效
- Boundary Patch 变化 → 修复链和下游 QC 全部失效
- Recipe 哈希不变且缓存媒体通过流检查 → 可复用渲染缓存

## 已知问题

- 约 18 条规则仍为文字约定（主要在 Evidence/Span/QC/render 环节）
- 遗留 TODO：SHA-256 绑定链类规则（rule 13/28/34）需引入多产物哈希约定
- 流程顺序类规则（rule 1/9/10/19）适合在 orchestrator 层以前置 Stage 状态检查落地

## 修改记录

| 版本 | 日期 | 变更 |
|------|------|------|
