# Research: VAD 当前调用链

- **Query**: 从 stage.py 到 vad.py 到 vad_worker.py 的完整调用链，VAD 结果存储和消费
- **Scope**: internal
- **Date**: 2026-08-14

## 文件位置

| 文件路径 | 行数 | 角色 |
|---|---|---|
| `autocut_core/stages/ac_source_prep/source_windows/stage.py` | 1259 | 调用入口（L1100-1259） |
| `autocut_core/audio/vad.py` | 613 | VAD 检测器（主进程侧） |
| `autocut_core/audio/vad_worker.py` | 277 | VAD worker（子进程，.venv-audio-boundary 中运行） |
| `autocut_core/contracts/audio_boundary.py` | ~50 | AudioBoundaryPolicy 数据类 |

## Findings

### 1. 完整调用链

```
source_windows/stage.py::_run_vad_detection()
  │
  ├─ get_vad_detector(backend="demucs_silero", ...)
  │    └─ DemucsSileroDetector(vad_python, cache_dir, device, policy, ...)
  │
  ├─ for each source:
  │    detector.detect(source_path)
  │      │
  │      ├─ 检查缓存: cache_dir / config_hash / sha[:2] / sha.json
  │      │
  │      └─ _run_worker() → subprocess:
  │           cmd = [vad_python, vad_worker.py, --source, --work-dir, ...]
  │           │
  │           └─ vad_worker.py (在 .venv-audio-boundary 中):
  │                1. ffmpeg 提取 stereo mix.wav
  │                2. demucs 分离人声 → vocals.wav + no_vocals.wav
  │                3. 加载 Silero VAD 模型
  │                4. 对三个轨道分别运行 VAD:
  │                   - original_mix (threshold=0.45)
  │                   - demucs_vocals (threshold=0.25)
  │                   - no_vocals (threshold=0.55)
  │                5. 输出 vad_result.json
  │
  │      └─ _from_worker_output():
  │           _smart_merge(demucs_vocals, original_mix, no_vocals)
  │           → VADResult(speech_intervals=union, track_intervals={...})
  │
  └─ 汇总为 episodes dict → speech_intervals.json
```

### 2. VAD 结果存储

#### 文件存储（主要）

| 产物 | 路径 | 格式 |
|---|---|---|
| 聚合 VAD 结果 | `{job_root}/speech_intervals.json` | `{"schema_version":"1.0", "detector":"demucs-silero-vad", "episodes":{"1":[{start,end},...]}}` |
| 单源 VAD 缓存 | `{job_root}/vad_cache/{config_hash}/{sha[:2]}/{sha}.json` | 完整 VADResult（含 track_intervals） |
| Worker 中间产物 | `{cache_dir}/_work/{sha[:2]}/{sha}/vad_result.json` | worker 原始输出 |
| Audio Boundary Report | 由 story_qc / audio_boundary_guard 生成 | 包含 `source_analyses[].vad_path` 指向 VAD JSON |

#### DB 存储

**VAD 结果不直接写入 DB**。VAD 以文件形式缓存和传递。
- 没有 `vad_results` 或类似 DB 表
- VAD 数据通过 `speech_intervals.json` 文件在 stage 间传递

### 3. VAD 结果的下游消费者

#### 消费者 1: `scene_boundary_fusion.py`（snap 逻辑）

- **调用位置**: `apply_scene_boundary_fusion()` 接收 `speech_intervals` 参数
- **数据来源**: `source_windows/stage.py` 产出的 `speech_intervals.json`
- **消费方式**: `_extract_speech_intervals(speech_data, episode_id)` → 按集提取语音区间列表
- **用途**: 音频门控 snap — 避免切点落在语音中间

#### 消费者 2: `junction_edits.py`（right_av_overlap 策略）

- **调用位置**: `_load_speech_intervals(job_root, audio_boundary_report_path)` (L976-1003)
- **数据来源**: audio_boundary_report 中 `source_analyses[].vad_path` 指向的 VAD JSON
- **消费方式**: `_simultaneous_speech_seconds()` 计算左右 Clip 重叠区间的并发语音量
- **用途**: 验证 right_av_overlap 的同时语音安全性（≤0.1s）

#### 消费者 3: `highlight_evolution.py`

- **调用位置**: 通过 `apply_scene_boundary_fusion` 间接消费
- **用途**: 高光演化中传递 VAD 数据给 fusion

#### 消费者 4: `story_qc/stage.py`

- **调用位置**: `prepare_audio_boundary_with_repair()` 导入 audio boundary 相关逻辑
- **用途**: QC 阶段的音频边界检查

### 4. VAD 参数体系

#### AudioBoundaryPolicy（contracts/audio_boundary.py）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `demucs_model` | "htdemucs" | Demucs 模型名 |
| `vad_threshold` | 0.25 | demucs_vocals 轨道阈值 |
| `vad_threshold_original` | 0.45 | original_mix 轨道阈值 |
| `vad_threshold_no_vocals` | 0.55 | no_vocals 轨道阈值 |
| `min_speech_duration_ms` | 100 | 最小语音段时长 |
| `min_silence_duration_ms` | 350 | 最小静音段时长 |
| `speech_pad_ms` | 120 | 语音两端填充 |
| `minimum_safe_gap_seconds` | 0.35 | 最小安全间隙 |

#### Smart Merge 参数（vad.py）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `extend_window` | 1.5s | original_mix 扩展 demucs 边界的最大距离 |
| `phrase_gap` | 0.15s | demucs 间隙 ≤ 此值视为同短语 |
| `merge_min_gap` | 0.15s | 最终合并的最小间隙 |

#### Fusion 场景覆盖（source_windows/stage.py）

Fusion VAD 使用更敏感的参数:
- `vad_threshold`: 默认 0.25（vs story_qc 的 0.50）
- `min_speech_ms`: 80（vs 默认 100）
- `min_silence_ms`: 200（vs 默认 350）
- `speech_pad_ms`: 150（vs 默认 120）

### 5. VAD Venv 位置

`.venv-audio-boundary` 实际位于:
```
/Users/liuzx/Code/python/work_ai/auto_cut_bot/.venv-audio-boundary/
```

搜索策略（source_windows/stage.py L1124-1148）:
1. `cfg.vad_python` / `cfg.audio_boundary_python` 显式配置
2. `job_root / vad_venv / bin/python`
3. `cwd / vad_venv / bin/python`
4. `autocut_core 包根 / vad_venv / bin/python`
5. `cwd / .venv-audio-boundary / bin/python`
6. `job_root / .venv-audio-boundary / bin/python`

## Caveats

1. **VAD 不入 DB**: 完全基于文件缓存，没有 DB 表
2. **VAD venv 路径容易混淆**: 实际在 `auto_cut_bot/.venv-audio-boundary/`，不在 workspace 根目录
3. **三路 VAD 合并策略复杂**: demucs_vocals 为结构主导，original_mix 仅扩展边界不桥接间隙，no_vocals 捕获喊叫声
4. **缓存按 config_hash 分桶**: 修改任何 VAD 参数会自动失效缓存
