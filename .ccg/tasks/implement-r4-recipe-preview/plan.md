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
