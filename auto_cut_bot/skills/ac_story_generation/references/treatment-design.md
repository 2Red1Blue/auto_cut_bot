# Story Treatment Options

## 目的

Story Treatment 位于 Story Portfolio 与 Story Script 之间，回答“同一个已选故事
用什么剪辑讲法呈现”。它只消费上游已经建立的 Story、Thread Beat、Event 与
Highlight Candidate，不重新发现剧情、不生成新事实，也不建立第二套因果图或证据
路由。

正式产物为 `story-treatment-options.json`。它绑定：

- `story-catalog.json`
- `story-portfolio.json`
- `series-bible.json`
- `highlight-hook-catalog.json`

四项输入均保存 SHA-256。任一输入变化后 Treatment 必须重编，不能沿用旧文件。

## 当前策略

每个 Primary 与 Reserve Story 至少有 `chronological_compression`；存在不超过 15 秒的合法
Highlight 时提供 delayed-reprise。只有 Highlight 不泄露 required Fact、且不覆盖
正文 mandatory Thread Beat 的完整 Event 集时，才额外提供 no-reprise：

1. `chronological_compression`
   - `teaser_mode=none`
   - 按已批准 Thread Beat 主顺序压缩重复信息。
   - 不生成 `teaser_intent`，从 `mainline` 正文开始。
2. `cold_open_no_reprise`
   - `teaser_mode=single_highlight`
   - 先展示一个合法 Highlight，再补全前因并继续向前。
   - 正文不得物理重放 Teaser 使用的原片区间。
3. `cold_open_delayed_reprise`
   - `teaser_mode=single_highlight`
   - 先展示 Highlight，完成全部 explanation Beat 后，至少推进一次
     `thread_role=primary` 的主线，再在声明的 reprise Beat 中重放。
   - 重放必须通过 `reprise_function` 说明新增信息或重新解释关系/后果。

`no_reprise` 的判定口径是最终 Span 的 `source_id + [start,end]` 物理重叠，不是
Event ID 相等。正文仍可召回同一个 Event 的后果或上下文，只要最终不重播开场区间。

## 主线与辅助线

编译器从现有 Catalog 子弧与 Series Bible Thread Beat 中确定
`primary_story_thread_id`，并为当前 Story 已声明的 Thread 标注：

- `primary`
- `integrated_support`
- `independent_secondary`

这些只是 Story 内的编辑职责，不是新的全剧叙事实体。Story Script 的每个 Beat
必须声明 `thread_role`；独立次线不得承担主转折、Payoff 或 Hook，结尾必须回到
主线。Delayed reprise 的“推进”只计算 `thread_role=primary` 的推进 Beat。

## 确定性边界

1. `compile_story_treatments.py` 只编译合法 Option、稳定 ID、推荐项和输入指纹。
   推荐不再是“存在 Highlight 就固定 delayed reprise”，而是基于当前已知证据做
   确定性风险排序：连续单主线且高光可从正文安全移除时优先 no-reprise；晚期
   payoff 且至少有一个主线推进位置时才优先 delayed；Highlight 命中 required
   Fact 或完整 required Beat、又没有安全冷开场时退回顺叙。该判断明确标记为
   `pending_span_compilation`，不提前声称物理 Plan 一定可行。
2. Story Script 动态 Schema 把 Treatment Option ID、strategy、mode 与
   reprise policy 绑定为同一个分支，模型不能混搭。
3. Script Preflight 重验 Treatment 文件、脚本哈希绑定、Beat 顺序和主/辅线职责。
4. Legal Option Compiler 在模型调用前：
   - 为 `no_reprise` 删除与 Teaser 物理重叠的正文 Option；
   - 为 `delayed_reprise` 只保留满足 explanation、主线推进与声明 reprise
     位置的 Partition。
   - 搜索节点或 Option 上限耗尽时只返回 `option_search_incomplete`；在搜索完成
     前不得追加 `no_partition_satisfies_treatment`，也不得把 Treatment 标成
     `infeasible`。
5. Materializer 再执行同一组防御性检查；非法选择只能得到 blocked Plan。
6. Script Preflight 在 Approval 前生成 `treatment_viability`：
   - `no_reprise` 的开场原片区间若被正文 Must-show 确定性强制重现，
     输出 `no_reprise_mandatory_body_replay`；
   - Highlight direct Event 若命中 Teaser 隐藏 Fact，输出
     `teaser_highlight_withheld_fact_conflict`；
   - 失败时只能从已编译 Option 中生成新 Story Script，重算 Script
     SHA/request signature，再创建新 Approval；不得原地改已批准 Script。
7. `story-treatment-attempts/<story-id>.json` 保存每个请求代次的
   Treatment ID、strategy、结果哈希、失败码、备选建议与最终选择。
8. Treatment 语义重试不得继续暴露三个 Option 的原始 `anyOf`。若失败码只涉及
   explanation/reprise 等可修结构，retry Schema 锁定当前 Option 一次；若当前
   Treatment 已被证明不可执行，则锁定 preflight 推荐且尚未失败的备选。已失败
   Option 不得重复，推荐项也失败后才使用顺叙兜底；没有未尝试 Option 时停止。
9. `beat_physical_compaction_required` 与
   `beat_event_thread_beat_mismatch` 不是 Treatment 不可行，而是当前 Script 对同一
   Treatment 的物理/身份编译失败。它们锁定当前 Option；普通/Treatment 语义重试
   与 compile repair 分别计数，因此 fallback Script 出现 compile-only 失败后仍有
   完整两轮修复。每轮 strict Schema 只返回失败 Editorial Beat 的 replacement，
   不再返回整份 Script；本地按原 Beat 顺序确定性合并，未失败 Beat 逐字段冻结。
   只有旧失败 Beat 原本位于 explanation/reprise 数组时，才允许把该引用映射到它
   自己的 replacement Beat。context `1.6` 的 direct-evidence contract、跨轮
   preservation contract 继续固定 Thread Beat、Fact、Local Payoff 和 must-show
   AND 义务；合并后重新执行完整 Script validator/preflight，超过 Broad 14 / Legacy
   11 Beat、弱化 must-show、改变顶层合同或错挂 Event/Thread Beat 均拒绝。连续错误
   签名不变时 `no_progress` 停止。compile-only 失败不得进入 Treatment carousel，
   也不得消耗其他已编译 Option；传输失败不消耗任一语义预算。
10. Auto 模式在第一次 Script Batch 与 Script Preflight 后、创建 Approval 前，
   汇总 batch-declared 与 preflight-declared 的 Treatment 失败，读取 attempt audit，
   通过可重复 `--target-story-id` 只为当前失败 Story 建 Job，并锁定尚未尝试的
   Option 生成新的 request signature。已成功、已淘汰、未审批和不在 active retry
   集合内的 Story 都不进入 Batch；较早恢复的 sibling 也不再重跑。只有达到成功、
   转为非 Treatment 修复路线或全部 Option 耗尽后才结束；最终失败才写入正式
   rejection。
   耗尽判断只统计当前 active Script stage version 的 generation 与带相同
   `script_stage_version` 的 recovery attempt；旧 stage 历史继续保留用于审计，
   但不得把新 stage 的 Option 提前标成已尝试。
   只有带 `disposition=rejected` 的类型化 Story Script 语义失败才进入此闭环。
   HTTP/网络等传输失败没有产生可判断的 Script，不装配 rejection、不消耗 Option；
   旧 audit 1.2 中 `script_generation_failed` 只有存在非空
   `result_failure_codes` 时才视为已尝试。
11. Auto 模式在 Plan preflight 证明当前 Treatment 没有合法 Partition 后，读取
   `recommended_alternate_treatment_option_id`，用
   `--lock-treatment-option STORY_ID=OPTION_ID` 生成新的 Script 请求代次。该代次的
   context 与 strict Schema 只暴露一个已编译 Option，因此 request signature 和
   Script SHA 都会更新；随后强制重建 Approval 哈希绑定、Evidence、Span 与 Plan。
   目标批次开始前快照正式 Script 与 Index；任一 Job 失败或装配失败时恢复原字节，
   全部成功后才用 merge-existing Index 提升目标代次并保留未重跑 sibling，禁止留下
   “新 Script 文件 + 旧 Index/Approval/Evidence/Span”的失败中间态。
   Script/Plan 两层恢复结果分别追加到同一
   `story-treatment-attempts/<story-id>.json` 和
   `pipeline-auto.log`；已失败 Option 不重复，全部耗尽后才正式 reject 该 Story。

## 失效与兼容

- Story Script Schema：`1.6`
- Story Treatment Options schema：`1.2`
- Story Treatment Compiler：`story-treatment-compiler-v4-reserve-precompile`
- Story Script stage：`story-first-story-script-v18-broad-contract-guided-authoring`
- Story Script context schema：`1.6`
- Story feasibility method：
  `functional-evidence-duration-v4-direct-atomic-compaction`
- Story Script semantic retry policy：
  `story-script-treatment-retry-v5-contract-checklists`

Treatment `1.2` 会一次性为 Portfolio 中的 Primary 与 Reserve 都编译合法讲法。
Reserve 此时仍不进入生产；只有 Primary 在 Story Script 的有界修复全部耗尽并形成
正式 rejection 后，独立的 `story-portfolio-replenishment.json` 才能把 Reserve
提升到空出的生产槽位。提前编译可保证补位时不改写整个 Treatment 文件，避免已经
成功的兄弟 Script 因 Treatment SHA-256 改变而失效。
一旦 Promotion 生效，被替换 Story 只保留 rejection/audit 历史，不再属于 active
Treatment recovery 集合；后续恢复只允许当前槽位最后一任占用者。不得在 Reserve
成功后用旧 Treatment lock 复活被替换 Story。
- Treatment attempt audit schema：`1.4`
- Legal Option Compiler：`story-plan-legal-option-compiler-v19-functional-boundary`
- Planning Contract：`planning-contract-v15-functional-boundary`

Portfolio 与 Treatment schema 要求 `story_granularity=broad`；Catalog 身份由动态
Broad Schema 固定。旧 Legacy/无标记 Story 产物必须从 Broad Catalog 重跑；有效的
Series Bible、Event Cards 与 Candidate Catalog 可继续复用。
Auto Script 恢复不修改正式 Story Script schema；目标 Story 过滤不改变单 Job 请求，
锁定 Option 会改变动态 Schema，因此自然产生新的 request signature。Retry policy
也进入请求签名；v4 会使旧 v3 Script cache 失效。Audit 1.4 在保留所有历史
generation 的同时，为 compile-only attempt 保存 phase/counter、base Script hash、
失败/replacement Beat、未失败 Beat hash 校验、compaction/mismatch 投影、错误签名
与 preservation contract/hash；正式 Story Script schema 仍为
`1.6`。Treatment 或本次 Script/Plan 合同变化会使 Story Script 及其下游
Evidence、Span、Plan、QC、Render 缓存失效。Render Recipe schema 不变。

## 非目标

- 不新增全局 Story Atom、Story Graph、Viewer State 图或因果证据路由。
- 不让模型输出自由时间码或直接剪片。
- 不引入多 Highlight 蒙太奇、通用自由 J/L-cut、标题卡、BGM 或视觉转场模板；
  唯一受控音视频分离例外是经本地编译、效果态 QC 的
  `audio_tail_visual_repair`（`reviewed_bridge` / `right_av_overlap`）。
- 不自动学习哪种 Treatment 的投放效果；推荐项只是稳定默认，实际选择仍由
  Story Script 在动态 Schema 允许的 Option 中完成。
