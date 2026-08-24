# 原版实现审查结论

审查日期：2026-08-23。仅限只读；“代码存在”与“当前入口可执行”已分开判断。

## 原版实际做过什么

```text
扫描本地视频或登记远程 URL
→ 推断集数、ffprobe、SHA-256
→ 约 240 秒的重叠物理窗口 + 720p 压缩
→ VLM 视频请求（Ark 上传或 LiteLLM inline/URL）
→ JSON Schema / identity / 时间区间校验
→ window summaries + best-effort DB projection
→ Event Cards / candidates / span / render
```

较新的同源实现还包含：更强的文件名分集识别、480p window 压缩、PySceneDetect、静音/VAD、可选 ASR anchor 和 FunASR transcript stage。

## 不能误认为已经闭合的能力

- 没有 OCR 引擎、关键帧抽取或帧采样 artifact。
- 480p 文件实际会生成，但请求组装读取 `media_file` 而非 `media_file_480p`；没有 VLM 确实用到 480p 的证据。
- ASR 在实际旧 CLI 链中位于 VLM 之后；较新的 ASR 会写数据库但其不可用状态不阻断下游。
- `confidence_check` 是报告/建议，不是强制 gate。
- 旧 CLI 主要文件扫描只发现 `plugins/*` stages；较新的 VLM-first stage 位于另一目录，除非 entry point 安装态恰好可发现，否则并不在实际入口。
- Agent/API 还存在失效 import，不能作为“可直接运行”的证明。

## 可迁移原则（不是直接 import）

1. 分集识别模式、滑窗/重叠策略、源内容 hash。
2. VLM 请求签名、JSON/schema/identity 的分层验证。
3. `canonicalize_vlm_analysis` 的纯归一化/审计 finding 思路。
4. 场景、VAD、ASR anchor 的三层安全切点策略。
5. VLM evidence → EventCard → candidate 的证据可追溯关系。

## 禁止原样搬迁

- ArtifactBus/Stage/项目 JSON checkpoint 与 DB best-effort 混合写入。
- 物理窗口从 0 秒播放、却要求模型返回原片绝对秒的时间语义。
- 运行时 prompt override、自动文本降级、流式时删除 response schema。
- 旧 provider/client、缓存目录和 legacy 数据库对象进入 Kernel。

## 对新任务的直接影响

新 Stage 1 不做“整段视频 → 模型秒数”的黑箱调用。它应先持久化源证据和帧样本，再让 VLM 用 `frame_id` 引用观察；PTS 与物理剪辑仍由 Kernel/MediaEvidence 独立掌握。ASR（按需）、代理和 VLM 均应成为独立、可验证的 evidence producer，任何缺失或失败都必须被显式记录，不能只打 warning 后继续生成候选。OCR 不在本版本范围内：当前 VLM 已可直接理解画面和烧录字幕，引入 OCR 只会增加冲突源与校准成本。
