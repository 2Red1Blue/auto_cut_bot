"""Recipe 注册表 — 文件扫描发现 + 按名称/版本/latest 查找。

发现源: 文件扫描 — 遍历 plugins/*/recipes/*.json
         entry_points — pip 安装的插件 (辅助)

同名冲突: 文件扫描优先 — entry_points 不会覆盖文件扫描发现的同名 Recipe。

Recipe 文件名约定: {name}.json
版本号写入 JSON 文件内 version 字段, latest 按 semver 排序取最大版本。

与 StageRegistry 一致的发现模式:
  - 文件扫描为主 (plugins/*/recipes/*.json)
  - entry_points 为辅 (ac_cutflow.recipes 组)
  - 单个文件解析失败不中断整体扫描 (记录警告)
"""

from __future__ import annotations

import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from autocut_core.io import load_json
from autocut_core.logging import get_logger
from autocut_core.recipe.models import Recipe

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Semver 解析: 拆分为 (major, minor, patch, pre_release) 元组用于排序
# ---------------------------------------------------------------------------
_SEMVER_PARSE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
)


def _parse_semver(version: str) -> tuple[int, int, int, str]:
    """解析 semver 字符串为可排序元组。

    pre-release 版本排序规则: 有 pre-release 的版本 < 无 pre-release 的版本
    (semver 规范: 1.0.0-alpha < 1.0.0)。
    同一 pre-release 内按字符串字典序比较。
    """
    match = _SEMVER_PARSE.match(version)
    if not match:
        raise ValueError(f"非法 semver 版本号: {version!r}")
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3))
    pre = match.group(4) or ""
    return (major, minor, patch, pre)


def _semver_sort_key(version: str) -> tuple[int, int, int, int, str]:
    """将 semver 转为排序键: pre-release 版本排在 release 版本之前。

    semver 优先规则: 1.0.0-alpha < 1.0.0.
    pre_nonempty=0 表示有 pre-release (排在前面), =1 表示 release (排在后面)。
    """
    major, minor, patch, pre = _parse_semver(version)
    pre_nonempty = 0 if pre else 1
    return (major, minor, patch, pre_nonempty, pre)


class RecipeRegistry:
    """发现并索引所有已注册的 Recipe。

    扫描路径: plugins/*/recipes/*.json
    索引方式: {name: {version: Recipe}} 支持按名称+精确版本或 latest 查找。
    """

    def __init__(self) -> None:
        self._recipes: dict[str, dict[str, Recipe]] = {}

    # ── 发现 ──────────────────────────────────────────────────────────

    def discover(self) -> dict[str, dict[str, Recipe]]:
        """扫描所有可用的 Recipe, 返回 {name: {version: Recipe}} 映射。

        主要机制: 文件约定发现 — plugins/*/recipes/*.json
        辅助机制: entry_points — pip 安装的插件

        文件发现的 Recipe 优先 — entry_points 不会覆盖同名同版本 Recipe。
        """
        self._discover_from_files()

        # 辅助: entry_points
        try:
            eps = entry_points(group="ac_cutflow.recipes")
        except TypeError:
            eps = entry_points().get("ac_cutflow.recipes", [])
        for ep in eps:
            try:
                recipe = ep.load()
                if isinstance(recipe, Recipe):
                    self._register(recipe, source=f"entry_point:{ep.name}")
            except (ImportError, AttributeError, TypeError):
                logger.warning(
                    "entry_point Recipe 加载失败: %s (%s)",
                    ep.name, ep.value, exc_info=True,
                )

        return self._recipes

    def _discover_from_files(self) -> None:
        """遍历 plugins/*/recipes/*.json 查找 Recipe 文件。"""
        plugins_root = _PROJECT_ROOT / "plugins"
        if not plugins_root.is_dir():
            return

        for plugin_dir in sorted(plugins_root.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            recipes_dir = plugin_dir / "recipes"
            if not recipes_dir.is_dir():
                continue

            for recipe_file in sorted(recipes_dir.glob("*.json")):
                if recipe_file.name.startswith("."):
                    continue
                try:
                    self._load_recipe_file(recipe_file)
                except (OSError, ValueError, TypeError) as exc:
                    logger.warning(
                        "Recipe 文件解析失败: %s — %s", recipe_file, exc
                    )

    def _load_recipe_file(self, path: Path) -> None:
        """从单个 JSON 文件加载 Recipe 并注册。"""
        data = load_json(path)
        if not isinstance(data, dict):
            logger.warning("Recipe 文件不是 JSON 对象: %s", path)
            return

        # 从文件内容反序列化 Recipe
        recipe = Recipe(**data)
        # 反序列化后校验完整性
        if not recipe.verify_hash():
            logger.warning(
                "Recipe content_hash 不匹配: %s (name=%s version=%s) — "
                "文件可能被手动编辑, 建议重新导出",
                path, recipe.name, recipe.version,
            )
        self._register(recipe, source=str(path))

    def _register(self, recipe: Recipe, *, source: str = "") -> None:
        """将 Recipe 注册到索引中。

        文件扫描优先: 同名同版本 Recipe 不覆盖 (保留文件扫描的)。
        """
        name_map = self._recipes.setdefault(recipe.name, {})
        if recipe.version in name_map:
            logger.debug(
                "Recipe %s/%s 已存在 (来源: %s), 跳过 %s",
                recipe.name, recipe.version,
                name_map[recipe.version].content_hash, source,
            )
            return
        name_map[recipe.version] = recipe
        logger.debug("Recipe 注册成功: %s/%s (来源: %s)", recipe.name, recipe.version, source)

    # ── 查找 ──────────────────────────────────────────────────────────

    def get(self, name: str, version: str) -> Recipe | None:
        """按名称和精确版本查找 Recipe, 未找到返回 None。"""
        name_map = self._recipes.get(name, {})
        return name_map.get(version)

    def latest(self, name: str) -> Recipe | None:
        """按名称查找最新版本 (semver 排序最大)。

        返回 semver 最高版本; 不存在时返回 None。
        """
        name_map = self._recipes.get(name, {})
        if not name_map:
            return None
        versions = sorted(name_map.keys(), key=_semver_sort_key)
        return name_map[versions[-1]]

    def list_names(self) -> list[str]:
        """列出全部已注册 Recipe 的名称 (排序后)。"""
        return sorted(self._recipes.keys())

    def list_versions(self, name: str) -> list[str]:
        """列出指定名称 Recipe 的全部版本 (semver 从低到高排序)。"""
        name_map = self._recipes.get(name, {})
        return sorted(name_map.keys(), key=_semver_sort_key)

    def find_by_hash(self, content_hash: str) -> Recipe | None:
        """按 content_hash 查找 Recipe (跨项目身份判定)。

        返回第一个匹配的 Recipe; 不存在时返回 None。
        """
        for name_map in self._recipes.values():
            for recipe in name_map.values():
                if recipe.content_hash == content_hash:
                    return recipe
        return None

    # ── 合并 ──────────────────────────────────────────────────────────

    def resolve_merged(
        self,
        name: str,
        *,
        version: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> Recipe | None:
        """按名称 (可选版本) 查找 Recipe, 可选合并 override。

        流程:
          1. 查找 base Recipe (version 为 None 时取 latest)
          2. 若 overrides 非空, 以 base 为底合并 overrides 生成新 Recipe
          3. 返回合并后的 Recipe

        未找到 base 时返回 None。
        """
        base = self.get(name, version) if version else self.latest(name)
        if base is None:
            return None
        if overrides is None:
            return base
        return base.merge(**overrides)

    def merge_two(
        self,
        base_name: str,
        override_name: str,
        *,
        base_version: str | None = None,
        override_version: str | None = None,
    ) -> Recipe | None:
        """用两个已注册 Recipe 执行合并: base + override。

        两个 Recipe 都未注册时返回 None; 仅 base 未注册时返回 override。
        """
        base = self.get(base_name, base_version) if base_version else self.latest(base_name)
        override = self.get(override_name, override_version) if override_version else self.latest(override_name)

        if base is None and override is None:
            return None
        if base is None:
            return override
        if override is None:
            return base
        return base.merge(
                style_params=override.style_params,
                stage_overrides=override.stage_overrides,
            )

    # ── 从字典构建 (用于无需文件扫描的编程式使用) ──────────────────────

    def register_dict(self, data: dict[str, Any]) -> Recipe:
        """从字典注册 Recipe (编程式使用, 不写文件)。

        返回注册后的 Recipe 实例。
        """
        recipe = Recipe(**data)
        self._register(recipe, source="programmatic")
        return recipe