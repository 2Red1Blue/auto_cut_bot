# Plan

1. 审计新架构现有 Media DTO、fixture preflight、source prep、Artifact Store 与 Runtime stage 边界。
2. 定义生产用时序 evidence DTO、冻结 Policy/Calibration、覆盖闭合与自适应窗口状态机。
3. 先写生产 adapter 与 Command 的失败/幂等/原子提交测试，再实现本地 evidence producers。
4. 将 `media_preflight` 注册为 VLM 后的 HTTP Pipeline stage，禁止 Runtime 私有写路径。
5. 在真实 PostgreSQL、真实媒体及一条已提交 Doubao observation 上跑通，修复发现的问题。
6. 运行 lint、type check、architecture tests，并用独立 Codex reviewer 对抗审查。
7. 同步设计文档、归档 CCG/Trellis task 并提交 Git。
