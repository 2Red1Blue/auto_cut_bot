"""共享 ID 模式与 JSON Schema 构建器 — schema/ 层的核心工具模块。

所有 ID 格式都通过严格正则模式在 API schema 中强制校验,
在畸形/注入值落盘之前拒绝它们。

在流水线中的位置: 被各 Stage 的产物 schema 引用 (事件卡、
剧集实体、故事线等 ID), 与 io.stable_id 生成的确定性 ID 配套。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# JSON Schema 构建器
# ---------------------------------------------------------------------------


def obj(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    additional: bool = False,
) -> dict[str, Any]:
    """构建 object 类型 schema — 默认全部字段必填且禁止额外字段。"""
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": additional,
    }


def arr(
    items: dict[str, Any],
    *,
    min_items: int | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """构建 array 类型 schema, 可选限制元素个数上下界。"""
    value: dict[str, Any] = {"type": "array", "items": items}
    if min_items is not None:
        value["minItems"] = min_items
    if max_items is not None:
        value["maxItems"] = max_items
    return value


# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------

STR = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1}
NUM = {"type": "number"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
STRINGS = arr(STR)
NONEMPTY_STRINGS = arr(NONEMPTY, min_items=1)

# ---------------------------------------------------------------------------
# 稳定 ID 模式
#
# 确定性 Python 代码产出的每个 ID 都携带固定长度十六进制后缀。
# schema 层模式可在落盘前拒绝 LLM 虚构的 ID、JSON 分隔符碎片
# 以及空/无界后缀。
# ---------------------------------------------------------------------------

# compile_event_cards.stable_id("event", ...) → event-<12位十六进制>
EVENT_ID = {"type": "string", "pattern": r"^event-[0-9a-f]{12}$"}
EVENT_IDS = arr(EVENT_ID)
NONEMPTY_EVENT_IDS = arr(EVENT_ID, min_items=1)

# 实体 ID: kebab-case 前缀 + 2-40 位小写十六进制/连字符后缀。
# 拒绝下划线、点号、空后缀和无界长度。
CHAR_ID = {"type": "string", "pattern": r"^char-[a-z0-9-]{2,40}$"}
REL_ID = {"type": "string", "pattern": r"^rel-[a-z0-9-]{2,40}$"}
THREAD_ID = {"type": "string", "pattern": r"^thread-[a-z0-9-]{2,40}$"}
FACT_ID = {"type": "string", "pattern": r"^fact-[a-z0-9-]{2,40}$"}
CHAR_IDS = arr(CHAR_ID)
NONEMPTY_CHAR_IDS = arr(CHAR_ID, min_items=1)
NONEMPTY_THREAD_IDS = arr(THREAD_ID, min_items=1)

# 带外问题 ID (out-of-band question)
OQ_ID = {"type": "string", "pattern": r"^q-[a-z0-9-]{2,40}$"}

# 别名: 最小长度 2 防止单字符误碰撞
ALIAS = {"type": "string", "minLength": 2}
ALIASES = arr(ALIAS)

# 实体/故事线/语言枚举
ENTITY_TYPE = {
    "type": "string",
    "enum": ["individual", "group", "creature", "unknown"],
}
LANGUAGE = {"type": "string", "enum": ["zh", "en"]}
THREAD_KIND = {"type": "string", "enum": ["arc", "coda"]}

# ---------------------------------------------------------------------------
# 常用复合 schema
# ---------------------------------------------------------------------------

# 身份证据: 集数 + 至少 10 字的台词引用 (支撑实体身份判定)
IDENTITY_EVIDENCE = obj(
    {
        "episode": {"type": "integer", "minimum": 1},
        "quote": {"type": "string", "minLength": 10},
    }
)
