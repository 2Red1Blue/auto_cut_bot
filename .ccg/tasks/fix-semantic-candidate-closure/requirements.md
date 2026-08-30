# Requirements

1. VLM 必须能够输出带视频证据区间的语义候选，供 CandidateCatalog 和 Story Design 使用。
2. 语义候选不得包含或冒充最终物理切点；ASR/VAD/Frame PTS 仍由 Media Preflight 与 Stage 4 负责。
3. 不修改 V22 的已持久化语义；使用新版本并保留历史重放能力。
4. Provider wire 只包含模型负责的字段，完整 Artifact 引用由确定性本地编译器维护。
5. 为候选投影到非空 Story 输入增加跨层测试，防止局部测试全部通过但流水线断路。
6. Runner 必须区分可重试、永久拒绝、永久失败与结果未知，静态契约错误不得无限重试。
7. 所有变更通过测试、独立审查、Git 提交和推送；PC 只通过 Git 拉取代码。
8. PC 使用真实单集验证 SourcePrep → ContextPrepare → VLM → Stage 1–3；优先复用已有 VLM 结果。
