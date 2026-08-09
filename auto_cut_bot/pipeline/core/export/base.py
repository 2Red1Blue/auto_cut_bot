"""Export 格式化抽象基类 — 非破坏性导出 (引用源媒体, 不复制)。

每个 Exporter 实现此接口, 将 Story Render Recipe 转换为目标编辑软件的
项目格式 (FCPXML / 剪映 draft JSON / 等)。导出器不修改源文件, 仅生成
引用源媒体路径的工程文件, 下游编辑软件直接打开即可继续编辑。

设计原则:
  - 非破坏性: 导出的工程文件只包含源媒体路径引用, 不复制、不转码
  - 策略模式: 继承 ExportFormat, 实现 export() 方法
  - 注册发现: 通过 ExportRegistry + entry_points 自动发现
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from autocut_core.contracts.types import ArtifactBus


class ExportFormat(ABC):
    """导出格式抽象基类 — 策略模式接口。

    子类需声明:
      - format_name: str 类属性 — 可读格式名 (如 "FCPXML 1.11")
      - file_extension: str 类属性 — 输出文件扩展名 (如 ".fcpxml")

    子类需实现:
      - export(artifacts, output_dir) -> Path — 从 ArtifactBus 读取
        story_render 产物, 生成目标格式的工程文件, 返回输出文件路径。
    """

    format_name: str = ""
    file_extension: str = ""

    @abstractmethod
    def export(self, artifacts: ArtifactBus, output_dir: Path) -> Path:
        """从 ArtifactBus 读取 story_render 产物, 生成导出文件。

        参数:
            artifacts: 流水线产物总线 — 通过 bus.latest("story_render")
                      或 bus.resolve("story_render", "render_recipe") 获取
                      Story Render Recipe。
            output_dir: 输出目录 — 工程文件将写入此目录。

        返回:
            生成的工程文件路径。

        实现约定:
          - 非破坏性: 只生成引用源媒体路径的工程文件, 不复制/转码源文件
          - 幂等: 同一输入多次调用产生相同输出
          - 原子写入: 建议使用 atomic_write_text 落盘, 保证中断恢复正确性
        """
        ...

    def _load_recipe(self, artifacts: ArtifactBus) -> dict[str, Any]:
        """从 ArtifactBus 加载 Story Render Recipe。

        优先取 story_render 最新产物, 否则按名称解析 render_recipe。
        产物内容需为 JSON 对象 (dict), 包含 clips / sources / timeline
        等字段。
        """
        from autocut_core.errors import ArtifactNotFoundError

        ref = artifacts.latest("story_render") or artifacts.resolve(
            "story_render", "render_recipe"
        )
        if ref is None:
            raise ArtifactNotFoundError(
                "story_render/render_recipe 产物未找到 — 请先运行 story_render Stage"
            )
        data = artifacts.get(ref)
        if not isinstance(data, dict):
            raise TypeError(
                f"story_render 产物类型错误: 期望 dict, 实际 {type(data).__name__}"
            )
        return data

    def _resolve_output_path(self, output_dir: Path, recipe: dict[str, Any]) -> Path:
        """根据 recipe 中的 story_id / output_filename 生成输出路径。

        优先使用 recipe 中的 output_filename (去掉原有扩展名, 拼接
        self.file_extension); 否则以 story_id 为文件名。
        """
        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        base = recipe.get("output_filename", "")
        if not base:
            story_id = recipe.get("story_id", "")
            if not story_id:
                raise ValueError("recipe 中缺少 story_id 和 output_filename")
            base = str(story_id)

        stem = Path(base).stem
        filename = f"{stem}{self.file_extension}"
        return output_dir / filename