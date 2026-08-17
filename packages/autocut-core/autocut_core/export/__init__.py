"""autocut_core.export — 非破坏性导出模块。

将 Story Render Recipe 转换为第三方编辑软件的项目格式:
  - FCPXMLExporter: Final Cut Pro XML 1.11
  - JianyingExporter: 剪映 Draft JSON

所有导出器遵循非破坏性原则: 只引用源媒体路径, 不复制/不转码。
"""

from autocut_core.export.base import ExportFormat
from autocut_core.export.fcpxml import FCPXMLExporter
from autocut_core.export.jianying import JianyingExporter
from autocut_core.export.registry import ExportRegistry

__all__ = [
    "ExportFormat",
    "FCPXMLExporter",
    "JianyingExporter",
    "ExportRegistry",
]