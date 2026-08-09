# Span Candidate Compiler

## 目的

把 Story Evidence Packet 中的 Event/Candidate 粗范围扩展成可供 Story Plan
选择的候选原片段。候选必须保留完整语义片段、稳定身份、Beat 支撑关系和边界风险。
本阶段不决定最终使用哪个 Span，也不安排播放顺序。

## 命令

```bash
python3 /absolute/skill/scripts/compile_span_candidates.py \
  /absolute/job

python3 /absolute/skill/scripts/validate_span_candidates.py \
  /absolute/job
```

默认输入：

- `story-evidence/index.json`
- `story-evidence/<story-id>.json`

默认输出：

```text
span-candidates/
  index.json
  <story-id>.json
span-candidate-review.md
span-candidate-validation.json
```

## 编译规则

逐 Beat 对 Evidence Packet 的分层范围执行：

1. `direct_range_refs + candidate_range_refs` 生成 `tight/scene/context`；
   `context_range_refs` 只生成 `scene/context`。三层分别写入
   `provenance_tiers=direct|candidate|context`。
2. 按 Source 和时间排序，合并重叠或间隔不超过 1.5 秒的同层锚点。
3. 每个 Highlight/Hook Candidate 额外保留一个独立 Candidate-only `tight` 锚点，
   即使它与更粗 Event 重叠也不能被吞掉。
   - 仅当 Highlight Candidate 是 `teaser_contract` 指定的 primary Candidate 时，
     同时生成 `highlight_atomic` tight，并写入 `teaser_atomic=true` 与 owner ID：
     只吸收与原始 Candidate 锚点直接重叠的 Dialogue、Screen Text、Visual
     Event，保留完整锚点和直接重叠动作。
   - padding 与最多 2 秒 reaction tail 不得跨越已知的前一/后一语义边界，
     不得吸收仪式结果、下一 Story Beat 或后续对白。
   - `highlight_atomic` 是内部编译档，不新增 Schema 枚举；输出仍使用
     `variant_types=["tight"]`，并保留 Candidate provenance。
   - 原有通用 tight/scene/context 必须继续保留；atomic 只新增稳定 Span，
     不得修改或覆盖旧 Span。
   - 若 Teaser direct must-show Event 位于 primary Candidate 相邻范围，
     编译器必须复用 Script preflight 的联合算法：以 primary 为锚，每个
     must-show 选择一个直接 Event range，求 gap≤5 秒、联合≤15 秒的最短合法
     物理区间并生成 atomic Span。不得用经过语义裁边的普通 tight 代替原始
     Event range 做几何判定。
   - 联合区间与已有普通 Span 的 Source/起止相同时，必须在同一稳定 ID 上合并
     provenance 并升级 `teaser_atomic`/owner/stitch 字段；不得因已有 key、
     包含关系或普通 Span 先生成而跳过。
   - direct-event atomic 的句中风险必须与普通 Span 共用同一个
     Dialogue/Screen Text 边界计算（0.05 秒容差）。精确落在片段起止点
     必须记为非句中边界；真正落在片段内部时仍保留硬风险。
   - 每个 direct Event 还必须独立生成一个语义完整的 `tight` 候选；它与宽
     direct 组、`scene/context` 和 `continuity_closure` 并存，用于 Plan 在不丢失
     原子动作/对白的前提下做编辑压缩，不得用整段 Beat 包络代替。
4. 从 Window Summary 编译去重语义段：
   - Dialogue
   - Screen Text
   - Visual Event
   - Story Beat
5. 生成三种候选：
   - `tight`：保留锚点及完整对白、屏幕文字、动作和紧邻反应。
   - `scene`：继续扩展到相关 Story Beat 和局部场景上下文。
   - `context`：按 `continuity` 补充更宽的因果或连续场景上下文。
6. 扩边只按本轮固定初始包络选择语义段，禁止用新增边界再次递归吸收下一个
   相邻语义段。`tight → scene → context` 可以逐层变宽，但每一层内部不得链式扩张。
7. 把候选限制在 Evidence Packet 已召回的连续窗口范围和 Source 时长内。
8. 合并同 Source、同起止范围的重复候选，并汇总其 Beat、must-show、
   Event、Candidate 和变体类型。
   仅 direct/candidate provenance 可授予 Beat、must-show 和 Thread Beat 支撑；
   纯 context 候选的三类 `supports_*` 必须为空。
   must-show 的 `direct_event_ids` 是 AND 义务；单个 Span 只有包含该 must-show
   的全部 direct Event 才能写入 `supports_must_show_ids`。
   `fact_context_event_ids` 和兼容字段 `resolved_event_ids` 不具备 must-show
   支撑资格。多个 Span 的 Event 并集可在 Plan Block 层共同完成同一 must-show，
   Validator 必须按各层合同重算，不只做 ID 子集检查。
9. 为 `continuous_scene` 编译连续场景聚合候选：
   - 仅考虑同一 Source、同一 Evidence 连续组件内的 `scene/context` 候选。
   - 先按原始锚点组去重，再把物理范围相互重叠的锚点组组成连通分量。
   - 连通分量必须至少含两个不同锚点组，且 provenance 并集必须比任一单独
     锚点组增加真实 Beat、must-show 或 Thread Beat 支撑。
   - 聚合后的范围不得超过 `maximum_span_seconds`。
   - 聚合候选只合并成员已经证明的 Beat、must-show、Thread Beat、Event、
     Candidate、Anchor Ref、Semantic Segment、角色、时间位置和风险；不得
     因为最终物理范围包含某个事件就自动认领它。
   - 若聚合范围与既有候选的 Source、起止完全一致，只增强该稳定候选的
     provenance，不创建第二个 ID。
10. 所有普通 `tight` 在最大时长允许时必须从对白/屏幕文字内部向外吸附到
    完整语义边界；删除历史 `tight_narrow` 30 秒硬裁变体。完整语义单元超过
    Span 硬顶时可以保留为诊断候选，但必须带句中风险，后续 Legal Option
    不得选用。
11. 同一 Evidence 连续组件内、相邻锚点间距不超过 45 秒时，为
    `continuous_scene`、`causal_chain` 和 `montage_allowed` 编译
    `continuity_closure` 连续保底候选。Closure 只合并成员已经证明的功能
    provenance，不因物理包含自动认领新义务；其作用是保证碎片化组合被淘汰前
    已有可播放替补。

默认单个候选不超过 180 秒。锚点本身超过限制时不得删除锚点；保留并标记视频复核风险。

每个候选必须输出 `source_duration_seconds`、`source_coverage_ratio`、
`covered_timeline_segment_count` 和 `full_source_like`。先把候选内去重后的 Dialogue、Screen Text、Visual Event
与 Story Beat 区间并集除以候选时长，得到 `semantic_density_ratio`，再使用统一
分类：

- Source 覆盖率 `<85%`：`full_source_like=false`。
- Source 覆盖率 `≥85%`，且 Source `<180s`、语义密度 `≥75%`，并且候选只与
  一个去重 Timeline Segment 正向重叠：视为高密度短集单连续表演，
  `full_source_like=false`。
- 其余 Source 覆盖率 `≥85%`：`full_source_like=true` 并保留整集型风险。

Timeline Segment 缺失或候选横跨多个 Segment 时不得使用短源豁免；按整集型保守
分类。Validator 必须从当前 Evidence Window 重算 Segment 数量，不能信任产物字段。

相同 Source/起止范围的候选合并 provenance 后必须重新计算语义密度和分类；
编译器与 Validator 必须调用同一口径。这样不把短集完整语义场景误判为填充，
也不放过长集、低密度短集的整集搬运。

连续场景聚合不改变以下合同：

- `variant_types` 仍只使用 `tight`、`scene`、`context`，不新增 Schema 枚举。
- `causal_chain` 和 `montage_allowed` 不执行上述聚合。
- 聚合候选固定保留 `needs_video_review`，不得输出 `verified`。
- Story Plan 的普通同 Story 重叠禁令不放宽；聚合的目的正是提供一个可单选、
  结构化覆盖完整的连续候选。
- Legal Option Compiler 的 Pareto 与 Option cap 必须固定保留覆盖丰富的单 Span
  union frontier，不能因同组 duration 变体过多而只留下碎片化多 Span 组合。

## 稳定身份

`span_candidate_id` 只由以下字段确定：

```json
{
  "source_id": "ep012",
  "start": 128.4,
  "end": 176.2
}
```

不要把 Story ID、Beat ID、模型排序或生成时间写入身份。这样相同原片范围在不同 Story
中获得相同 ID，可显式识别跨 Story 复用。

当前 Bundle/Index schema 为 `1.4`，编译方法为
`semantic-window-boundary-v7-dialogue-boundary`；旧 v6 Bundle 不得作为当前
Plan 输入复用。

## 边界状态

- `proposed`：结构化扩边完成，未发现明确风险，但尚未经过视频级确认。
- `needs_video_review`：存在窗口中段、对白截断、屏幕文字、非线性返回、
  continuous scene 或最大时长截短等风险。
- `verified`：只允许后续定向视频复核写入。

当前编译器固定 `emits_verified_boundaries=false`，不得输出 `verified`。
`highlight_atomic` 候选固定为 `needs_video_review`，由后续 Story QC 确认动作
完整性；不得因其更短而提前宣称边界已验证。

边界依据包括：

- Source 起止点。
- Dialogue/Screen Text 起止点。
- Story Beat 起止点。
- Visual Event 起止点。
- 上下文 Padding。
- Evidence Window 可用范围。

## 覆盖合同

每个 Story Bundle 必须：

- 覆盖 Evidence Packet 的全部 Beat。
- 为每个 Beat 列出其 Candidate ID。
- 没有候选的 must-have Beat 将 Bundle 标为 `incomplete`。
- 全部候选均需视频复核时，将 Beat 和 Bundle 标为 `needs_video_review`。
- 保存 Evidence Index、Evidence Packet 和已批准 Story Script SHA-256。

## 硬停止点

生成并验证 Span Candidate 后停止。不得在本阶段：

- 选定最终 Span。
- 把 `proposed` 静默提升为 `verified`。
- 安排 Block 或 Clip 播放顺序。
- 处理 `teaser_reprise` 的最终复用范围。
- 生成 Story Plan、QC、转场或 MP4。
