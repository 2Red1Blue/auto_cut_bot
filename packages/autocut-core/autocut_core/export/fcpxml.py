"""FCPXML 1.11 导出器 — 非破坏性, 引用源媒体路径。

将 Story Render Recipe 的时间线映射为 FCPXML 1.11 格式的
sequence/clip 元素。源媒体以 asset 元素引用, 不复制/不转码。

FCPXML 1.11 结构:
  <fcpxml version="1.11">
    <resources>
      <asset id="..." src="file:///path/to/source.mp4" .../>
      <format id="..." .../>
    </resources>
    <library>
      <event name="...">
        <project name="...">
          <sequence ... format="...">
            <spine>
              <clip name="..." offset="..." duration="..." ref="..."/>
              <gap name="..." offset="..." duration="..."/>
            </spine>
          </sequence>
        </project>
      </event>
    </library>
  </fcpxml>
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from autocut_core.contracts.types import ArtifactBus
from autocut_core.export.base import ExportFormat


# FCPXML 时间基 — 所有时间值以 1/240000s 为单位的 rational 表示
_FCPXML_TIMEBASE = 240000


def _seconds_to_fcpxml_ticks(seconds: float) -> str:
    """将秒数转换为 FCPXML rational 时间字符串 (如 "240000/240000s" = 1s)。"""
    ticks = round(seconds * _FCPXML_TIMEBASE)
    return f"{ticks}/{_FCPXML_TIMEBASE}s"


def _seconds_to_fcpxml_duration(seconds: float) -> str:
    """将秒数转换为 FCPXML duration 字符串 (如 "240000/240000s")。"""
    return _seconds_to_fcpxml_ticks(seconds)


def _sanitize_xml_text(value: str) -> str:
    """清理 XML 文本中的非法字符。"""
    return "".join(c for c in value if c.isprintable() or c in "\n\r\t")


class FCPXMLExporter(ExportFormat):
    """FCPXML 1.11 格式导出器。

    将 Story Render Recipe 的时间线映射为 Final Cut Pro 可导入的
    FCPXML 工程文件。源媒体通过 file:// URL 引用, 不复制/不转码。
    """

    format_name: str = "FCPXML 1.11"
    file_extension: str = ".fcpxml"

    def export(self, artifacts: ArtifactBus, output_dir: Path) -> Path:
        """生成 FCPXML 1.11 工程文件。"""
        recipe = self._load_recipe(artifacts)
        output_path = self._resolve_output_path(output_dir, recipe)
        xml_bytes = self._build_fcpxml(recipe)
        _atomic_write_xml(output_path, xml_bytes)
        return output_path

    def _build_fcpxml(self, recipe: dict[str, Any]) -> bytes:
        """构建完整的 FCPXML 文档。"""
        fcpxml = Element("fcpxml", {"version": "1.11"})

        resources = SubElement(fcpxml, "resources")
        format_id = self._build_format(resources, recipe)
        asset_map = self._build_assets(resources, recipe)

        library = SubElement(fcpxml, "library")
        event = SubElement(library, "event", {"name": _sanitize_xml_text(recipe.get("title", "Story"))})
        project = SubElement(event, "project", {"name": _sanitize_xml_text(recipe.get("title", "Story"))})
        timeline = recipe.get("timeline", [])
        total_duration = (
            recipe.get("expected_duration_seconds", 0.0)
            if timeline
            else 0.0
        )
        sequence = SubElement(
            project,
            "sequence",
            {
                "format": format_id,
                "duration": _seconds_to_fcpxml_duration(total_duration),
                "tcStart": "0/240000s",
                "tcFormat": "NDF",
            },
        )
        spine = SubElement(sequence, "spine")

        self._build_spine(spine, recipe, asset_map)

        raw = tostring(fcpxml, encoding="utf-8")
        return _pretty_print_xml(raw)

    def _build_format(self, resources: Element, recipe: dict[str, Any]) -> str:
        """构建 format 资源元素, 返回 format ID。"""
        profile = recipe.get("render_profile", {})
        width = int(profile.get("width", 1080))
        height = int(profile.get("height", 1920))
        fps = str(profile.get("fps", "25"))

        format_id = "r1"
        SubElement(
            resources,
            "format",
            {
                "id": format_id,
                "name": f"FFVideoFormat{height}p{fps}",
                "frameDuration": f"{_FCPXML_TIMEBASE // int(float(fps))}/{_FCPXML_TIMEBASE}s",
                "width": str(width),
                "height": str(height),
                "colorSpace": "1-1-1 (Rec. 709)",
            },
        )
        return format_id

    def _build_assets(
        self, resources: Element, recipe: dict[str, Any]
    ) -> dict[str, str]:
        """构建 asset 资源元素, 返回 {source_id: asset_ref_id} 映射。"""
        sources = recipe.get("sources", [])
        asset_map: dict[str, str] = {}
        for source in sources:
            source_id = source.get("source_id", "")
            if not source_id:
                continue
            path = source.get("path", "")
            asset_id = f"asset_{len(asset_map) + 1}"
            asset_map[source_id] = asset_id
            asset_attrs = {
                "id": asset_id,
                "name": _sanitize_xml_text(source_id),
                "src": _file_url(path),
                "duration": _seconds_to_fcpxml_duration(
                    float(source.get("duration_seconds", 0))
                ),
            }
            episode = source.get("episode")
            if episode is not None:
                asset_attrs["name"] = _sanitize_xml_text(
                    f"EP{int(episode)} — {source_id}"
                )
            SubElement(resources, "asset", asset_attrs)
        return asset_map

    def _build_spine(
        self,
        spine: Element,
        recipe: dict[str, Any],
        asset_map: dict[str, str],
    ) -> None:
        """将 recipe timeline 映射为 spine 内的 clip/gap 元素。"""
        timeline = recipe.get("timeline", [])
        if not timeline:
            return

        clips = {item["id"]: item for item in recipe.get("clips", [])}
        transitions = {item["id"]: item for item in recipe.get("transitions", [])}

        for item in timeline:
            kind = item.get("kind", "clip")
            offset = float(item.get("start_seconds", 0))
            duration = float(item.get("duration_seconds", 0))

            if kind == "clip":
                ref_id = item.get("ref_id", "")
                clip = clips.get(ref_id)
                if clip is None:
                    continue
                source_id = clip.get("source_id", "")
                asset_ref = asset_map.get(source_id, "")
                if not asset_ref:
                    continue
                source_start = float(clip.get("source_start", 0))
                clip_elem = SubElement(
                    spine,
                    "clip",
                    {
                        "name": _sanitize_xml_text(clip.get("id", ref_id)),
                        "offset": _seconds_to_fcpxml_ticks(offset),
                        "duration": _seconds_to_fcpxml_duration(duration),
                        "ref": asset_ref,
                        "start": _seconds_to_fcpxml_ticks(source_start),
                    },
                )
                # 添加 metadata 记录源剪辑的原始信息
                md = SubElement(clip_elem, "metadata")
                md_key = SubElement(
                    md,
                    "md",
                    {
                        "key": "com.apple.proapps.originalClipProperties",
                        "value": _sanitize_xml_text(
                            f"source={source_id} "
                            f"episode={clip.get('episode', '')} "
                            f"block={clip.get('block_id', '')}"
                        ),
                    },
                )

            elif kind == "transition":
                ref_id = item.get("ref_id", "")
                transition = transitions.get(ref_id)
                if transition is None:
                    continue
                # 转场在 FCPXML 中表示为 gap (间隙) — 在 spine 中插入一个
                # 持续时间为转场时长的空白 clip, 让编辑器识别为编辑点
                gap_elem = SubElement(
                    spine,
                    "gap",
                    {
                        "name": _sanitize_xml_text(
                            f"Transition-{transition.get('type', 'cut')}"
                        ),
                        "offset": _seconds_to_fcpxml_ticks(offset),
                        "duration": _seconds_to_fcpxml_duration(duration),
                    },
                )


def _file_url(path: str) -> str:
    """将文件路径转换为 file:// URL。"""
    from pathlib import Path

    resolved = Path(path).expanduser().resolve()
    return resolved.as_uri()


def _pretty_print_xml(raw: bytes) -> bytes:
    """格式化 XML 输出 (UTF-8, XML declaration)。"""
    dom = minidom.parseString(raw)
    return dom.toprettyxml(indent="  ", encoding="utf-8")


def _atomic_write_xml(path: Path, xml_bytes: bytes) -> None:
    """原子写入 XML 文件。"""
    from autocut_core.io import atomic_write_text

    atomic_write_text(path, xml_bytes.decode("utf-8"))