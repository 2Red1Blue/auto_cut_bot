# 非 legacy VLM-first 实现审查

范围严格限于 `ac_auto_cut/autocut_core`、`ac_auto_cut/plugins` 与同源 `auto_cut_bot/packages/autocut-core`；不包含 `_legacy_v4`。

## 已验证的强能力

- `autocut_core/stages/ac_source_prep/source_windows/stage.py`：较强的集数推断、窗口代理、PySceneDetect、静音/VAD、ASR anchor 产物。
- `autocut_core/stages/ac_source_prep/vlm_analysis/stage.py`：窗口视频 VLM、严格结果收集、候选与三层对齐调用。
- `semantic/request.py`、`schema/window.py`、`semantic/vlm_analysis_contract.py`：中文任务提示、严格 JSON schema、identity/time admission、候选归一化和审计 finding。
- `semantic/scene_boundary_fusion.py`、`audio/asr_anchor.py`、`audio/vad.py`：ASR anchor → VAD → 视觉场景的切点安全策略。

## 不能直接运行/导入的原因

当前 `ac_auto_cut` 的 plugin source stage 产生 `window_analysis`，而 VLM-first stage 期望 `vlm_analysis`；注册器源码扫描只能发现 plugin stages，富 source stage 位于核心目录，只有安装态 entry point 才可能发现。`auto_cut_bot` 副本又缺少它所转发的 request/batch runtime。它们不能混拼为新运行时。

此外：480p 代理文件会生成但请求主路径仍读取 720p `media_file`；prompt/cache signature 未绑定最终 prompt 或 context injection；`confidence_check` 只报告并不调度 ASR；三层对齐缺少产物时静默跳过。因此任何“直接 import 旧 stage”都会把版本漂移与 fail-open 行为带入 Kernel。
