# Story 本地音频 Boundary Repair

## 目的

把 Story QC 中本地双路 VAD 检出的可恢复吞音边界，转换为可审计的不可变
Boundary Patch 和派生 Story Plan，再自动重新验证。该阶段不重选 Story、Beat、
Span 或 Block，不生成 J-cut/L-cut、正式转场或成片。

若操作员另行提供 `junction-edit-constraints.json`，它在 Boundary Repair 完成后由
独立的本地 Junction Edit Compiler 处理，不属于 Boundary Repair，也不回写基础或
派生 Story Plan。

## 输入

- 当前有效的基础 `story-plans/index.json`。
- Plan Index 已绑定的不可变 `span-candidates/<story-id>.json`；派生 Plan 只使用其
  语义/因果身份，物理 gap 使用当前有效 Clip 时间码。
- 当前有效的本地音频 Plan 与报告。
- 音频报告绑定的本地源完整 SHA-256。
- 固定的 Demucs、Silero VAD、ONNX Runtime 版本和策略。

基础 Story Plan 必须先通过原 Story Plan Validator。人工 Admission 可以允许已物化的
blocked Plan 的可放行风险进入音频分析和 Story QC，但
`dialogue_incomplete`、`same_source_causal_gap` 或缺少当前 continuity contract 的
旧 Plan 不可 Admission。Boundary Repair 不能清除原始 blocked reasons，也不能
放宽下述时长、Teaser、同源重叠或连续性硬约束；修复违反任一硬约束时仍进入
`blocked_replan`。

## 自动修复资格

只有同时满足以下条件的 `adjustment_required` 可以自动落盘：

1. 音频门禁提供有限的建议源时间码。
2. 入点建议严格早于原入点，或出点建议严格晚于原出点。
3. 调整绝对值不超过 12 秒。
4. 新边界位于同一 Source 的物理范围内。
5. Story 总时长 ≤1200 秒。
6. Teaser 仍不超过 15 秒。
7. 不新增未经 `teaser_reprise` 声明的同源重叠。
8. 派生 Plan 仍通过严格 Story Plan schema。
9. 重新计算后不存在 `dialogue_incomplete` 或 `same_source_causal_gap`。

修复只允许向外保留更多原片。禁止通过入点后移或出点前移继续裁短来隐藏吞音。

相邻 Clip 在同一 Source 上精确连续（0.05 秒容差）时，共同时间点不构成两个
独立音频切口，即使该点有活跃语音也按连续播放处理。若只有不超过 policy
`minimum_safe_gap_seconds` 的微小正 gap，则只单侧延长左 Clip 到右 Clip 现有
入口；不得把左出点和右入点分别扩到两个安全点而制造重复播放。

派生 Plan 必须通过共享确定性函数重新计算并验证时长、重复率、整集率、Source
Usage、Editorial Metrics、顶层 `continuity` 与逐 Junction 的
`same_source_gap_seconds`/status/findings；不得用扩边幅度代替重验。特别是
Outro 裁尾如果把原本安全的同源因果 gap 扩大到 12 秒以上，Patch 必须拒绝，不能
留下状态与时间码不一致的派生 Plan。
非 reprise 重叠检查以父 Plan 的逐 Clip-pair 重叠为基线，只拒绝本轮新增或放大的
部分；父 Plan 中未变化且已经通过正式 Plan Validator 的重叠不得反向阻断所有
无关音频修复。

## 状态路由

- `not_needed`：没有需要修复或人工判断的边界。
- `verified_after_repair`：单轮 Patch 后，最终双路 VAD 全部通过。
- `review`：扩边超过 12 秒、物理源边缘已有活跃语音、音频门禁返回
  `blocked_replan`，或单轮复检仍不安全；这些边界记录 `fade_fallback`。
- `blocked_replan`：方向错误、非有限时间码、违反 Plan 硬约束或
  `analysis_error` 等不可安全降级的数据/合同错误。

`review` 不得自动升级为 approved；`blocked_replan` 必须返回 Story Plan/Candidate
选择或扩展 Story Scope。

## 版本与产物

每轮输出：

```text
story-plan-repairs/round-<NN>.index.json
story-plan-repairs/<story-id>/round-<NN>.patch.json
story-plan-repairs/<story-id>/round-<NN>.plan.json
```

Patch 必须保存：

- Story、Clip、Source 和 boundary 身份。
- 父 Plan 路径与 SHA-256。
- 触发修复的音频报告路径与 SHA-256。
- 原时间码、建议时间码、调整量和语音区间。
- 修复策略与原因。
- 结果 Plan 路径与 SHA-256。

有效 Plan Index 保存完整 Patch History，并绑定基础 Plan Index、父 Plan Index 和
本轮音频报告。任一文件或哈希变化后，修复链失效。

## 增量复核

- Demucs/Silero 的 Source 分析缓存按源内容哈希复用。
- Clip 代理按 Source、起止时间和编码参数内容寻址。
- Junction 代理按左右 Clip 的实际边界和编码参数内容寻址。
- 只重新编码发生边界变化的 Clip 与相邻 Junction。
- 派生 Plan 使完整 Story Proxy 和 Story Flow 失效，因此必须重新汇总。
- 未变化 Junction 的媒体与上下文指纹保持稳定，可复用旧视频判断。

## 轮次上限

自动修复最多一轮。复检仍不安全、建议扩边超过 12 秒或无法向外扩边时，控制器
停止扩边并记录受控 `fade_fallback`；方向、数据或 Plan 硬合同错误仍输出
`blocked_replan`。不得继续循环；只有显式记录、进入效果态 QC 并由 Render Recipe
执行的 `fade_fallback` 可以使用约定的 audio crossfade + video fade，不能用任意
静音、淡化、补帧或音频覆盖隐藏问题。

## 当前不自动处理

- 物理源文件本身从半句话开始或结束。
- 跨 Source/跨集拼接。
- 改选其他 Span Candidate。
- 由 Boundary Repair 自行生成 J-cut/L-cut 或音视频分离边界。
- 基于 ASR 句意判断语义是否完整。

这些情况只生成明确路由，交给人工复核、Story Plan 重选或后续 Render Recipe。
后置 Junction Edit Compiler 可以处理操作员明确提交的
`audio_tail_visual_repair`，但不回写本阶段产物：`reviewed_bridge` 必须明确给出
安全桥接区间和禁画区间；`right_av_overlap` 必须明确给出禁画区间及右侧入口视觉
复核，并复用本阶段已经绑定的双路 VAD 证明没有明显双对白。不得从音频分析结果
自动猜测桥接画面、右侧时间码或重选 Span；两种策略均不得与同一 Junction 的
`fade_fallback` 叠加。
