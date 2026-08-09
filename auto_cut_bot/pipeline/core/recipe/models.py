"""Recipe Pydantic v2 模型 — 可复用的参数化 Pipeline 配方。

每个 Recipe 是一组可调参数的有名快照, 通过 SHA-256 内容寻址实现跨项目
身份判定, 支持 base + override 合并模式。

设计原则:
  - 不可变 (frozen): 一次创建, 永不修改; 修改通过合并生成新 Recipe。
  - 内容寻址: content_hash 由 canonical JSON 计算, 同内容必同哈希。
  - 语义版本: version 遵循 semver (MAJOR.MINOR.PATCH), 支持 latest 查询。
  - 宽松参数: style_params 与 stage_overrides 均为 dict, 不强约束键名,
    由下游 Stage 自行校验参数合法性, 保持 Recipe 层的开放性。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from autocut_core.io import json_sha256

# ---------------------------------------------------------------------------
# Semver 正则: MAJOR.MINOR.PATCH  (可选 pre-release 后缀: -alpha.1, -beta.2)
# ---------------------------------------------------------------------------
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
)

# ---------------------------------------------------------------------------
# 合法 recipe name 字符集: 小写字母、数字、连字符、下划线
# ---------------------------------------------------------------------------
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class Recipe(BaseModel):
    """可复用的流水线配方 — 参数化配置的不可变快照。

    name + version 组成唯一标识; content_hash 保证跨项目身份一致性;
    style_params 存放全局风格参数; stage_overrides 存放逐 Stage 覆盖。

    示例:
        >>> rec = Recipe(
        ...     name="fast-cut",
        ...     version="1.0.0",
        ...     description="快速剪辑风格",
        ...     style_params={"cut_style": "fast", "transition": "dissolve"},
        ...     stage_overrides={"story_plans": {"max_duration": 60}},
        ... )
        >>> rec.content_hash  # 自动计算
    """

    name: str = Field(
        default="",
        description="配方名称 (小写字母开头, 长度 1-64); 仅 merge 时可为空",
    )
    version: str = Field(
        default="",
        description="语义版本号 (semver: MAJOR.MINOR.PATCH); 仅 merge 时可为空",
    )
    description: str = Field(
        default="",
        description="配方用途简述",
    )
    style_params: dict[str, Any] = Field(
        default_factory=dict,
        description="全局风格参数 (如 cut_style, transition, pacing)",
    )
    stage_overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="逐 Stage 参数覆盖 (key 为 Stage 名, value 为参数 dict)",
    )
    content_hash: str = Field(
        default="",
        description="SHA-256 of canonical JSON — 内容寻址身份 (构建时自动计算)",
    )

    model_config = {
        "extra": "forbid",
        "frozen": True,
        "validate_default": True,
    }

    # ── 字段校验 (仅非空时校验, 空字符串用于 merge 占位) ────────────────

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if value and not _NAME_RE.match(value):
            raise ValueError(
                f"Recipe name 必须是 1-64 字符的小写字母/数字/连字符/下划线, "
                f"以小写字母开头, 收到: {value!r}"
            )
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if value and not _SEMVER_RE.match(value):
            raise ValueError(
                f"Recipe version 必须是 semver 格式 (MAJOR.MINOR.PATCH), "
                f"收到: {value!r}"
            )
        return value

    # ── 构建后自动计算 content_hash ───────────────────────────────────

    @model_validator(mode="after")
    def _compute_content_hash(self) -> "Recipe":
        """构建后自动计算 content_hash, 除非显式传入 (用于反序列化比对)。

        从 dict 反序列化时 content_hash 已在 JSON 中, 不需要重新计算;
        新建 Recipe 时 content_hash 为空, 自动填充并校验 name/version 非空。

        空 name/version 仅允许在 merge 流程中作为临时 override 对象存在,
        此时 content_hash 已由外部设置或无需校验。
        """
        if self.content_hash:
            return self  # 已从 dict 反序列化, 保留原值
        if not self.name or not self.version:
            raise ValueError(
                "Recipe name 和 version 不能为空 (merge 时请使用 "
                "Recipe.override_params() 构建临时 override 对象)"
            )
        computed = json_sha256(self._canonical_dict())
        object.__setattr__(self, "content_hash", computed)
        return self

    # ── 实例方法 ──────────────────────────────────────────────────────

    def _canonical_dict(self) -> dict[str, Any]:
        """返回用于哈希计算的规范化字典 (不含 content_hash)。"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "style_params": self.style_params,
            "stage_overrides": self.stage_overrides,
        }

    def verify_hash(self) -> bool:
        """校验 content_hash 是否与当前内容一致。

        用于反序列化后验证完整性 (检测 JSON 文件被篡改或手动编辑)。
        """
        expected = json_sha256(self._canonical_dict())
        return self.content_hash == expected

    def merge(self, **overrides: Any) -> "Recipe":
        """以当前 Recipe 为 base, 用 overrides 覆盖生成新 Recipe。

        合并规则:
          - name, version: 取自 overrides (非空才覆盖 base)
          - description: 取自 overrides (非空才覆盖 base)
          - style_params: 浅合并 (overrides 中的键覆盖 base)
          - stage_overrides: 逐 Stage 浅合并 (overrides 中的键覆盖 base)

        示例:
            >>> base.merge(
            ...     style_params={"transition": "wipe"},
            ...     stage_overrides={"story_plans": {"max_duration": 30}},
            ... )
        """
        merged_name = overrides.get("name") or self.name
        merged_version = overrides.get("version") or self.version
        merged_description = overrides.get("description") or self.description
        merged_style = {**self.style_params, **overrides.get("style_params", {})}
        merged_stages: dict[str, dict[str, Any]] = {}
        all_stages = set(self.stage_overrides.keys()) | set(
            overrides.get("stage_overrides", {}).keys()
        )
        for stage in sorted(all_stages):
            base_stage = self.stage_overrides.get(stage, {})
            override_stage = overrides.get("stage_overrides", {}).get(stage, {})
            merged_stages[stage] = {**base_stage, **override_stage}

        return Recipe(
            name=merged_name,
            version=merged_version,
            description=merged_description,
            style_params=merged_style,
            stage_overrides=merged_stages,
        )

    def to_file(self) -> dict[str, Any]:
        """导出为可写入 JSON 文件的完整字典 (含 content_hash)。"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "style_params": self.style_params,
            "stage_overrides": self.stage_overrides,
            "content_hash": self.content_hash,
        }