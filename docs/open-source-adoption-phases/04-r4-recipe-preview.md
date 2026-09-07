# R4：Recipe 时间线预览与受控修订

## 状态（2026-09-07）

R4A 已实现并推送至 `feat/v213-contract-codegen`：Kernel 的 `RecipeTimeline/v1` / `RecipeDiff/v1`、
静态 committed-set inspection reader、精确 Store lookup、Runtime read service 和以下 HTTP GET 入口：

```text
/api/pipeline/runs/{run_id}/recipes/{story_id}/timeline?revision=N
/api/pipeline/runs/{run_id}/recipes/{story_id}/diff?base_revision=N&target_revision=N
```

API 只返回展示所需的 story/revision、整数 tick、时基、源时钟和候选信息；不返回 Receipt、
ArtifactSet、scope、Blob ref 或 exact-span 证明哈希。PC WSL 定向回归 82 项通过，目标 Ruff 通过。
当前 HTTP runtime 未编排 Stage 4，因此尚无真实 HTTP run 的 Stage 4 Recipe 可作端到端读回证据。

R4B 仍是设计态。其输入要求为“持久化、可独立复核的可行 SpanVariant 集”；当前 Recipe 只保存已选择
的 exact span，不保存可替换 variant 集。不能把任意 UI tick、当前 Recipe 的内容或模型建议伪装成可选 variant。

## 目标与完成定义

吸收 Timeline Studio/Dawn Cut 的 inspect→diff→apply 体验，把已经编译的 Recipe 展示成可定位、可比较的时间线。
自动 Pipeline 不等待人工；预览和修订是可选入口。首个版本只读，第二个版本才允许受控编辑。

完成要求：timeline 从确切 Recipe 可复算；预览时间与渲染一致；编辑使用 CAS 产生新 Recipe revision；
受影响 span 重新编译和 QC；旧 Recipe/Render 可回看。

## R4A：只读投影

拟新增：

```text
packages/autocut-kernel/src/autocut_kernel/pipeline/recipe_timeline.py
auto_cut_bot/pipeline/review/recipe_projection.py
auto_cut_bot/api/routes/...                # 按现有 API 路由布局实现时确定
webui-next/...                             # 若当前 WebUI 仍为目标前端
tests/pipeline/test_recipe_timeline.py
```

`RecipeTimeline/v1` 从 `read_committed_production_recipe_set` 读取并投影：recipe/story/revision refs、
总时长、整数 tick/time_base、每个 beat/clip 的 source span、独立 video/audio in/out、转场、字幕/BGM refs、
QC issue refs、render preview ref。显示秒数是 UI 派生值，权威值始终是整数 tick。

`RecipeDiff/v1` 比较两个 Recipe refs：新增/删除/移动/变更 variant、时长差、受影响 beat、需重跑 QC。
两边内容不存在或时间基不同不能静默换算，返回具体错误。

## R4B：编辑提案与应用

`EditProposal/v1` 字段如下；`created_at` 只用于审计，不进入内容等价 hash。

| 字段 | 必要性 | 用途 |
|---|---|---|
| `proposal_id` | 必需 | 幂等提交与查询 |
| `base_recipe_ref`, `base_revision` | 必需 | CAS 和差异基线 |
| `edit_ops` | 必需、非空 | closed edit union |
| `reason` | 条件必需 | 人工/Agent 修订理由；系统 revert 使用固定 reason code |
| `created_by` | 必需 | actor 类型与非密钥 ID |
| `created_at` | 必需 | 审计时间，不改变编辑业务等价性 |

R4B 进入实现前必须先完成 `SpanVariantSet/v1`：每个 beat/requirement 的 variant 绑定相同的
Blueprint、媒体 evidence、策略和 exact endpoint proof；每个 variant 有稳定 ID，集合由 Stage 4 Command
持久化并经独立 reader 复核。之后首批 edit op 限定为：

- `select_variant`：在已提交可行 SpanVariant 中替换；
- `reorder_beat`：仅当 Blueprint/Story policy 允许顺序变化；
- `request_recompile`：修改受限目标偏好，由 compiler 重新选择，用户不提供物理 tick；
- `revert_to_revision`：以历史 Recipe 为新修订来源，不删除中间历史。

暂不允许自由输入浮点 start/end、任意 FFmpeg filter、删除必选 beat 或绕过 QC。
未来确需手调切点时，应先定义带证据 clearance 的新操作类型。

`ApplyEditProposalCommand` 在事务内比较 base revision、重读 variant set/Blueprint/Policy、执行受影响
ExactSpan/Recipe 编译并提交新 revision。渲染到 staging，QC 通过后原子晋升新的本地输出；失败保留诊断及旧可见成片。

## API 与 UI 行为

API：读取 timeline、计算 diff、提交 proposal、查询 apply Receipt。响应返回 Artifact refs 和状态，
大视频/Blob 用受控下载或预览引用。UI 至少显示视频/音频轨、Story beat、字幕区域、切点、QC 标记和 revision。

并发编辑冲突返回当前 revision 与可重放 proposal，不自动覆盖。页面刷新从 Store 重建，浏览器状态不是真相。
无人模式直接使用编译器生成的 Recipe；没有打开 UI 不影响 Pipeline。

## 实现任务与验证

1. 定义 timeline/diff DTO 和纯投影，使用真实 Recipe fixture 验证 ticks、轨道和片段顺序。
2. 实现只读 API；再做最小 UI，支持跳转预览与两 revision diff。
3. 定义 EditProposal codec、Artifact/Command/reader，先实现 select_variant 和 revert。
4. 接局部 compiler、render staging、QC 与原子晋升；补 stale revision、并发和恢复。
5. 增加白屏、截字、字幕残留、A/V 不同步 fixture，确认编辑无法绕过已注册规则。

退出条件分两级：R4A 是真实 Recipe 在浏览器/报告中定位一致；R4B 是一次真实编辑产生新 Recipe/Render/QC，
一次非法编辑被拒绝且旧成片保持可用。R4A 可先进入 R5，R4B 不阻塞自动本地成片。
