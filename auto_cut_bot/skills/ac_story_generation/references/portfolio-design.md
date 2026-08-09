# Story Portfolio 与 Primary 分槽

## 目的

把 Catalog 输出的候选子故事排序、去重并按连续生产槽位分配为 Primary / Reserve。
Portfolio 只做选择，不决定剪辑讲法、不切子弧、不合并跨 Story；切子弧发生在 Catalog（由 Qwen
根据 `duration_contract` 输出 `source_thread_beat_ids/subarc_start_beat_id/
subarc_end_beat_id/required_bridge_beat_ids`）。

Portfolio 完成后由 [story-treatment.md](story-treatment.md) 的确定性 Compiler
为 Primary 与 Reserve Story 一次性编译顺叙、冷开场不重放、冷开场延迟重放等
讲法；Reserve 预编译不等于晋级，该层也不反向改变 Portfolio 选择。

## 时长合同（v4.13+）

- **仅硬上限 1200 秒**：Plan 阶段任何 Story 时长越过 1200 秒仍
  `blocked`；不再设最短时长下限。
- **不为凑时长做扩展**：Script preflight 的功能证据时长只作为观测输出，
  不再驱动 scope expansion 或分档。`expand_story_scope.py` 只在结构缺口
  （`no_legal_body_partition`）时触发，不为凑时长扩展。
- **成片时长兜底**：Plan 时长不足 300 秒时，由 render 阶段的
  filler tail（延伸最后一 Clip 到集尾）兜底，详见 SKILL.md rule 36 与
  [story-render.md](story-render.md)。

## Portfolio 阶段职责边界

Portfolio 消费 `story-catalog.json`，产出 `story-portfolio.json`。它：

- 按加权分数排序候选（覆盖、独特性、Highlight 相关性、素材充足度等）。
- 用近重复检查（Jaccard / 相同 payoff / 同 open question）把候选放进 Primary
  或 Reserve。
- 为实际 Primary 分配从 1 开始的连续生产槽位。
- 计算 `pairwise_similarity_checks` 与 `coverage_summary`。

Portfolio **不**做：

- 不切子弧（Catalog 已经切好）。
- 不合并两个 Story（本轮 non-goal）。
- 不向脚本追加 Thread Beat（这是 `expand_story_scope.py` 的职责）。

## Story Script 淘汰后的 Reserve 补位

原始 `story-portfolio.json` 在下游保持不可变。Primary 只有在 Story Script
经过有界修复后仍被类型化为正式 rejection，才会触发确定性的 Reserve 补位；
网络、限流、Provider 或传输故障不会触发。补位记录写入独立的
`story-portfolio-replenishment.json`，每个 Reserve 最多尝试一次，并绑定原槽位、
被替换 Story、缺失 Thread Beat、实际补回 Beat 与稳定 promotion fingerprint。
每个生产槽位只认补位链中的最后一任占用者：被替换 Primary/Reserve 的正式 rejection
继续保留用于审计，但不得再次进入 Treatment 重试、Script 生成、Preflight 或
Approval。Story Index、Validator 与 Approval 任一处发现同槽位多个活跃 Story 时
必须硬停止。

选择时从当前仍存活的 Story 重新计算原 Primary 覆盖集合中的缺口，依次优先补回
required、turn/reveal/payoff/consequence、非 coda、全量 Thread Beat。没有实际
补回任何缺口的 Reserve 不会为了凑数量而晋级。晋级后的 Reserve 仍必须完整经过
Treatment → Story Script → Preflight，不会直接进入审批。

## Broad Story granularity（v4.20+）

Broad 的 coda 不是“Beat 少”的同义词。只有 Registry 显式声明
`story_threads[].thread_kind=coda` 的末端尾声/框架揭幕/杀青或最终后果，且其
1–2 个 Beat 仅使用 `payoff|consequence|coda`、至少一个为 `phase=coda`，Compiler
才生成 coda Option。短 arc 即使只有 1–2 个 terminal Beat 也不得被自动推断成
coda；它必须补足合法闭合子弧或在上游重新建模。

Broad 是唯一受支持的生产 Profile。Catalog 必须显式携带
`story_granularity=broad` 并绑定同目录
`story-subarc-options.json` 的 SHA-256。Portfolio 重新校验每个
`subarc_option_id` 与 Beat/Event/Candidate 类型映射，然后使用 Coverage-first
选择：

1. 最大化 `importance=required` Thread Beat 覆盖（必须 100%）。
2. 最大化 turn/reveal/payoff/consequence 等局部兑现或状态变化覆盖。
3. 最大化非 coda Beat 覆盖（必须 ≥85%）与全量 Beat 覆盖。
4. 惩罚主体 Beat 重叠，再以 Catalog rank score 作稳定 tie-break。
5. 只有仍贡献未覆盖 Beat 且不构成近重复时，才为 42 集 7–10 的软目标补充
   Primary；不得复制 Story 凑数。

输出 `coverage_summary.thread_beat_coverage`，包含 required / non-coda /
all 三组总量、覆盖量、覆盖率和缺失 required ID。若素材可行候选无法满足覆盖
硬合同，Portfolio 输出 `status=blocked`、
`blocked_reason=broad_thread_beat_coverage_unmet`。Legacy 或缺少 Broad 身份标记
的旧 Catalog 不会被自动转换；必须从 Broad Story Catalog 重跑。F4 scope
expansion 也不作为扩容机制。

## thread_utilization 报告

Portfolio 为 Broad Primary 计算审计用 `thread_utilization`：

- `stories_per_thread` 列出每条 Bible Thread 被多少个 Primary Story 使用。
- `diversity_ratio` 为 Primary Story 数除以实际使用的 Thread 数。
- `underutilized_thread_ids` 标记 ≥8 Beat 但只派生一个 Primary 的 Thread。
- `unused_thread_ids` 标记 ≥3 Beat 且没有 Primary 的 Thread。
- `single_beat_terminal_thread_ids` 单列只有 1–2 Beat 且未使用的收尾/前置
  bookkeeping Thread。

该块在 Coverage-blocked 与 ready 两条路径都携带，供审批视图与调试直接读取；
它不另行改变 Coverage-first 的 ready/blocked 判定：

```json
"thread_utilization": {
  "diversity_ratio": 1.11,
  "stories_per_thread": {"thread-a": 2, "thread-b": 1, "thread-coda": 0},
  "underutilized_thread_ids": [],
  "unused_thread_ids": [],
  "single_beat_terminal_thread_ids": ["thread-coda"]
}
```

## Script Preflight 可行性判定（v4.13+）

`preflight_story_scripts.py` 只按结构完整性输出 `feasibility.status`：

- `feasible`：Teaser 契约、must-have Beat 与 Payoff 均结构性可覆盖。
- `partial`：可覆盖，但存在 partial 证据或边界需要人工视频复核。
- `not_feasible`：Teaser 合同失败、must-have Beat 缺失/冲突或 Payoff 不可用。

时长只作为观测输出（`estimated_source_duration_min/max_seconds`），
不再产生 `awaiting_scope_merge` 分档，也不产生 duration-driven failure code。

`story-script-preflight.json` 仍写出全 Story 列表（`failure_codes=[]`），
供 `expand_story_scope.py --source script_preflight` 消费；但 v4.13+ 该入口
在无结构缺口时通常无事可做。

## expand_story_scope.py 触发（v4.13+）

同一个命令、两个入口，都只处理结构缺口：

- `--source plan_preflight`：读 `story-plan-preflight.json`，处理
  `no_legal_body_partition`（body beats 集中在 1–2 集导致无合法 partition）。
- `--source script_preflight`：读 `story-script-preflight.json`；v4.13+
  Script preflight 通常不产生 expandable failure，该入口为空跑。
- `--source auto`（默认）：优先读 plan preflight，缺失回退 script。

被扩展的 Thread Beat 必须挂在 `setup`、`escalation` 或 `turn_or_reveal`；
没有兼容 Beat 时返回 `requires_story_script_revision`。默认 lookback 只做
`same_episode → earlier_episodes`；只有操作者明确传入
`--allow-whole-series` 时才允许自动扩大为 `whole_series`。

## 哈希与失效链

`expand_story_scope --apply` 修改 Story Script 后：

- Script SHA-256 变化 → Approval 的 `approved_script_sha256` 被清空，
  decision 变为 `revision_requested_auto_scope_expansion`。
- Approval 变化 → 下游 Evidence Packet / Span Bundle / Plan 全部失效，需
  重跑 Evidence Retrieval → Span Compiler → Plan preflight → Plan 物化。
- Portfolio SHA-256 **不变**。生产槽位、Primary / Reserve 结构保持稳定。

## 与其他文档的关系

- 完整读 [series-bible-schema.md](series-bible-schema.md) 了解 Thread Beat
  `importance=required|supporting|optional` 分级。
- 完整读 [story-script-schema.md](story-script-schema.md) 了解 Story
  Script 生命周期与 v4.13+ 三档可行性判定。
- 完整读 [story-plan.md](story-plan.md) 了解 Plan 阶段的
  时长与复用合同。
- 完整读 [story-render.md](story-render.md) 了解 Render 阶段的 filler
  tail 兜底。
