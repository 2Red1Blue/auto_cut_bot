# Review

## Verdict

V23 作为“恢复视频证据支持的语义候选”的独立契约可以提交。它不修改 V22 历史字节，不开放 cause/effect/temporal graph，也不授予模型 ASR、VAD、帧或物理剪辑端点权限。

## Verified

- V23 与 V22 稳定 Schema 只有候选集合及候选区间 uncertainty 上限发生预期变化。
- Prompt 明确 hook/highlight 闭合、局部 ID、引用闭合、规范枚举顺序和粗粒度毫秒区间。
- installed semantic authority 的 prompt/schema/resource hashes 已重新计算并闭合。
- Migration 0048 只注册新 execution profile，不改写历史运行。
- 290 项定向测试通过，覆盖 Ark wire、严格解析、authority、VLM stage、Stage 1–3 adapters、CandidateCatalog 投影和 Story compiler；Ruff 通过。

## Remaining Critical

完整运行计划尚未统一：`semantic_only` 使用 V23，但只调度 SourcePrep、ContextPrepare、VLM；full plan 会调度 Stage 1–3，却仍绑定旧 V3/local-run authority。因而本次提交只能声称“V23 契约和语义探测入口已闭合”，不能声称“同一真实 V23 HTTP run 已跑通 Stage 1–3”。下一步必须新增合法的 full semantic-story execution profile，或定义已提交 V23 Artifact 的跨 plan continuation；不得靠运行时临时改 profile。

## Deferred P0

`PipelineStageRunner` 仍把所有异常投影为 `indeterminate`。异常分类应作为独立变更完成，避免与 V23 契约升级混在一个提交里。
