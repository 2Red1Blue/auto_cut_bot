# Research: scene_boundary_fusion.py — Snap 逻辑分析

- **Query**: snap 逻辑完整分析，包括 _find_speech_boundary_start/end、needs_fade_fallback、snap 函数输入输出
- **Scope**: internal
- **Date**: 2026-08-14

## 文件位置

| 文件路径 | 说明 |
|---|---|
| `auto_cut_bot/packages/autocut-core/autocut_core/semantic/scene_boundary_fusion.py` | 主实现，1089 行 |

## Findings

### 1. 整体架构

该模块负责将 VLM 分析结果中的事件时间戳对齐到 PySceneDetect 检测到的镜头边界，支持**视觉 snap + 音频门控**两层策略。

三种模式（按优先级）:
1. **VAD 模式** (`speech_intervals` 不为 None): Demucs+Silero 语音区间，最高优先级
2. **Silence 模式** (`silence_intervals` 不为 None): ffmpeg silencedetect 静音区间，fallback
3. **纯视觉模式**: 无音频数据，仅做 tolerance 内的最近切点对齐

### 2. `_find_speech_boundary_start()` 完整逻辑 (L407-528)

**目的**: 当切点落在语音中时，寻找安全的高光起始点。

**参数**:
```python
def _find_speech_boundary_start(
    cut_point: float,            # 当前候选切点
    speech_intervals: list,      # VAD 语音区间 [{start, end}, ...]
    max_shift: float = 5.0,     # 最大调整距离
    speech_lead: float = 0.15,  # 语音起点前的安全前导
    *,
    original_timestamp: float | None = None,  # 原始 VLM 时间戳（预算锚点）
    gap_threshold: float = 0.7,        # 短间隙穿透阈值
    min_gap_duration: float = 0.25,    # 安全切点所需最小间隙
    max_cumulative_gap: float = 1.0,   # 穿透累计间隙上限
    max_penetration_speech: float = 2.0,  # 穿透带回的额外语音时长上限
) -> float | None
```

**策略（严格优先级）**:
1. **回退到语音段起点 - speech_lead**: 找到包含 cut_point 的语音段，取其 start - speech_lead
2. **短间隙穿透**: 从包含段向前遍历，穿过 ≤gap_threshold(0.7s) 的短间隙，找到对话轮次的真正起点
   - 累计间隙 ≤ max_cumulative_gap(1.0s)
   - 带回的额外语音时长 ≤ max_penetration_speech(2.0s)
   - 结果必须在 anchor ± max_shift 范围内
3. **搜索附近安全语音间隙**: 如果回退/穿透都超预算，在前后 max_shift 范围内找 ≥min_gap_duration(0.25s) 的语音间隙
   - 优先选 anchor 所在的间隙
   - 其次选距 anchor 最近的间隙边缘

**返回值**: 安全时间点 `float`，或 `None`（找不到）

### 3. `_find_speech_boundary_end()` 完整逻辑 (L531-634)

与 start 版本**完全对称**，方向相反：

**参数差异**:
- `tail: float = 0.25` (语音终点后的安全余量，对应 start 的 speech_lead)
- 向后遍历而非向前

**策略**:
1. 前进到语音段结束 + tail
2. 向后短间隙穿透（同样受 gap_threshold, max_cumulative_gap, max_penetration_speech 约束）
3. 搜索附近安全语音间隙

### 4. `needs_fade_fallback` 标记

**当前状态: 未实现**

该标记仅存在于设计文档 `docs/vad-audio-snap-design.md` 中：
- 文档描述了当 snap 找不到安全切点时，标记 `needs_fade_fallback=true`
- 下游 junction_edits 可以据此应用 crossfade 或 J-cut
- **但当前代码中**:
  - snap 函数返回 `float`（不是元组），不带 fade 标记
  - junction_edits.py 中没有任何代码读取 `needs_fade_fallback`
  - 当 snap 失败时，行为是**返回原始 VLM 时间戳**（不 snap）

文档中的设计意图:
```
2. 添加needs_fade_fallback标记（返回元组 (safe_point, needs_fade)）
```
但这只是 TODO，未落地。

### 5. 公开 snap 函数

#### `snap_highlight_start()` (L837-961)

**输入**:
```python
def snap_highlight_start(
    timestamp: float,              # VLM 输出的开始时间
    cut_points: list[float],       # 排序后的切点列表
    tolerance: float = 0.5,        # 纯视觉模式容差
    lead_in: float = 0.3,          # 切点后偏移
    *,
    silence_intervals: list | None = None,   # 静音区间（fallback）
    speech_intervals: list | None = None,    # VAD 语音区间（优先）
    max_shift: float = 5.0,        # 音频感知模式最大搜索距离
) -> float
```

**VAD 模式策略**:
1. 在 `timestamp ± max_shift` 范围内搜索所有视觉切点
2. 对每个切点做音频安全检查 (`_check_cut`):
   - 切点和 lead_in 候选点都不在语音中 → 安全，直接 snap
   - 切点在静音但 lead_in 进入语音 → 调用 `_find_speech_boundary_start` 减小 lead_in
   - 切点在语音中 → 调用 `_find_speech_boundary_start` 回退到语音起点
3. 所有视觉切点都不安全 → 纯音频 snap（不依赖视觉切点）
4. 纯音频 snap 也失败 → 返回原始 timestamp（不 snap）

**返回值**: `float` — 对齐后的开始时间

#### `snap_highlight_end()` (L964-1088)

与 start 版本对称，使用 `lead_out` 替代 `lead_in`，`_find_speech_boundary_end` 替代 `_find_speech_boundary_start`。

#### `apply_scene_boundary_fusion()` (L637-758) — 顶层入口

**输入**:
```python
def apply_scene_boundary_fusion(
    vlm_result: dict,              # VLM 分析结果（含 candidates 列表）
    scene_boundaries: dict,        # PySceneDetect 场景边界
    episode_id: str,
    *,
    snap_tolerance: float = 0.5,
    lead_in: float = 0.3,
    lead_out: float = 0.0,
    silence_intervals: dict | None = None,
    speech_intervals: dict | None = None,
    audio_max_shift: float = 3.0,
) -> dict
```

**输出**: 修改后的 vlm_result（原地修改），变更:
- `event["start"]` / `event["end"]` — snap 后的时间
- `event["original_start"]` / `event["original_end"]` — VLM 原始值
- `event["audio_snap_skipped"]` — 音频门控导致 snap 被跳过的标记

### 6. 辅助函数

| 函数 | 行号 | 说明 |
|---|---|---|
| `_extract_silence_intervals()` | L27-47 | 从 silence 数据提取指定集静音区间 |
| `_is_in_silence()` | L50-68 | 检查时间戳是否在静音区间内 |
| `_find_safe_snap_point_start()` | L71-136 | 静音模式下找安全 snap 起点 |
| `_find_safe_snap_point_end()` | L139-196 | 静音模式下找安全 snap 终点 |
| `_extract_speech_intervals()` | L364-383 | 从 VAD 数据提取指定集语音区间 |
| `_is_in_speech()` | L386-404 | 检查时间戳是否在语音区间内 |
| `validate_scene_boundaries()` | L199-357 | 验证场景边界数据质量 |
| `extract_cut_points()` | L761-790 | 从场景边界提取切点列表 |
| `snap_to_boundary()` | L793-834 | 基础 snap（二分查找最近切点） |

## Caveats

1. **needs_fade_fallback 未实现**: snap 返回 float 而非 tuple，无 fade 标记传递给 junction_edits
2. **`audio_snap_skipped` 标记**: 仅在 `apply_scene_boundary_fusion` 中设置，但未见下游消费
3. **max_shift 默认值不一致**: `apply_scene_boundary_fusion` 默认 `audio_max_shift=3.0`，但 `snap_highlight_start/end` 默认 `max_shift=5.0`
