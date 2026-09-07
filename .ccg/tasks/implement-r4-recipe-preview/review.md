# R4A 审查记录

范围：`f672388e..0630bdcf` 的 Recipe timeline/read-only HTTP 变更。

PC WSL 验证：84 passed；目标 Ruff passed。

## 已修复

1. HTTP public mapping 曾包含 committed Recipe ref 和 exact-span proof hash；现仅输出展示字段。
2. Recipe route 曾可能吞掉其它 `/api/pipeline/runs/*` 路径；现只处理明确的 `/recipes/` 路径，未知子路径落空。
3. inspection reader 现重新检查每个 member 的 canonical hash 和整个 ArtifactSet hash。
4. Store 查询与 payload inspection 移到工作线程，避免占用 WebUI event loop。
5. 不存在的 run 通过轻量 `PipelineRecipeNotFoundError` 映射为 404，避免 WebUI 顶层引入 runtime 包造成 Config 初始化顺序问题。

## 外部审查状态

Codex reviewer 报告的 provenance、set hash、重复 key 与路由问题已逐项处理。
Claude backend 实际报告模型为 `glm-5-3-flash`，与任务约定的 Claude 不一致，不能计作独立 Claude 通过；
保留此限制，不把当前 R4A 描述为双模型一致批准。

## 剩余

R4B 需要 Stage 4 持久化、可独立复核的 `SpanVariantSet`。当前 ProductionRecipe 只保存选中 span，
所以没有安全的 `select_variant` 写入源。R4A 不写 Store、不会编译、渲染或生成新 revision。
