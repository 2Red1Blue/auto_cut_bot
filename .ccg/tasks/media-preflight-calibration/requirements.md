# Requirements

- 以已提交的 Doubao VLM observation 粗区间为语义输入，不让 VLM 产生物理剪辑端点。
- 自适应扩展候选窗口并生成 Transcript、VAD、真实视频 PTS、音频 sample、shot/scene、visual validity 与 subtitle timing evidence。
- ASR、VAD、物理端点、视觉和字幕是合取约束；缺失、未知或覆盖未闭合时 fail-closed。
- OCR 文本不进入语义链；字幕 detector 只产生时间安全证据，未运行不能解释为无字幕。
- 使用整数 tick 与显式 time base，绑定 source hash、producer/policy/calibration identity 与 coverage。
- 通过共享 Kernel Command/Store 原子提交 ArtifactSet 与 Receipt；Pipeline Runtime 只提交 Command，不直接写权威业务结果。
- 接入 HTTP 触发的 Pipeline stage，并支持幂等重放。
- 至少用一条真实 Doubao observation 和本地媒体在真实 `autocut` PostgreSQL 中完成验证；只允许增量迁移和唯一 Job 写入，禁止测试夹具清空真实 schema。
- 禁止依赖 legacy pipeline、float-second aligner、fixture ground truth 或隐藏默认值进入生产路径。

## 首剧 bootstrap 与正式 authority 的分层

首部真实测试剧可以先进入 `shadow_bootstrap`：它允许 SourcePrep、VLM、ASR/VAD、PTS、音频 sample
和视觉/字幕探测生成**未认证时序观测**与校准候选。此路径的产物必须显式标为 shadow，不能进入
Recipe、Render promotion、外部发布或正式 `LocalMediaPreflightPolicy` authority。

独立 ASR/VAD 时间锚点不是首剧启动前置。它们在首剧观测后针对少量局部样本提供，并用于：

1. 计算可接受的 ASR/VAD 误差上界；
2. 生成和验证 CalibrationRecord；
3. 将同一策略从 shadow 候选提升为可供 MediaPreflight/Stage 4 使用的正式 authority。

禁止用 ASR/VAD 自己的输出作为其自身正式误差的真值；外部剧情 API 的章节、字幕、镜头或高光
时间也不能替代与原视频同源、可验证时基的独立音频锚点。
