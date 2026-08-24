# Plan

## 验收定义

“真实 VLM 验真通过”只在一次显式 opt-in 的运行同时满足下列条件时成立：真实视频形成完整 committed root evidence；受策略绑定的窗口代理可由 ProxyTimelineMap、hash 和 source clocks 追溯；已配置 VLM 实际返回响应；响应被严格解析并原子落库；VLM observations 能驱动现有 SemanticChain；root Transcript/VAD、视频帧、视觉和字幕约束共同形成 exact A/V feasible pairs；ExactSpanCompiler 选择唯一方案并由 Admission 独立复算。除此之外的 fake/replay 仅称为确定性验证或录制响应验收。

## Layer 1：可并行

1. 扩展现有 `media` 类型和 preflight，定义完整 root evidence、coverage/outcome 与相关测试。
2. 定义 Kernel-owned WindowManifest、ProxyTimelineMap、WindowMediaBuilder、VlmClient 与严格 parser。
3. 在现有 Store 上补足 BlobRef、GenerationAttempt 和各 ArtifactSet 的精确读取/篡改测试，不新建数据库。

## Layer 2：顺序集成

4. 在现有 Pipeline Command 层编写 `GenerateVlmEvidenceCommand`，绑定 Command/Attempt/Blob/Receipt/CAS 状态机。
5. 编写受信 VLM evidence adapter，将 committed observation 映射为现有 `SemanticChainInput`；核心语义函数不接触 PTS、图片、原始文案。
6. 将现有 `physical_edit` 演进为视频/音频四端点、并列 evidence constraints、exact A/V pairing 和唯一 canonical selection。
7. 让 Pipeline Runtime 与 Agent Runtime 仅调用同一 Kernel Commands，并保留 ScenarioRegistry 为测试 fixture。

## 验真分层

1. 默认 CI：fake evidence producers/window builder/client、A/V exact compiler oracle、零网络、命令幂等与所有拒绝路径。
2. 录制响应验收：脱敏 request/response fixture 的 replay，不访问外网。
3. 显式 live VLM smoke：`AUTO_CUT_BOT_RUN_LIVE_VLM=1` + 指定 smoke provider preset；小型无版权视频窗口、单次调用、不可写外部平台。失败单独报告，不伪装成单测成功。
4. 真实视频语义 E2E：用一段用户授权的剧集样本运行 root evidence → VLM → SemanticChain → Exact A/V span → 本地 Render/QC；验收证据链而非模型文案逐字一致。OCR 不属于验收条件，SubtitleCue timing 属于。

## 明确停止条件

- 未有可用配置或授权视频时，完成 fake/replay 与本地边界实现，但状态只能是“等待 live verification”，不能声称 VLM 已验证。
- 任何 provider 将视频降级为纯文本、无法保证结构化响应及本地严格解析，或结果处于 ambiguous timeout 时，拒绝该 invocation 成为 committed VLM evidence。
