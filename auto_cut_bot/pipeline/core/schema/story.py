"""故事相关 Schema — Pydantic v2 兼容桥。

原 story_schemas.py STORY_CATALOG/PORTFOLIO/TREATMENT/SCRIPT 等 schema。
"""

from __future__ import annotations
from typing import Any


# ID helpers (minimal versions to avoid circular import)
_S = {"type": "string"}
_NE = {"type": "string", "minLength": 1}
_N = {"type": "number"}
_B = {"type": "boolean"}
_I = {"type": "integer"}
_EVENT_ID = {"type": "string", "pattern": r"^event-[0-9a-f]{12}$"}
_CHAR_ID = {"type": "string", "pattern": r"^char-[a-z0-9-]{2,40}$"}
_THREAD_ID = {"type": "string", "pattern": r"^thread-[a-z0-9-]{2,40}$"}
_FACT_ID = {"type": "string", "pattern": r"^fact-[a-z0-9-]{2,40}$"}


def _arr(items, **kw):
    r = {"type": "array", "items": items}
    r.update(kw)
    return r


def _obj(props, required=None, additional=False):
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": additional,
    }



def story_dict_schemas() -> dict[str, Any]:
    """返回所有故事相关旧式 dict schema。"""

    STORY_CATALOG_SCHEMA = _obj({
        "schema_version": {"type": "string", "const": "1.0"},
        "story_id": _NE,
        "title": _NE,
        "source_episodes": _arr(_I),
        "source_windows": _arr(_S),
        "evidence_event_ids": _arr(_EVENT_ID, minItems=1),
        "thread_ids": _arr(_THREAD_ID, minItems=1),
        "primary_character_ids": _arr(_CHAR_ID),
        "hook_candidate_ids": _arr(_S),
        "highlight_candidate_ids": _arr(_S),
        "summary": _NE,
    })
    
    STORY_SCORE_SCHEMA = _obj({
        "story_completeness": {"type": "integer", "minimum": 1, "maximum": 10},
        "independent_clarity": {"type": "integer", "minimum": 1, "maximum": 10},
        "highlight_relevance": {"type": "integer", "minimum": 1, "maximum": 10},
        "source_sufficiency": {"type": "integer", "minimum": 1, "maximum": 10},
        "causal_clarity": {"type": "integer", "minimum": 1, "maximum": 10},
        "entertainment_value": {"type": "integer", "minimum": 1, "maximum": 10},
        "thread_saturation": {"type": "integer", "minimum": 1, "maximum": 10},
        "scene_visibility": {"type": "integer", "minimum": 1, "maximum": 10},
    })
    
    return {
        "STORY_CATALOG_SCHEMA": STORY_CATALOG_SCHEMA,
        "STORY_SCORE_SCHEMA": STORY_SCORE_SCHEMA,
    }
