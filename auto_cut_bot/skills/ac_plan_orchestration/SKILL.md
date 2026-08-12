---
name: ac_plan_orchestration
description: 计划编排 — Story Evidence 检索、Span Candidate 编译、Plan Preflight、正式 Story Plan 生成、Plan Materialize 与 QC Admission。覆盖 Stage 17-22，产出可进入 QC 的正式 Story Plan。
version: 0.1.0
stages: [17, 18, 19, 20, 21, 22]
status: active
triggers:
  - "生成计划"
  - "Story Plan"
  - "story_plans"
  - "story_evidence"
  - "证据检索"
  - "span_candidates"
  - "Span 编译"
  - "plan_materialize"
  - "plan_preflight"
  - "plan_validation"
  - "QC Admission"
anti_triggers:
  - "生成故事" → 使用 ac_story_generation
  - "QC 检查" → 使用 ac_qc
  - "渲染视频" → 使用 ac_render
---

# 计划编排 (ac_plan_orchestration) — Stage 18-23

从已批准的故事脚本中检索原片证据、编译 Span Candidates、生成正式 Story Plan，经 Legal Option Compiler 和 Preflight 校验后交付 QC。

## Stages

| Stage | Name | Description | Status |
|-------|------|-------------|--------|
| 18 | `story_evidence` | Story Evidence Packets（三层原片范围） | active |
| 19 | `span_candidates` | Span Candidate Compiler（tight/scene/context） | active |
| 20 | `plan_preflight` | Legal Option Compiler + Plan Preflight | active |
| 21 | `story_plans` | 正式 Story Plan 生成（模型 fallback） | active |
| 22 | `plan_materialize` | Plan Materialize（展开 Option → Clip） | active |
| 23 | `plan_validation` | Plan Validation + QC Admission | active |

## Input Artifacts

| Artifact | Source | Required |
|----------|--------|----------|
| Story Approval | ac_story_generation | yes |
| Story Scripts | ac_story_generation | yes |
| Story Portfolio | ac_story_generation | yes |
| Series Bible | ac_series_knowledge | yes |
| Event Cards | ac_series_knowledge | yes |
| Highlight/Hook Catalog | ac_series_knowledge | yes |
| Source Manifest | ac_source_prep | yes |
| Window Manifest | ac_source_prep | yes |

## Output Artifacts

| Artifact | Path Pattern | Consumer |
|----------|-------------|----------|
| Story Evidence Packets | `story-evidence/index.json` | ac_plan_orchestration |
| Span Candidates | `span-candidates/index.json` | ac_plan_orchestration |
| Plan Preflight | `story-plan-preflight.json` | ac_plan_orchestration |
| Plan Candidates | `story-plan-candidates/index.json` | ac_qc |
| Story Plans | `story-plans/index.json` | ac_qc, ac_render |

## References

| Document | Description |
|----------|-------------|
| [references/evidence-design.md](references/evidence-design.md) | Story Evidence 检索：三层范围识别与证据包编译 |
| [references/span-compiler.md](references/span-compiler.md) | Span Candidate 编译：tight/scene/context 分层与 Teaser atomic 编译 |
| [references/plan-design.md](references/plan-design.md) | 正式 Story Plan 设计：Legal Option Compiler、Plan Materialize、Validation |
| [docs/degradation-strategy.md](../../docs/degradation-strategy.md) | 降级框架 |
| [docs/timeline-anchoring.md](../../docs/timeline-anchoring.md) | 时间锚定系统 |

## Contract Rules

| Rule ID | Description | Engine Status |
|---------|-------------|---------------|
| rule_13 | Story Evidence Retrieval 只处理人工批准且 Script/Portfolio SHA-256 有效的 Story | landed |
| rule_14 | Story Evidence 只使用结构化 ID 与相邻窗口，不引入向量库，不让模型猜时间码 | landed |
| rule_15 | Evidence Packet 将原片范围分为 direct_range_refs / candidate_range_refs / context_range_refs | landed |
| rule_16 | Span Candidate Compiler 的 tight 只使用 direct/candidate 锚点；Source 覆盖率 < 85% 固定 full_source_like=false | landed |
| rule_19 | Story Plan 调用模型前必须运行 Legal Option Compiler；保留最多 3 个 Candidate | landed |
| rule_22 | Story Plan 必须覆盖全部 must-have Beat、must-show 和 required Thread Beat；1200 秒硬上限 | landed |

## Quick Start

```bash
# 生成 Story Evidence Packets
python3 /absolute/skill/scripts/build_story_evidence_packet.py \
  /absolute/job/story-approval.json

python3 /absolute/skill/scripts/validate_story_evidence.py \
  /absolute/job

# 编译 Span Candidates
python3 /absolute/skill/scripts/compile_span_candidates.py \
  /absolute/job

python3 /absolute/skill/scripts/validate_span_candidates.py \
  /absolute/job

# 生成正式 Story Plans (Arena 模式)
python3 /absolute/skill/scripts/prepare_story_stages.py plans \
  --job-root /absolute/job \
  --backend qwen \
  --candidate-arena

python3 /absolute/skill/scripts/run_semantic_batch.py \
  /absolute/job/story-plan-batch.json \
  --backend qwen --workers auto --requests-per-minute 0

python3 /absolute/skill/scripts/materialize_story_plans.py \
  /absolute/job/story-plan-batch.json

python3 /absolute/skill/scripts/validate_story_plans.py \
  /absolute/job

# QC Admission（人工放行 blocked Plan）
python3 /absolute/skill/scripts/story_plan_qc_admission.py init /absolute/job

python3 /absolute/skill/scripts/story_plan_qc_admission.py decide /absolute/job \
  --story-id story-001 \
  --decision accepted_for_qc \
  --note "已人工复核并接受列出的非线性边界风险，仅批准进入 Story QC"
```

## Recovery

- Evidence 修复路由：`story_script`（原子义务超 15 秒）、`span_compiler`（编译 Span 超限/正文缺口）、`teaser_reprise_exceeds_repeat_budget`（联合区间超 60 秒）。
- 每个 Candidate 独立通过 Plan Validator 和完整 Story QC；确定性复验一致的 blocked Candidate 以 `plan_validation_blocked` 隔离。
- 零 Winner 时正式 Plan Index 保持 `stale` 且为空，修复代码或请求签名后可从 Candidate QC 重新准备。

## Version History

