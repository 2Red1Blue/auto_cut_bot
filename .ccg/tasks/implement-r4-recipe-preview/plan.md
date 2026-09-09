# 实施计划

## 分析结论

现有 `read_committed_production_recipe_set` 是执行期 reader：它依赖完整的
`CompileProductionRecipeRequest`、authority resolver 和 limits，并独立重编译/验收。
R4A 不能在 WebUI 读取路径调用它。现有 Pipeline Runtime 也尚未编排 Stage 4，因此 R4A 只能读取
同一 Job scope 下已经成功提交的 Recipe，不能在预览时编译、渲染或写入。

已完成的 Codex 独立分析建议：RecipeTimeline 的时长必须来自
`build_production_render_plan` 的 output timescale 和 exact segment durations；不许累加不同源的裸 tick。
Claude 分析调用在项目 AGENTS 规则下递归超时；改用隔离 leaf 后仍未在资源限制内返回，不能把它当通过。
实现完成后的双模型 patch review 将作为交付门槛。

## R4A 文件范围

1. 新建 `packages/autocut-kernel/src/autocut_kernel/pipeline/recipe_timeline.py`：closed DTO、
   `project_recipe_timeline`、`diff_recipe_timelines` 和受限 read limits。
2. 修改 `compile_production_recipe_command.py`：新增 inspection-only decoder，静态核对已提交
   report/recipe/admission 的 layout、scope、revision、hash 和 codec；不重编译、不授予 render authority。
3. 修改 `store/postgres.py`：按 Job/scope/revision 精确查找唯一成功 `CompileProductionRecipeCommand@1`
   ArtifactSet，不读 logical head。
4. 新建 `auto_cut_bot/pipeline/runtime/recipe_projection.py`：镜像 highlight reader，读取 run/profile，
   调用 inspection reader 和 timeline projection。
5. 修改 runtime composition/export、WebUI gateway composition、channel manager 和 `ws_http.py`：
   增加只读 timeline/diff HTTP 路由，复用 token 认证、未组合 503、内部错误泛化。
6. 新增 Kernel、runtime 和 WebUI 测试。R4B `EditProposal`/CAS/recompile/render/QC 继续只保留设计。

## 验证顺序

1. 先跑纯 Kernel timeline/diff 和 inspection reader 测试。
2. 再跑 runtime/WebUI 读取路由测试和目标 ruff。
3. PC WSL 跑相同定向集；若没有真实 committed Stage 4 Recipe，只报告 synthetic/DB 读回验证，
   不宣称真实 R4A 已跑。
4. 对最终 patch 运行隔离双模型 review，修复 Critical 后重跑检查。

## R4B-A 实施计划

第一性原理结论：R4B 只需要一组明确安全、可复算的可选端点，不需要把完整 Cartesian relation
全部落库。完整性由 `feasible_count + feasible_relation_sha256` 表达；可编辑选项是 canonical 排序前 K 个，
并明确 `omitted_count`。这避免存储爆炸，同时不会把 top-K 冒充完整 relation。

1. 提取 `candidate_exact_span.py` 的完整搜索内核；旧 `compile_candidate_av_span` 仍是 ordinal-0 权威入口，
   未请求 variant 时不创建 variant 集。新 top-K 投影复用相同 relation，多个边界 fixture 做 byte/hash parity。
2. 新增 `span_variant_set.py`：policy、variant、entry、set DTO 和 closed codec；variant 0 等于父 report selected result。
3. 新增 `BuildSpanVariantSetCommand@1`：先用原 reader 验证父 Stage 4 成功，再独立枚举并持久化一个
   `span_variant_set` Artifact；重放再次读回和复算。
4. 为 Postgres Store 增加专用 commit writer/generic writer deny；不建新表。
5. 增加纯 compiler、command、Store/重放测试；PC WSL 定向和相关 Stage 4 回归。
6. 更新 R4 文档并进行双模型 patch review。EditProposal/CAS 留给下一切片。

最小实验：在现有 Stage 4 fixture 上设置 K=3，验证旧 canonical result 字节/哈希不变、variant ordinal 0
完全相等、集合有序、K 小于 feasible_count 时 `omitted_count` 正确、重复执行返回同一 Receipt。

硬限制：K 最大 16；单 Artifact 最大 8 MiB；总枚举工作沿用父策略的 video/AV visit 上限，超限停止且
不提交 partial Artifact。父绑定包含 exact request/outcome/set/member refs；所有 re-enumeration 只读取该父请求
解析出的 persisted Stage3/media/authority 输入，不读取当前默认 profile。墙钟时间只做 telemetry，不决定 hash 或选择。

## R4B-B 受控选择与新 Recipe revision

### 目标与边界

将 R4B 的首个写操作收敛为 `select_variant`：调用方只可提交一个已提交
`SpanVariantSet/v1` 中的 `variant_id`，不能提交 tick、span、FFmpeg 参数、旧包对象或未绑定的
Recipe 内容。`EditProposal/v1` 是闭合、不可变的意图载体；`ApplyEditProposalCommand@1` 是唯一的
写入者。`reorder_beat`、`request_recompile`、`revert_to_revision` 保留为未实现的操作种类，不能以
unknown field 或兼容默认值进入本版本。

### 不变量

1. proposal 绑定 exact base Recipe ArtifactSet/member ref、base revision/set hash、SpanVariantSet member
   ref/content hash、variant id、actor 与 reason；审计时间不参加业务内容 hash。
2. Apply 在 claim 前独立读取 base Recipe 和 SpanVariantSet；重放完整候选 relation，所选 variant 必须
   与重放后的 canonical payload 相同。调用方给的任何物理端点均不被解析。
3. Store transaction 以 Job → command slot → current base identity 的固定顺序锁定，并在锁内再次核对
   CAS。冲突不得产生 ArtifactSet、Receipt、head 或可见 Render。
4. 成功产物必须携带可复算的 compilation report、Recipe 与独立 Admission；Render/QC 只接收该成功
   ArtifactSet 的 Recipe member，不接受 proposal 或 variant set。
5. 同 idempotency key 只在全量重读、重放与 canonical bytes 恒等时回放 Receipt；任何 policy、parent、
   set、variant 或 committed bytes 漂移均 fail-closed。

### 分层实现与验证

1. 新增 closed `EditProposal/v1` codec 和只含 `select_variant` 的 edit union，配纯 DTO/closed-json
   negatives。
2. 新增 `ApplyEditProposalCommand@1`，从 exact persisted inputs 复算选择、构造新 revision 的 Recipe
   closure，并用 dedicated Store writer 提交。泛用 `commit_command_success` 必须显式拒绝该 command。
3. 将 admitted Stage 4 set reader 和 render reservation 的 producer/layout 判断抽象为同一闭合的
   `CompileProductionRecipeCommand@1 | ApplyEditProposalCommand@1` 许可集合；仍要求 report→recipe→admission
   和 deterministic execution。不得为新 producer 放宽成员、scope、revision 或 admission 核验。
4. 纯 command、Postgres 事务/replay/CAS、Render/QC rejected-input 测试必须覆盖；PC WSL 用隔离验证库
   跑真实 PostgreSQL writer/replay。只有真正成功执行 persisted renderer+QC 时才报告“真实成片”。

### 明确不纳入本切片

HTTP/UI、自由拖拽 tick、全局故事重排、真实剧 Stage 1–4 runtime 编排及 R5 产品接入均不与 R4B-B
混做。它们分别依赖其它 owners 的 Stage 1–3、Stage 4 runtime port 和真实 media/job closure；本切片
只提供已闭合的 Kernel/Store/Render-QC consumer compatibility。
