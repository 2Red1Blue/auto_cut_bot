# Research: junction_edits.py 模块分析

- **Query**: junction_edits.py 的 J-cut/L-cut 实现、crossfade 参数、B-roll 视觉桥接、与 snap 的衔接接口
- **Scope**: internal
- **Date**: 2026-08-14

## 文件位置

| 文件路径 | 说明 |
|---|---|
| `auto_cut_bot/packages/autocut-core/autocut_core/libs/junction_edits.py` | 主实现，1204 行 |

## Findings

### 1. 整体架构

该模块**不涉及 J-cut / L-cut / crossfade** 这些传统剪辑概念。它的职责是：

> 将 operator 审核过的 junction 约束编译为不可变的 edit plan（确定性、本地、无模型调用）。

核心是 **audio-tail visual repair** — 当左 Clip 的视频在音频结束前被截断时，用一段视觉桥接或右 Clip 的 A/V 重叠来遮盖"音频尾巴"。

### 2. 两种策略（Strategy）

```python
REVIEWED_BRIDGE = "reviewed_bridge"       # B-roll 视觉桥接
RIGHT_AV_OVERLAP = "right_av_overlap"     # 右 Clip A/V 重叠
```

#### 2.1 REVIEWED_BRIDGE（B-roll 视觉桥接）

- **触发条件**: operator 在 constraints 中指定 `strategy: "reviewed_bridge"` + `bridge_candidate`
- **实现逻辑** (L515-578):
  1. 计算 `audio_tail_duration = left_audio_end - left_video_end`（左 Clip 音频比视频多出的部分）
  2. 将 audio_tail 对齐到帧数：`frame_count = ceil(audio_tail_duration * output_fps)`
  3. 从 `bridge_candidate` 指定的 Source 中取一段 `safe_start ~ safe_end` 的静音视觉片段
  4. 桥接段视频静音 (`audio_policy: "mute"`)，如果桥接比音频尾巴长，填充静音 (`audio_padding: silence`)
- **输出格式**:
  ```json
  {
    "bridge": {
      "source_id": "...", "source_start": 0.0, "source_end": 1.2,
      "duration_seconds": 1.2, "frame_count": 30, "audio_policy": "mute"
    },
    "audio_padding": {"type": "silence", "duration_seconds": 0.0},
    "duration_delta_seconds": 0.0
  }
  ```

#### 2.2 RIGHT_AV_OVERLAP（右 Clip A/V 重叠）

- **触发条件**: operator 指定 `strategy: "right_av_overlap"` + `right_entry_visual_review: "safe"`
- **限制**: 仅限同集 body Clips（不能是 teaser，不能跨集）
- **实现逻辑** (L579-654):
  1. audio_tail 不能超过 `HARD_MAX_OVERLAP_SECONDS = 1.2s`
  2. 右 Clip 必须有足够的 A/V handle 容纳重叠
  3. **关键**: 需要双轨 VAD 证据（`speech_intervals_by_source`），检查重叠区间内左右两侧的同时语音量
  4. 同时语音不能超过 `MAX_SIMULTANEOUS_SPEECH_SECONDS = 0.1s`（极严格）
  5. 计算左右音频淡入淡出：
     - `left_audio_fade_out_seconds`: 默认 0.25s
     - `right_audio_fade_in_seconds`: 默认 0.05s
  6. 必须绑定 `audio_boundary_report_sha256` 指纹
- **输出格式**:
  ```json
  {
    "overlap": {
      "duration_seconds": 0.8,
      "left_audio_fade_out_seconds": 0.25,
      "right_audio_fade_in_seconds": 0.05,
      "simultaneous_speech_seconds": 0.0,
      "max_simultaneous_speech_seconds": 0.1,
      "right_entry_visual_review": "safe",
      "right_av_sync_offset_seconds": 0.0
    },
    "duration_delta_seconds": -0.8
  }
  ```

### 3. 与 Snap 的衔接接口

#### 输入（来自上游）

- **effective_story_plan**: 包含 blocks → clips，每个 clip 有 `source_id`, `source_start`, `source_end`
- **constraints document**: operator 提供的 junction 约束（`from_clip_id`, `to_clip_id`, `left_video_end_seconds` 等）
- **source_manifest** + **local_source_manifest**: Source 的 duration 等信息
- **audio_boundary_report**: 包含 VAD 的 `speech_intervals` 路径（通过 `_load_speech_intervals()` 加载）
- **speech_intervals_by_source**: `dict[source_id, list[{start, end}]]` — 这是 VAD 产出的语音区间

#### 输出（给下游）

- **Junction Edit Plan JSON** (`{story_id}.json`): 包含编译后的 edits 列表
- **Junction Edit Index** (`index.json`): 所有 Story 的 plan 索引
- 每个 edit 包含完整的 `bridge` 或 `overlap` 信息，供渲染阶段消费

#### 与 snap 的关系

- junction_edits **不直接调用** snap 函数
- 它消费 VAD 的 `speech_intervals` 来验证 `right_av_overlap` 的同时语音安全性
- snap 逻辑在 `scene_boundary_fusion.py` 中，产出的高光时间戳经 snap 后进入 effective plan，再被 junction_edits 消费
- `needs_fade_fallback` 标记在 junction_edits 中**不存在** — 它只存在于设计文档 `vad-audio-snap-design.md` 中，是 snap 层和 junction_edits 层之间的桥梁概念，但**当前代码未实现**

### 4. 关键常量

| 常量 | 值 | 说明 |
|---|---|---|
| `DEFAULT_MAX_AUDIO_TAIL_SECONDS` | 2.0 | 音频尾巴最大允许时长 |
| `HARD_MAX_OVERLAP_SECONDS` | 1.2 | right_av_overlap 硬上限 |
| `MAX_SIMULTANEOUS_SPEECH_SECONDS` | 0.1 | 同时语音安全上限 |
| `DEFAULT_LEFT_AUDIO_FADE_OUT_SECONDS` | 0.25 | 左侧淡出 |
| `DEFAULT_RIGHT_AUDIO_FADE_IN_SECONDS` | 0.05 | 右侧淡入 |
| `DEFAULT_OUTPUT_FPS` | 25 | 输出帧率 |
| `TIME_TOLERANCE_SECONDS` | 0.002 | 浮点容差 |

## Caveats

1. **没有 J-cut / L-cut / crossfade**: 该模块做的是 audio-tail visual repair，不是传统意义上的 J/L-cut
2. **needs_fade_fallback 未实现**: 设计文档中描述了这个标记，但代码中 snap 返回值不带此标记
3. **crossfade 参数**: 只有 `left_audio_fade_out_seconds` 和 `right_audio_fade_in_seconds`，是简单的音频淡入淡出，不是视频 crossfade
