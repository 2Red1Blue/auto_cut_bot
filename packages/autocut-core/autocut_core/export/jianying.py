"""剪映 (Jianying) Draft JSON 导出器 — 非破坏性, 引用源媒体路径。

将 Story Render Recipe 的 time line 映射为 剪映 draft_info.json 格式。
源媒体在 materials 中以路径引用, 不复制/不转码; 剪辑段以 segments
映射到对应的 track 上。

剪映 Draft JSON 结构:
  {
    "platform": {"os": "mac"},
    "draft_name": "Story Title",
    "draft_id": "...",
    "draft_materials": {
      "videos": [{ "id": "...", "path": "/path/to/source.mp4", ... }],
      "audios": [...],
      "texts": [...],
      "effects": [...],
      "transitions": [...],
      "filters": [...],
      "stickers": [...],
      "beautifications": [...]
    },
    "draft_tracks": [
      {
        "id": "...",
        "type": "video",
        "segments": [
          {
            "id": "...",
            "material_id": "...",
            "source_timerange": { "start": 0, "duration": 1234567890 },
            "target_timerange": { "start": 0, "duration": 1234567890 },
            "speed": 1.0,
            "volume": 1.0
          }
        ]
      }
    ],
    "draft_content": {...}
  }
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from autocut_core.contracts.types import ArtifactBus
from autocut_core.export.base import ExportFormat


# 剪映内部时间单位: 纳秒 (nanosecond)
_JIANYING_TIMEBASE_NS = 1_000_000_000


def _seconds_to_ns(seconds: float) -> int:
    """将秒数转换为纳秒整数。"""
    return int(round(float(seconds) * _JIANYING_TIMEBASE_NS))


def _make_uuid() -> str:
    """生成 剪映 兼容的 UUID (无连字符, 大写)。"""
    return uuid.uuid4().hex.upper()


class JianyingExporter(ExportFormat):
    """剪映 Draft JSON 导出器。

    将 Story Render Recipe 的时间线映射为 剪映 可导入的 draft_info.json
    工程文件。源媒体通过路径引用, 不复制/不转码。

    支持的 mapping:
      - Source clips → video track segments
      - Transitions → 转场效果 (通过 transition materials + segments)
      - Render profile → canvas 配置
    """

    format_name: str = "Jianying Draft JSON"
    file_extension: str = ".draft.json"

    def export(self, artifacts: ArtifactBus, output_dir: Path) -> Path:
        """生成 剪映 draft JSON 工程文件。"""
        recipe = self._load_recipe(artifacts)
        output_path = self._resolve_output_path(output_dir, recipe)
        draft = self._build_draft(recipe)
        self._atomic_write_draft(output_path, draft)
        return output_path

    def _build_draft(self, recipe: dict[str, Any]) -> dict[str, Any]:
        """构建完整的 剪映 draft JSON 结构。"""
        profile = recipe.get("render_profile", {})
        width = int(profile.get("width", 1080))
        height = int(profile.get("height", 1920))
        fps = float(profile.get("fps", 25))

        title = str(recipe.get("title", "Story"))
        story_id = str(recipe.get("story_id", ""))

        # 构建源媒体 material 列表
        sources = recipe.get("sources", [])
        source_map = self._build_materials(sources, width, height)

        # 构建 timeline 映射
        timeline = recipe.get("timeline", [])
        clips = {item["id"]: item for item in recipe.get("clips", [])}
        transitions = {item["id"]: item for item in recipe.get("transitions", [])}

        track_id = _make_uuid()
        segments = self._build_segments(timeline, clips, transitions, source_map, fps)

        draft = {
            "platform": {"os": "mac"},
            "draft_name": title,
            "draft_id": _make_uuid(),
            "draft_cover": "",
            "draft_folders": [],
            "draft_timeline_metas": {
                "video_track_volume": 1.0,
                "max_video_track_count": 1,
                "has_audio_track": True,
            },
            "draft_materials": {
                "videos": [
                    {
                        "id": mat["mat_id"],
                        "path": mat["path"],
                        "type": "video",
                        "duration": mat["duration_ns"],
                        "width": width,
                        "height": height,
                    }
                    for mat in source_map.values()
                ],
                "audios": [],
                "texts": [],
                "effects": [],
                "transitions": [],
                "filters": [],
                "stickers": [],
                "beautifications": [],
            },
            "draft_tracks": [
                {
                    "id": track_id,
                    "type": "video",
                    "segments": segments,
                }
            ],
            "draft_content": {
                "canvas_config": {
                    "width": width,
                    "height": height,
                    "ratio": "9:16",
                    "scale": 1.0,
                },
                "auto_background": False,
            },
            "draft_removed_materials": [],
            "draft_meta": {
                "created_at": _make_uuid(),
                "edited_at": _make_uuid(),
                "source": "autocut_core",
                "story_id": story_id,
            },
        }
        return draft

    def _build_materials(
        self, sources: list[dict[str, Any]], width: int, height: int
    ) -> dict[str, dict[str, Any]]:
        """构建 source_id -> material 映射。

        返回的每个 material 包含:
          - mat_id: 剪映 material ID
          - path: 源文件绝对路径
          - duration_ns: 源文件时长 (纳秒)
        """
        source_map: dict[str, dict[str, Any]] = {}
        for source in sources:
            source_id = source.get("source_id", "")
            if not source_id:
                continue
            path = source.get("path", "")
            source_map[source_id] = {
                "mat_id": _make_uuid(),
                "path": path,
                "duration_ns": _seconds_to_ns(
                    float(source.get("duration_seconds", 0))
                ),
            }
        return source_map

    def _build_segments(
        self,
        timeline: list[dict[str, Any]],
        clips: dict[str, dict[str, Any]],
        transitions: dict[str, dict[str, Any]],
        source_map: dict[str, dict[str, Any]],
        fps: float,
    ) -> list[dict[str, Any]]:
        """将 timeline 项映射为 剪映 track segments。

        每个 clip 生成一个 segment:
          - source_timerange: 源素材中的入出点 (纳秒)
          - target_timerange: 时间线上的位置 (纳秒)
          - speed: 恒定 1.0 (不变速)
          - volume: 1.0 (保留原音频)

        转场 (transition) 以 gap 形式插入, 在 剪映 中体现为编辑点。
        """
        segments: list[dict[str, Any]] = []
        for item in timeline:
            kind = item.get("kind", "clip")
            ref_id = item.get("ref_id", "")
            target_start_ns = _seconds_to_ns(float(item.get("start_seconds", 0)))
            duration_ns = _seconds_to_ns(float(item.get("duration_seconds", 0)))

            if kind == "clip":
                clip = clips.get(ref_id)
                if clip is None:
                    continue
                source_id = clip.get("source_id", "")
                mat = source_map.get(source_id)
                if mat is None:
                    continue
                source_start_ns = _seconds_to_ns(float(clip.get("source_start", 0)))
                source_duration_ns = _seconds_to_ns(
                    float(clip.get("duration_seconds", 0))
                )

                segments.append(
                    {
                        "id": _make_uuid(),
                        "material_id": mat["mat_id"],
                        "source_timerange": {
                            "start": source_start_ns,
                            "duration": source_duration_ns,
                        },
                        "target_timerange": {
                            "start": target_start_ns,
                            "duration": duration_ns,
                        },
                        "speed": 1.0,
                        "volume": 1.0,
                        "clip_metadata": {
                            "clip_id": clip.get("id", ""),
                            "block_id": clip.get("block_id", ""),
                            "block_role": clip.get("block_role", ""),
                            "episode": clip.get("episode", ""),
                        },
                    }
                )

            elif kind == "transition":
                transition = transitions.get(ref_id)
                if transition is None:
                    continue
                # 转场在 剪映 中作为带 transition 标记的 segment
                transition_type = transition.get("type", "black_separator")
                segments.append(
                    {
                        "id": _make_uuid(),
                        "material_id": "",
                        "source_timerange": {"start": 0, "duration": 0},
                        "target_timerange": {
                            "start": target_start_ns,
                            "duration": duration_ns,
                        },
                        "speed": 1.0,
                        "volume": 0.0,
                        "is_transition_gap": True,
                        "transition_type": transition_type,
                        "transition_metadata": {
                            "transition_id": transition.get("id", ""),
                            "from_clip_id": transition.get("from_clip_id", ""),
                            "to_clip_id": transition.get("to_clip_id", ""),
                            "audio_policy": transition.get("audio_policy", "silence"),
                        },
                    }
                )

        return segments

    def _atomic_write_draft(self, path: Path, draft: dict[str, Any]) -> None:
        """原子写入 剪映 draft JSON 文件。"""
        from autocut_core.io import atomic_write_json

        atomic_write_json(path, draft)