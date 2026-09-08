# R4 Recipe 时间线预览与受控修订

用户授权独立完成 R4；R2/R3 由其他 harness 负责，本任务不得修改其文件。

第一交付聚焦 R4A：从已提交的 Production Recipe 投影只读 `RecipeTimeline/v1` 和 `RecipeDiff/v1`，
提供可复算的 Kernel reader/service 与最小 HTTP 读取入口。R4B 的 EditProposal/CAS 应完成详细设计和
测试骨架，但只在 R4A 有真实 Recipe 读回证据后再实现写路径。

约束：

- 仅共享 `autocut-kernel` 的 Recipe/Store/Receipt 为业务权威；不调用旧 `autocut_core` 或旧文件 Recipe。
- 时间权威使用整数 tick + time_base；显示秒数只能派生。
- API/前端只读取同一 committed Recipe，不维护浏览器私有真相。
- 任何修订必须新 revision、CAS、受影响 Recipe 编译和 QC；R4A 不写 Store。
- 全部测试在 PC WSL 运行；Mac 只编辑、分析、Git。
- 保留其他 harness 的未提交改动，不暂存或提交其文件。

验收：真实 committed Recipe 可投影、同 Recipe 输入确定性、错误 recipe/跨 time_base diff 被拒绝、
目标测试/ruff 通过、双模型 review 无未解决 Critical。

## R4B-A SpanVariantSet 持久化

本任务继续实现 R4B 的前置产物，不实现 EditProposal/CAS 写 Recipe。

- 保留 `candidate-local-exact-v1` 的 request/result/hash/默认行为。
- 新 variant compiler 对同一完整可行 relation 排序，只保留显式上限 K 个；记录 `feasible_count`、
  `omitted_count`、relation/domain/request/policy hash，不能声称完整物化所有候选。
- `1 <= K <= 16`；枚举继续受父 `max_video_pair_visits/max_av_pair_visits` 限制，`feasible_count`
  不得超过访问上限，整数不超过 `2^53-1`。不用墙钟超时决定业务结果。
- 单个 `span_variant_set` canonical JSON 最大 8 MiB；超过时命令明确 denied，不能截断到一个未声明的新 K。
- 始终满足 `omitted_count = feasible_count - len(retained_variants)`；K 未形成截断时 omitted 为 0。
- ordinal 0 必须与现有 `compile_candidate_av_span()` canonical result 完全相同。
- Variant 必须绑定同一 Story/Beat/requirement/alternative/candidate/query 和完整 BoundaryProof/DialogueGuard。
- 使用独立 `BuildSpanVariantSetCommand@1`，请求身份绑定父 Stage 4 request hash、Job、slot/Receipt/ArtifactSet、
  set hash、report/recipe/admission 精确成员 refs 和 variant policy；失败/非 succeeded 父结果在 claim 前拒绝。
- Store 采用 `commit_span_variant_set_success(request, success)` 专用 writer；
  `commit_command_success` 按 command name 显式抛 `CommandStateError`，并有直接负例。重复 key 只有在
  全量读回、独立重算和 immutable content 相等后才返回原 Receipt；冲突/损坏直接失败。
- reader 精确核对父 Recipe、输入、成员 hash/set hash、排序和每个 variant 的完整搜索证据。
- provider 调用数为零，不增加数据库表，使用现有 ArtifactSet/Receipt。
- 新策略为 `build-span-variant-set-v1`；改变排序、保留策略、payload 或父绑定语义必须注册新版本。
