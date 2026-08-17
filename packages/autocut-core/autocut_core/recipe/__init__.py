"""Recipe 模块 — 流水线配方 (可复用的参数化 Pipeline 配置)。

本模块采用与 schema/ 一致的 Pydantic v2 模型定义, 与 registry.py 一致的
文件扫描发现机制, 以及与 io.py 一致的 SHA-256 内容寻址身份判定。

子模块:
  models.py  — Recipe Pydantic 模型 (不可变, 内容寻址)
  registry.py — RecipeRegistry (文件扫描发现 + 按名称/版本/latest 查找)
"""

from autocut_core.recipe.models import Recipe
from autocut_core.recipe.registry import RecipeRegistry

__all__ = ["Recipe", "RecipeRegistry"]