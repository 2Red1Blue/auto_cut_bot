# Source Assembly Plan（正式 Story Plan）

## 目的

把已批准 Editorial Blueprint、所选 Story Treatment 和 Span Candidate Bundle
先确定性编译为合法
Teaser/正文 Option，由本地代码从全量合法 Partition 中保留最多 3 个质量合格且
风险隔离的 Candidate Plan。每个 Candidate 锁定唯一 Teaser/正文组合，正文时间
关系优先由本地 Beat/Episode/主线状态编译；只有语义歧义才按 Story 合并为一次模型
fallback。Plan 阶段不再多选一。全部 Candidate 独立通过
Plan Validator 和完整 Story QC 后，确定性 Winner Publisher 才发布唯一正式
`story-plans/index.json`。本阶段决定各候选使用哪些原片、按什么叙事顺序播放；
不验证真实视频边界，不执行转场渲染或正式 FFmpeg 成片。

## 命令

```bash
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
```

正式输出：

```text
story-plan-preflight.json
story-plan-candidates/
  index.json
  generations/<plan-generation-sha256>/<story-id>/<plan-candidate-id>.json
story-plan-candidate-review.md
story-plan-validation.json
```

`story-plan-preflight.json` 是 Qwen 调用前的正式确定性门禁；
`story-plan-batch.json`、`intermediate/story-plan-contexts/` 和
`story-plan-selection-results/` 是可重跑的内部批处理文件。Arena 完成时
`story-plans/index.json` 必须保持 `stale`；只有 QC 后 Winner Publisher 可以恢复
唯一正式 Plan Index。直接调用命令时不带 `--candidate-arena` 只保留旧单 Plan
兼容行为；生产 `run_pipeline.py` 显式启用 Arena。

## Legal Option Compiler

`prepare_story_stages.py plans` 必须先为每个 Story 编译：

- `legal_teaser_options`：每项恰好包含一个 primary `teaser_atomic` Span，
  覆盖首 Beat 全部 must-show，携带唯一 Highlight provenance，总播放时长不超过 15 秒。
- `legal_block_options`：覆盖一个或多个连续正文 Beat，包含这些 Beat 全部
  must-show，且由实际 Span 支撑 Beat 与 required Thread Beat。普通 required
  Thread Beat 在单个 Option covering-set 内闭包；scope expansion Thread Beat
  可保留 base/enriched Option，但完整 Partition 必须覆盖全部 Story-level
  required Thread Beat。

must-show 的 `evidence_event_ids` 是 AND 义务。单个 Span 可以独立覆盖整项；也可以
由同一 Block 的多个原子 Span 以 Event 并集共同覆盖，但不能把“命中任意一个
Event”误算成整项完成。Fact、Character、Relationship 与 whole Thread 的扩展
只用于 context recall，不参与这个功能覆盖判定。

Option 保存稳定 `option_id`、固定 Beat ID 集、固定 Span Candidate ID 集、
真实总时长、Coverage 和 Teaser 兼容关系。编译器优先保留局部 Option；存在
非整集型完整方案时，不向模型暴露同组的 `full_source_like` 方案。
`auto_scope_expansion` 中新增/附着的 Thread Beat 不再只是召回提示：Compiler
在基础 Beat/must-show covering set 之外继续搜索实际带
`supports_thread_beat_ids` 的扩展 Span，生成覆盖一个或多个扩展 token 的
enriched Option；基础 Option仍保留。这样扩容素材能进入功能 Option 池，又不会
因强迫单个 Block 一次覆盖全部历史扩容节点而违反 Span cap。

不存在合法 Teaser 时先读取脚本诊断：原始 Teaser 义务违反 single-highlight
契约或超过 15 秒时输出 `no_legal_teaser_option → story_script`；原始义务合法、
仅编译 Span 超限或缺失时输出 `no_legal_teaser_option → span_compiler`。任一正文
Beat 不存在完整 Option 时输出 `no_legal_block_option → span_compiler`。
两种情况都必须在 Qwen 请求前停止，并在 `story-plan-preflight.json` 保存
15 秒上限、必需 must-show、Highlight provenance、最短完整组合时长和 Span ID。
若最短组合超时，还必须保存 `excess_duration_seconds`、
`priority_recompile_span_candidate_ids`、每个候选在其余 Span 固定时的目标上限、
`recommended_origin_candidate_ids` 和
`recommended_recompile_profile=highlight_atomic`，供 Span Compiler 定向重编。

### Treatment 门禁

完整读取 [story-treatment.md](story-treatment.md)：

- `chronological_compression` 不编译 Teaser Option，正文从首 Beat 起播。
- `cold_open_no_reprise` 在模型请求前删除与任一合法 Teaser 物理重叠的正文
  Option；Event ID 相同但 source range 不重叠不构成重放。
- `cold_open_delayed_reprise` 只保留满足以下全部条件的 Partition：全部
  explanation Beat 位于第一次重放之前；中间至少存在合同要求数量的
  `thread_role=primary` 推进 Beat；所有实际重放都落在声明的 reprise Beat。

Delayed-reprise 合法性必须在 DFS 分支状态中追踪；不得先截断
500 个通用 Partition 再事后过滤。`MAX_LEGAL_BODY_PARTITIONS=500`
只对已通过 Treatment、重复率和其他硬合同的完整结果生效。
Plan preflight 必须把 Treatment 不可行路由到 `story_script`，不得误送
`story_scope`/F4。

Materializer 对同一合同做防御性复验，避免旧缓存或手工 Selection 绕过请求前门禁。

Plan context 固定使用 600,000 字符硬预算。Batch 的每个 job 保存
`context_chars/context_budget_chars`，超限在请求模型前失败。上下文不得重复发送
全量 Partition、全量 Body Options 和 `option_hints`；每个 Candidate 记录只保存它
锁定的 Option 与 Span 摘要。本地对全部锁定 Candidate 尝试 synthetic
orientation；明显倒序、返回主线、未来预演、并行线和正向连续关系不调用模型。
同一 Story 若一个或多个 Candidate 仍存在混合时间位置、Episode 缺失或定向线索
缺失，只生成一个 `story_plan_orientation_fallback` 请求并覆盖该 Story 的全部歧义
Candidate；不允许逐 Candidate fallback，也不允许任何请求在 QC 前选 Winner。

每个 Candidate 继续复用现有 `story_plan_selection` 响应结构，但动态 Schema 只含
一个 `finalist` 分支：唯一 `body_partition_id`、唯一兼容 Teaser 与精确长度的
`body_block_orientations`。`(story_id, plan_candidate_id)` 是独立 Plan/QC subject；
不得把 Candidate A 的正文、Teaser、orientation 或结果混入 Candidate B。
相同 `physical_span_sequence` 的 Partition 在进入 finalist arena 前确定性去重，
避免重复执行 QC。第一 Candidate 保持原质量排序；后续 Candidate 优先隔离
must-show Span、Junction、Beat Span、Block 分组和 continuity closure 风险，最后
才使用普通物理序列差异。每个 Candidate 本身仍执行原连续性、整集型和
功能边界质量排序：
`functional_evidence_coverage_ratio`、`functional_selection_precision_ratio`、
`nonfunctional_slack_seconds` 以及 head/tail slack。这些信号优先于每 40 秒至少
一条 Clip、Clip 中位时长优选 20–40 秒、目标时长距离和 Source 覆盖率。
只有 direct Event 范围缺失时该指标才保持中性，不用 Fact/context 范围猜测功能边界。偶数 Clip
使用标准统计中位数，即中间两项平均值。
旧的 Plan Comparison Proxy 在 Candidate Arena 中停用；真实视频比较由后续完整
Story QC 承担，避免用低成本局部代理提前淘汰可能避开 Coverage/Flow/Cut 风险的
合法 Candidate。

## 模型与本地代码边界

本地编译成功时直接生成当前锁定的复合 `finalist`。Story 级模型 fallback 只能为
动态 Schema 锁定的歧义 Candidate 输出 orientation 数组，不能选择 Candidate、
Body 或 Teaser。最终每个 Candidate selection 仍包含：

- 当前 finalist 内的合法 Teaser Option ID（逐字复制）。
- 当前 finalist 内的 Body Partition ID（逐字复制）。
- 与该 Body Partition 的 Block 数精确相等的时间关系和观众定向策略。
- 选择理由和规划风险。

选择模型不得输出或改写：

- Beat ID、Span Candidate ID、Block role、首 Block 的时间关系和复用声明。
- `source_id`、Episode。
- 原片起止时间码或时长。
- Event/Candidate ID 映射。
- 边界状态。
- 转场和编码参数。

每个 Story 的请求必须使用动态 `json_schema + strict=true`。`story_id`、
`production_slot` 使用当前 Story 的 `const`；每个复合 finalist 分支的 Body、
Teaser 使用 `const`，orientation 数组同时固定 `minItems=maxItems=segment_count`；
首 Block 字段不进入模型输出。完整动态 Schema 必须进入请求签名，防止旧缓存跨
Story、跨 Span Bundle 或跨 Body/Teaser 组合复用。

Planning Contract v18 在 v17 的 local-first orientation 与 Story-level
ambiguity fallback 之上，补齐 `mode=none` Body-only 重复率请求前门禁：
同一 Treatment 内的 Candidate 分别物化、分别验证、分别 QC，Winner 只能在 QC 后
确定；跨 Treatment 候选不进入同一 Arena。v15 的位置、Treatment、连续性、编辑
压缩与功能边界合同全部保持：
direct Event 保持可独立剪辑，compound must-show 按所选
Span Event 并集执行 AND 覆盖；covering-set cap 固定保留功能边界最紧的 frontier，
Partition 在 20–40 秒中位时长和目标时长之前先惩罚功能证据缺失与前后无关冗余。
single-highlight
的 `teaser_intent` 无论模型是否写 `must_have=true` 都属于结构必需 Beat；
`chronological_compression` 的全部正文 Beat 无论 `must_have` 值如何都属于
结构必需 Beat，并统一进入 Preflight、Compiler、Materializer、Validator 与
Coverage 口径；
首个结构必需正文 Beat 必须位于首个正文 Block。正文候选不得以
`starts_mid_sentence_risk/ends_mid_sentence_risk` 进入 Legal Option；相邻同源
Clip 若共享同一直接 Event 或 must-show 原子因果身份，gap 超过 12 秒时该
Partition 请求前淘汰。只共享宽粒度 Thread Beat 不构成同一原子因果单元；
两侧 direct Event/must-show 不同时，允许跳过中间无关 Event，并由后续 Junction/Flow QC
判断该剪辑是否叙事清晰，不得为了原片时码连续而选入中间剧情。违反这些关系的 Body
Partition 不进入 `body_partition_id` 枚举，因此模型无法选中一个 Schema
合法、展开后却因首 Beat 位置被 materializer 阻断的 Partition。

本地 Arena stage 为
`story-first-story-plan-candidate-orientation-local-v15`；歧义 fallback stage 为
`story-first-story-plan-orientation-fallback-v15`，每 Story 最多一个请求。逐 Candidate
selection 响应 Schema 仍为 `4.0`，fallback 响应 Schema 为 `1.0`；Legal Option Compiler 为
`story-plan-legal-option-compiler-v21-mode-none-repeat-parity`。旧单 Plan 兼容入口仍使用
selection stage/schema v13。stage、Planning Contract、动态 Schema、Span Bundle
或 Candidate 合同任一变化都必须产生新请求签名，
旧 Selection cache 不得跨合同复用。

Auto orchestrator 在 Plan preflight 前只允许一次旧 Span 恢复：仅当当前
Span Index 不是 schema `1.4` / method
`semantic-window-boundary-v7-dialogue-boundary` 时，复用现有
`span_candidates` 阶段重编一次并写入 `pipeline-auto.log`；当前 v7 generation
不得重复无效重编。v7 后仍无法证据安全压缩的连续表演不另起第二套 Plan，按
Script 的 `continuity_fallback` 写入现有 `editorial_density_diagnostics` 与
`editorial_density_reasons`，保留审计信号而不静默硬切。

每次 prepare 在写 context 前先发布当前 generation 的空 `stale` Index；
历史 Plan 文件保留但 inactive。Materialize 先完整写入不可变 generation 目录，
最后才原子替换根 Index；批处理中途失败时不得暴露混合代次。

`materialize_story_plans.py` 先按 Option ID 展开批准顺序内的 Beat 和稳定 Span，
确定性生成首 Block 的 `teaser/start/orientation_required=false/
orientation_strategy=none/reuse_mode=none`，再复制真实源字段，生成 Block、Clip、
Sequence Edge、Story-local Viewer Knowledge、Source Usage 和覆盖摘要。它同时
从当前 Span 语义与有效 Clip 时间码生成顶层 `continuity`，以及逐 Junction 的
`same_source_gap_seconds`、`continuity_status`、`continuity_findings`；
`dialogue_incomplete` 或 `same_source_causal_gap` 必须把 Plan 标为 blocked。
Validator 使用同一确定性函数从 Span Bundle/Clip 重建并逐字段比对，旧缓存或手工
修改不能绕过。

## Block 与 Beat

- 冷开场 Treatment 的首个 Block 固定为 `teaser`，只允许选择 Legal Teaser
  Option；顺叙 Treatment 的首个 Block 是正文。
- Teaser 总长优先 8–15 秒，超过 15 秒阻断。
- 正文 Option 数组必须把每个 must-have 正文 Beat 按批准顺序恰好分区一次。
- Block 内可包含多个连续叙事 Beat；一个 Span 也可同时支撑多个 Beat。
- Beat 展平后的顺序不得违背已批准 Story Script。
- 每个 Block 的首个 Beat 决定 Block role。
- 每个 Beat 和其 must-show 都必须被本 Block 已选 Span 支撑。
- 每个 `required_thread_beat_id` 必须被至少一个实际选中的 Span 的
  `supports_thread_beat_ids` 支撑。
- 已选 Span 不支撑本 Block 任何 Beat 时视为无功能填充并阻断。

Thread Beat 覆盖独立于 Editorial Beat 覆盖。即使 `beat-setup`、`beat-payoff`
都被 Span 支撑，只要中间必需桥接（例如 EP30 转折、EP31 揭示、EP32 后果）
没有原片支撑，Plan 仍必须 `blocked`。禁止用 EP27 直接跳 EP33 后宣称子故事完整。

非线性只描述原片时间关系，不允许静默改写已批准的故事叙事顺序。

## 时间关系与观众定向

首个 Block 由本地编译器固定使用 `start`。后续 Block 使用：

- `continuation`
- `flashback_context`
- `preview_future`
- `return_to_mainline`
- `parallel`

非线性关系必须指定 `dialogue_anchor`、`visual_anchor` 或 `title_card`；
本阶段只保存定向意图，不渲染标题卡或转场。

相邻 Block 若前一 Block 的最早集号仍晚于当前 Block 的最晚集号，属于确定性的
明显倒序跳转（例如 EP38→EP06）。此时只允许
`flashback_context + orientation_required=true + 非 none strategy`；
不得把它标成 `continuation`。Materializer 与 Validator 都必须执行此门禁，
即使模型错误输出 `continuation/false/none` 也只能得到 blocked Plan。

## Viewer Knowledge

本地代码在每个 Block 内按已批准的 Beat 顺序推进观众知识，并在 Block
输出中汇总：

- `before_fact_ids`
- `required_before_fact_ids`
- `introduced_fact_ids`
- `intentionally_withheld_fact_ids`
- `after_fact_ids`

每个非 Teaser Beat 所需 Fact 必须在进入该 Beat 前已引入。同一 Block
中，较早 Beat 可以隐藏某个 Fact，较晚 Beat 再引入它；该引入也可以满足
更后面的 Beat。只有真正支撑当前隐藏 Beat 的已选 Span，其 Event 命中
`must_not_reveal_fact_ids` 的证据 Event 时才判定提前泄露。同一个 Beat
同时引入和隐藏同一 Fact 仍必须阻断，不得把剧透留给后续 QC 才发现。

## 时长与复用

- v4.13+ 撤除硬下限；只保留硬上限 **1200 秒**。
- 短就短：不为凑时长做无功能扩充。成片时长的兜底扩展由 render 阶段的
  filler tail 兜底（SKILL.md rule 36）在最后一 Clip 上执行，Plan 阶段
  不需要感知。
- 超过 1200 秒时 Plan 为 `blocked`。

跨 Story 允许使用相同原片。同 Story 内重复或重叠默认阻断，唯一例外是：

1. 更早 Block 是 Teaser。
2. 后续 Block 返回正文。
3. 后一次选择显式使用 `teaser_reprise`。
4. `reprise_adds_information` 具体说明正文新增信息。

该例外仍受以下硬限制：

- 同一个 `span_candidate_id` 允许在一个 Story 内的多个 Block 中出现——
  既可以是 Teaser↔正文的 `teaser_reprise`，也可以是不同正文 Block 之间的
  复用（v4.5+ 新放开）。同 Block 内部不得复用，Teaser Block 有且只有一个 Clip。
- Teaser 与正文、正文之间的重复不限于时间重叠：既可以是两个不同 Span 的
  部分重叠，也可以是完全相同 Span 的整段复用。两种情况都按其源片时长
  计入下面的总重复预算。
- 全片最终重复率不超过播放时长的 10%。60 秒只作为 DFS 搜索剪枝上界，
  不是最终绝对秒数判决。Legal Option Compiler 与 materializer 使用同一公式：
  `repeated = playback_duration − merged_unique_source_duration`。
  `single_highlight` 计算 Teaser+Body；`mode=none` 计算全部 Body Span。
  10% 只在完整 Partition 上判定，因为后续新增不重复素材可能降低比例。
  不得用 pairwise overlap 累加替代全局区间并集，也不得固定预留 10 秒 Body 预算。
- `full_source_like` 读取 Span Compiler 的联合分类，不在 Plan 阶段自行按覆盖率
  重算：覆盖 ≥85% 通常为整集型，但 Source `<180s`、候选语义密度 `≥75%`
  且只覆盖一个 Timeline Segment 的高密度短集单连续表演豁免。候选池较窄不再
  放宽单 Story 最多一个整集型 Clip 的上限。
- 两条及以上整集型 Clip，或整集型播放占比超过 50%，Plan 阻断。
- v4.13+ 撤除 `insufficient_editorial_surplus` / 编辑余量比例门禁；候选素材
  总量作为观测指标继续输出，但不再阻断 Plan。`expand_story_scope.py` 只在
  `no_legal_body_partition`（结构缺口）时触发，不为凑时长扩展。

## Editorial Metrics

本地物化必须输出 `editorial_metrics`，至少包含：

- 播放时长、候选可用去重时长和实际选择去重时长。
- 重复秒数与重复率。
- 整集型 Clip 数量、播放秒数与占比。
- Teaser 时长、Clip 数、Clip 中位时长。
- `preferred_minimum_clip_count`、20–40 秒中位时长软目标、
  `editorial_density_status/reasons` 与 continuity fallback 审计。
- 高光前置状态。
- 编辑余量秒数（v4.13+ 仅作观测，`insufficient_editorial_surplus` 恒为 false）。

这些指标由本地代码根据不可变 Span 计算，模型不得自报。

## 失败路由

`repair_routes` 必须指向真正需要重跑的上游：

- Teaser 定义不是未来高光 → `story_script`。
- must-show 证据未召回 → `story_evidence`。
- 没有细粒度 Highlight/正文 Candidate → `span_compiler`。
- 原始 atomic obligation 超过 15 秒或违反 single-highlight 契约 → `story_script`；
  direct Event 必须在正文完整重现且联合义务超过 60 秒 DFS 剪枝预算同样 →
  `story_script`；
  原始义务 ≤15 秒但编译 Span 超限/缺失 → `span_compiler`，不得调用 Qwen。
- 任一正文 Beat 没有 must-show/Thread Beat 完整 Option → `span_compiler`。
- Teaser 超时且证据已齐时，优先为诊断指定的 Highlight provenance 新增
  `highlight_atomic` tight Span；保留原宽 Span，不得原地改时间码、放宽 15 秒
  上限或让模型自由裁剪。
- 选择重复、重叠、整集型或 Block 编排错误 → `story_plan`。
- 结构缺口（Legal Option Compiler 报出 `no_legal_body_partition`——body
  beats 集中在 1–2 集导致无合法 partition，或 required Thread Beat 缺失
  等）→ `story_scope`。由 `expand_story_scope.py` 承担。v4.13+ 撤除时长
  下限后不再触发时长驱动的 `insufficient_editorial_surplus`；`expand_story_scope`
  只处理结构缺口。两个入口（`--source plan_preflight` 主要触发、
  `--source script_preflight` 兜底）共用同一命令与
  `auto_scope_expansion[]` 审计条目。

修复反馈不得硬编码"在顺序 1、顺序 2 重播同一 Span"的布局。

## 状态与哈希

单 Story Plan 状态：

- `ready_for_video_qc`
- `blocked`

Portfolio Index 状态：

- `ready_for_video_qc`
- `partially_ready`
- `blocked`

`--allow-partial` 只允许跨层合法的不物化：Evidence 仍覆盖全部 approved Story；
Span Bundle 覆盖 Evidence 非 `incomplete` 的 Story；Plan batch 与 Plan 覆盖
Plan preflight 为 `ready` 的 Story。Evidence-ready Story 缺 Span、或
preflight-ready Story 缺 batch/Plan 仍是错误。只物化 approved 子集时，单 Story
Plan 可以是 `ready_for_video_qc`，但 Portfolio Index 必须是
`partially_ready`。

Candidate Arena 不删除或重排已物化的 blocked Candidate；Validator 保留其
Plan、原 rank、typed `blocked_reasons` 和 `repair_routes`，Candidate QC 以
`plan_validation_blocked` 隔离。每个 Story 有至少一个 ready Candidate 时整个
Arena 可继续；`--allow-partial` 可把零 ready Candidate 的 Story 作为拒绝项省略。
缺文件、哈希陈旧、Schema/身份错误、不能确定性重物化或 generation 漂移
仍使整个 Arena 失败。

每次 `prepare plans` 计算 `plan_generation_sha256`，绑定 Approval、Evidence
Index、Span Index、preflight、compiler version 与 planning contract version。
Preflight、batch、context、Plan fingerprint、Plan Index 和 validator 必须属于
同一 generation。没有 `--allow-partial` 且任一 Story 阻塞时仍必须原子写入当前
`jobs=[]` batch、把 `story_plans` stage 与旧 validation 标为 stale，然后再报错；
旧 Plan 文件可留作历史，但 active ready count 固定为 0。每份 Plan 另绑定
Portfolio、Story Script、Evidence Packet、Span Bundle 和模型选择结果 SHA-256。
任一输入变化后，旧 Plan 失效。

Coverage 同时输出：

- Editorial Beat 与 must-show 的 required/covered/uncovered。
- Thread Beat 的 `required_thread_beat_ids`、`covered_thread_beat_ids` 和
  `uncovered_required_thread_beat_ids`。

Story QC 的本地音频 Boundary Repair 可以从当前有效 Plan 派生新版本，但必须：

- 保留基础 Plan 不变。
- 只执行一轮，并且只对同一 Clip 做不超过 12 秒的向外扩边；超过预算、源边缘
  活跃语音或复检仍不安全时记录 `fade_fallback`，方向/数据/Plan 硬合同错误仍
  `blocked_replan`。
- 保存父 Plan、音频证据、Patch 和派生 Plan 的完整 SHA-256 链。
- 重新计算时长、重复率、整集率、Source Usage、Editorial Metrics、顶层
  `continuity` 与逐 Junction gap/status/findings；裁尾若制造新的连续性硬错误，
  Patch 不得落盘。
- 重新通过音频门禁与受影响的 Junction/Story Flow QC。

已物化但为 `blocked` 的 Plan 可由审核人通过
`story-plan-qc-admission.json` 明确接受可放行风险，仅放行到 Story QC。基础 Plan
状态和指标不变；Admission 精确绑定 Plan 与 blocked reasons 哈希。
`dialogue_incomplete`、`same_source_causal_gap` 或缺少当前 continuity contract 的
旧 Plan 永远不可 Admission；Legal Option preflight 未生成时间轴的失败同样不属于
可放行对象。

派生 Plan 是 QC 的有效输入，不反向冒充模型选择阶段的确定性基础 Plan。

## 硬停止点

验证完成后停止。不得在本阶段：

- 把 `proposed` 或 `needs_video_review` 提升为 `verified`。
- 生成独立 Planning Intent 业务实体。
- 运行 Selected Video QC。
- 在初始 Plan 阶段生成 Boundary Patch；Boundary Repair 只允许由后续 Story QC
  根据本地音频报告触发。
- 选择或渲染转场。
- 生成 Render Recipe 或 MP4。
