# Requirements

- 完成阶段/单集重算方案的对抗审查，核对实际代码，不能仅重复理想契约。
- 保持 Pipeline 独立 HTTP 编排，Kernel 共享结果/授权规则，原生产事实不可变。
- 说明真正复用需要什么接口和持久化变化，不把 debug/旧 Receipt 复制当成功。
- 重点覆盖同策略重算、策略变更、下游失效、跨 Job Blob 权限、预算和多 worker 暂停。
- 本轮只做设计/审查/现有测试验证及 Git 提交；不执行付费模型或 PC/数据库变更。
- 用户私有 auto_cut_bot.config.json 与既有任务均不纳入提交。
