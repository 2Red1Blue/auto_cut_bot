# Plan

1. 审计新架构现有 Media DTO、fixture preflight、source prep、Artifact Store 与 Runtime stage 边界。
2. 定义生产用时序 evidence DTO、冻结 Policy/Calibration、覆盖闭合与自适应窗口状态机。
3. 先写生产 adapter 与 Command 的失败/幂等/原子提交测试，再实现本地 evidence producers。
4. 将 `media_preflight` 注册为 VLM 后的 HTTP Pipeline stage，禁止 Runtime 私有写路径。
5. 在真实 `autocut` PostgreSQL、真实媒体及一条当前格式的 Doubao observation 上运行
   `shadow_bootstrap`；保存未认证时序观测与校准候选，不得用会 `DROP SCHEMA` 的测试夹具冒充真实验真。
6. 从首剧观测中抽取少量局部样本，写入独立 ASR/VAD 时间锚点；验证 CalibrationRecord 后才生成
   正式 MediaPreflight policy / Stage 4 authority。
7. 使用正式 authority 重跑受影响范围，验证 MediaPreflight、Stage 4、Render/QC 的发布边界。
8. 运行 lint、type check、architecture tests，并用独立 Codex reviewer 对抗审查。
9. 同步设计文档、归档 CCG/Trellis task 并提交 Git。
