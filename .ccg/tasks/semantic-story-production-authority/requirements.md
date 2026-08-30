# Requirements

1. `semantic_story` 必须独立运行 SourcePrep、ContextPrepare、VLM、Stage 1、Stage 2、Stage 3。
2. 它不得读取或依赖包含 ASR、VAD、物理剪辑、渲染能力的旧 `local-run` authority。
3. Stage 1-3 使用真实 Doubao 文本生成策略，不得使用 tests 中的 synthetic policy/model/prompt。
4. 安装资源必须以闭合 schema、内容摘要和代码复算绑定策略身份。
5. 运行时持久化的 VLM/Stage 1/2/3 policies 必须与安装 authority 完全相等。
6. 测试必须证明 `semantic_story` 组合时即使 local-run resolver 被禁止调用仍可成功。
7. 代码经定向与数据库测试后提交并推送，PC 通过 Git 拉取并运行一集真实流程。
8. 模型输入不得新增物理剪辑、ASR/VAD 或外部发布参数。
9. V4 SemanticPack 必须以独立类型通过 committed reader 重验并供 Stage 1 消费，不得伪装成 V3 或伪造 frame evidence。
10. ContextPack 参与的 VLM batch 幂等键必须由 VLM finalizer 与 Stage 1 predecessor 使用同一已提交 Pack 集合重算。
11. 新的 Candidate enrichment 与 Story proposal 必须是两个独立、可重试、可恢复的 GenerationInvocation；不得把两次调用伪装成同一 invocation 的两次 attempt。
12. CandidateCatalog V2 必须由 Kernel 扩展引用、派生 coarse support、复算 measurement closure 和 capability；候选生成模型不得填写 Admission、物理端点或发布结论。
13. 旧 Stage2 V1、CandidateCatalog V1、V11/V23 资源与历史 replay 语义必须保持不变；新语义使用新 policy/profile/migration 版本。
