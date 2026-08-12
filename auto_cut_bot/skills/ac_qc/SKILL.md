---
name: ac_qc
description: 质量检查 — Story QC（Coverage/Flow/Cut Safety 代理复核），覆盖 Stage 23-24。用于在 Story Plan 通过验证后，对每个 Candidate 执行代理渲染与多模态复核，决选唯一 Winner 进入正式渲染。
version: 0.1.0
stages: [23, 24]
status: active
triggers:
  - "Story QC"
  - "质量检查"
  - "QC 代理"
  - "音频边界修复"
  - "Boundary Repair"
  - "story_qc"
  - "boundary_repair"
  - "Winner 决选"
  - "VAD 检查"
anti_triggers:
  - "渲染视频" → ac_render
  - "Story Plan 生成" → ac_plan_orchestration
  - "素材准备" → ac_source_prep
  - "故事脚本" → ac_story_generation
---

# ac_qc — 质量检查

对已通过 Plan Validator 的 Story Plan Candidate 执行代理渲染、Qwen 多模态复核和本地音频 Boundary Repair，决选唯一 Winner 发布给下游 Render。

## Stage Range

| Stage | Name | Description |
|-------|------|-------------|
| 24 | `story_qc` | Story QC（Coverage / Flow / Cut Safety）+ 代理渲染 + Qwen 多模态复核 |
| 25 | `boundary_repair` | 本地双路 VAD 音频 Boundary Repair（一轮 ≤12s 扩边 + fade_fallback） |

## 快速开始

Candidate Arena 模式：

```bash
python3 /absolute/skill/scripts/story_candidate_qc.py prepare \
  /absolute/job --backend qwen --candidate-rank 1 --allow-partial \
  --audio-boundary-python /absolute/job/.venv-audio-boundary/bin/python

python3 /absolute/skill/scripts/run_semantic_batch.py \
  /absolute/job/story-qc-candidate-batch.json \
  --backend qwen --workers auto --requests-per-minute 0

python3 /absolute/skill/scripts/story_candidate_qc.py assemble /absolute/job
python3 /absolute/skill/scripts/publish_story_plan_winners.py /absolute/job
python3 /absolute/skill/scripts/validate_story_qc.py /absolute/job
```

## 输入 / 输出

- **输入**：`story-plan-candidates/index.json`（status=ready_for_video_qc）、批准 Script、Source Manifest、本地下载源
- **输出**：`story-qc-candidates/index.json`、`story-plan-winner-selection.json`、`story-plans/index.json`、`story-qc/index.json`、`story-qc-review.md`、`story-qc-validation.json`、`story-boundary-repair.json`、`story-plan-repairs/round-01.*`
- **副作用**：派生 Plan 不覆盖基础 Plan；Winner 发布后更新正式 Story Plan Index

## 参考文档

| 文档 | 用途 |
|------|------|
| [references/qc-design.md](references/qc-design.md) | Story QC 设计：代理渲染、Qwen 复核、决选逻辑（Stage 24） |
| [references/boundary-repair.md](references/boundary-repair.md) | 音频 Boundary Repair：双路 VAD、扩边、fade_fallback（Stage 25） |
| [references/editorial-knowledge-integration.md](references/editorial-knowledge-integration.md) | 编导知识库融合：类型路由、黄金样例、规则边界 |
| [../../shared_contracts/references/editorial-knowledge/](../../shared_contracts/references/editorial-knowledge/) | 7 种剧集类型适配器（正面模板 + 错误反例） |
| [references/qc-rules.json](references/qc-rules.json) | QC 规则配置 |

## 合同规则

| Rule | 描述 | 状态 |
|------|------|------|
| 23 | QC Admission 门禁：`dialogue_incomplete`/`same_source_causal_gap`/缺 continuity 永不放行 | 已落地 |
| 24 | QC 职责划分：双路 VAD 并集、动态 strict Schema、代理硬切 | 部分落地 |
| 26 | 双路 VAD 判决、≤12s 扩边、fade_fallback | 部分落地（策略源） |
| 27-30 | 报告、Plan 不可变、一轮修复、接纳策略 | 文字约定 |

详见 [shared_contracts/references/contract-rule-matrix.md](../shared_contracts/references/contract-rule-matrix.md)。

## 关键约束

- **Coverage / Flow / Cut Safety** 由 Qwen `qwen3.7-plus` 复核，吞字/音节截断由本地双路 VAD 判决
- **Boundary Repair** 只一轮，≤12s 向外扩边；超出或不可扩边时记录 `fade_fallback`
- **Candidate Arena** 按 rank 分轮并行：`approved` 后早停，`review/blocked` 继续下一 rank
- **Winner 决选**：`approved` 优先；无 approved 时只允许全部 finding 为 `fade_fallback` 的 auto-safe review
- **音频引擎**：固定 Demucs 4.1.0 + Silero VAD 6.2.1 + ONNX Runtime 1.24.3

## 已知问题

- 不支持通用自由 J-cut/L-cut 或音视频分离边界
- 物理源文件从半句话开始/结束的情况无法自动修复，需人工复核
- 超过 12s 的扩边建议走 `fade_fallback`，不自动扩边

## 修改记录

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-08-07 | 从 v4 SKILL.md 拆分，初始化 Stage 24-25 |