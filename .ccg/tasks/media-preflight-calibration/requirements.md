# Requirements

- 以已提交的 Doubao VLM observation 粗区间为语义输入，不让 VLM 产生物理剪辑端点。
- 自适应扩展候选窗口并生成 Transcript、VAD、真实视频 PTS、音频 sample、shot/scene、visual validity 与 subtitle timing evidence。
- ASR、VAD、物理端点、视觉和字幕是合取约束；缺失、未知或覆盖未闭合时 fail-closed。
- OCR 文本不进入语义链；字幕 detector 只产生时间安全证据，未运行不能解释为无字幕。
- 使用整数 tick 与显式 time base，绑定 source hash、producer/policy/calibration identity 与 coverage。
- 通过共享 Kernel Command/Store 原子提交 ArtifactSet 与 Receipt；Pipeline Runtime 只提交 Command，不直接写权威业务结果。
- 接入 HTTP 触发的 Pipeline stage，并支持幂等重放。
- 至少用一条真实 Doubao observation 和本地媒体在 disposable PostgreSQL 中完成验证。
- 禁止依赖 legacy pipeline、float-second aligner、fixture ground truth 或隐藏默认值进入生产路径。
