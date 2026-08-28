# VLM 选择集显式重跑

目标：支持已完成 SourcePrep 的真实语义运行在策略变更后仅重跑指定集；旧 run、Receipt、
ArtifactSet 与 provider Attempt 不可改写。新 run 必须有独立的 immutable plan 和新的
GenerationAttempt。相同计划/key 重放返回同一新 run；跨主机续跑只依赖 PostgreSQL/Blob，
不依赖原媒体路径或 Metadata 凭据。

首切片只允许 `stage=vlm`、`completion_scope=selected_only`、一个或多个已存在源集号。
它不声称完整剧集成功，不进入 Story 阶段，也不实现全量复用、自动扩展或跨-lineage 预算。
缺少可验证原始输入、授权或 Blob 时拒绝，不降级为猜测/重新扫描。

详细边界与后续 full-stage 设计见 `docs/pipeline-selective-recompute-design.md` 第 1–8 节。
