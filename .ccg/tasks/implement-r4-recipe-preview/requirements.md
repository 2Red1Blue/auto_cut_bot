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
