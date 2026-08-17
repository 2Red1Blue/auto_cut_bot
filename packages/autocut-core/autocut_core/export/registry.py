"""ExportRegistry — 通过 entry_points 发现并注册导出器。

发现机制:
  1. entry_points 组 "ac_cutflow.exporters" — pip 安装的导出器插件
  2. 内置导出器 — FCPXMLExporter, JianyingExporter (包内默认注册)

同名冲突: entry_points 优先 — 用户安装的导出器覆盖内置同名导出器。

使用方式:
  registry = ExportRegistry()
  registry.discover()
  exporter = registry.get("fcpxml")
  output_path = exporter.export(bus, output_dir)
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from autocut_core.logging import get_logger

if TYPE_CHECKING:
    from autocut_core.export.base import ExportFormat

logger = get_logger(__name__)


class ExportRegistry:
    """导出器注册中心 — 发现并索引所有可用的导出格式。"""

    def __init__(self) -> None:
        self._exporters: dict[str, "ExportFormat"] = {}

    def discover(self) -> dict[str, "ExportFormat"]:
        """扫描所有可用的导出器, 返回 {name: exporter_instance} 映射。

        发现源:
          1. 内置导出器 (FCPXML, 剪映)
          2. entry_points 组 "ac_cutflow.exporters"

        entry_points 中的导出器覆盖同名内置导出器。
        发现完成后记录已注册格式列表到 info 日志。
        """
        # 1. 内置导出器
        self._discover_builtins()

        # 2. entry_points (覆盖同名内置)
        self._discover_entry_points()

        logger.info(
            "已注册 %d 个导出格式: %s",
            len(self._exporters),
            ", ".join(self.list_formats()),
        )
        return self._exporters

    def _discover_builtins(self) -> None:
        """注册内置导出器 (FCPXML, 剪映)。"""
        from autocut_core.export.fcpxml import FCPXMLExporter
        from autocut_core.export.jianying import JianyingExporter

        for cls in (FCPXMLExporter, JianyingExporter):
            instance = cls()
            key = self._registration_key(instance)
            self._exporters[key] = instance

    def _discover_entry_points(self) -> None:
        """从 entry_points 组 "ac_cutflow.exporters" 加载导出器。

        entry_points 中的导出器覆盖同名内置导出器 — 允许用户安装
        自定义导出器替换默认实现。
        """
        try:
            eps = entry_points(group="ac_cutflow.exporters")
        except TypeError:
            eps = entry_points().get("ac_cutflow.exporters", [])

        for ep in eps:
            try:
                exporter_cls = ep.load()
                instance = exporter_cls()
                if not hasattr(instance, "export"):
                    logger.warning(
                        "entry_point 导出器 %s 缺少 export 方法, 跳过", ep.name
                    )
                    continue
                key = self._registration_key(instance)
                if key in self._exporters:
                    logger.info(
                        "entry_point 导出器 %s 覆盖内置导出器 '%s'",
                        ep.name, key,
                    )
                self._exporters[key] = instance
            except (ImportError, AttributeError, TypeError) as exc:
                logger.warning(
                    "entry_point 导出器加载失败: %s (%s)",
                    ep.name, ep.value, exc_info=True,
                )

    def get(self, name: str) -> "ExportFormat | None":
        """按注册名获取导出器实例, 未注册时返回 None。

        注册名来源:
          - 内置导出器: 类名的 kebab-case 形式 (如 FCPXMLExporter → "fcpxml")
          - entry_points: entry_point 的 name 字段
        """
        return self._exporters.get(name)

    def list_formats(self) -> list[str]:
        """列出全部已注册导出格式的名称 (排序后)。"""
        return sorted(self._exporters.keys())

    def formats_info(self) -> list[dict[str, str]]:
        """返回全部导出格式的元信息列表。

        每项包含:
          - key: 注册名
          - format_name: 可读格式名
          - file_extension: 输出文件扩展名
        """
        return [
            {
                "key": key,
                "format_name": exporter.format_name,
                "file_extension": exporter.file_extension,
            }
            for key, exporter in sorted(self._exporters.items())
        ]

    @staticmethod
    def _registration_key(exporter: "ExportFormat") -> str:
        """从导出器实例推导注册 key。

        内置导出器: 类名去掉 "Exporter" 后缀, 转为 kebab-case。
        如 FCPXMLExporter → "fcpxml", JianyingExporter → "jianying"。
        """
        import re

        class_name = type(exporter).__name__
        # 去掉 "Exporter" 后缀
        if class_name.endswith("Exporter"):
            class_name = class_name[: -len("Exporter")]

        # PascalCase → kebab-case
        key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", class_name)
        key = re.sub(r"([a-z\d])([A-Z])", r"\1-\2", key)
        return key.lower()