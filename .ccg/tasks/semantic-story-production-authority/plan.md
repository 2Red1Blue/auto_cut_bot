# Plan

1. 保留已完成的 semantic authority V2/V11，不改写其字节和历史 replay 语义。
2. 修复 ContextPack-bound VLM batch key 在 finalizer 与 Stage 1 predecessor 之间的不一致，增加 video_only Pack 跨层回归。
3. 实现版本化 V4 committed reader 与 Stage 1 core-observation union，保留 V3/V4 exact type 和原始响应重验。
4. 先运行 Store + Stage 1 定向测试和独立审查；未通过前不触发 Ark 真实调用。
5. 设计并实现 Stage 2 V2 双 GenerationInvocation 状态机：先 candidate_enrichment，后 story_proposal，两者独立重试/恢复。
6. 新增 CandidateCatalog V2 和本地 capability evaluator；只在最终 Admission 通过后原子提交 CandidateCatalog/Proposal/Portfolio/Ledger/Admission。
7. 新增不改写 V11 的 authority/profile/migration 版本，并为 Stage 3 增加 V4/CandidateCatalog V2 可重放上下文。
8. 运行 Ruff、定向 pytest、PostgreSQL migration/recovery/replay 测试和独立对抗审查。
9. 提交并推送 Git；PC 只在干净部署工作树 fast-forward 到精确提交。
10. PC 新建一个单集真实 run，验证 SourcePrep→ContextPrepare→VLM→Stage 1–3 的 Receipt/debug/replay；失败时仅重跑最小受影响 invocation。
