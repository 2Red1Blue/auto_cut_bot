"""NarrativeDutyPolicy/v1: optional narrative-duty annotations for Stage 2.

Absorbed from NarratoAI's short-drama narration planning method (segment duties
and cross-segment continuity), stripped of everything we must NOT absorb:
subtitle-timing based clip selection, forced narration/OST alternation, and
narration-text-as-plot-fact. Duties are annotations only — they never replace
required obligations/facts, and material feasibility stays program-computed.

Version naming: this is a shadow experiment wire; it never mutates
stage2-proposal-compact-v2 (see R1B registration rules).
"""

from __future__ import annotations

import json
from typing import Any

NARRATIVE_POLICY_SCHEMA = "NarrativeDutyPolicy/v1"
NARRATIVE_POLICY_VERSION = "narrative-duty-policy-v1"
NARRATIVE_WIRE_SCHEMA_VERSION = "stage2-story-design-compact-narrative-v1"

# Closed duty enum from doc 01-r1-narrative-strategy.md
NARRATIVE_DUTY_KINDS = (
    "hook",
    "setup",
    "escalation",
    "relationship_turn",
    "reveal",
    "payoff",
    "bridge",
    "cliffhanger",
)

# Duties whose narrative meaning pairs two references (setup before payoff).
_PAIRED_KINDS = frozenset({"payoff", "reveal", "relationship_turn", "cliffhanger"})

_CANDIDATE_PROMPT_SECTION = """

叙事职责标注（可选扩展）：
在保持上述全部规则的前提下，你可以为每个 proposal 额外给出 narrative_duties 数组，
标注该 proposal 在叙事结构中承担的职责。可用职责只有：
hook（开场钩子）、setup（铺垫）、escalation（升级）、relationship_turn（关系转变）、
reveal（揭示）、payoff（回收）、bridge（过渡承接）、cliffhanger（悬念）。
每条职责包含 duty_kind、target_ref（必须填本 proposal 的位置引用 proposal-<序号>）、
reason（一句解释）；当 duty_kind 是 payoff、reveal、relationship_turn 或 cliffhanger 时，
必须给出 setup_ref 与 payoff_ref，指向上下文中已存在的事件（e*）或事实（f*）别名，
且 setup_ref 与 payoff_ref 必须不同。
职责标注是可选的：某个 proposal 没有职责不构成失败；
职责不替代、不减少任何必选 obligation/fact；素材可行性与物理安全检查仍由程序裁定。
不要根据字幕时间选择片段；不要引入旁白或字幕文本作为剧情事实。
"""


def candidate_prompt_section() -> str:
    """The only prompt delta between baseline and candidate arms."""
    return _CANDIDATE_PROMPT_SECTION


def extend_response_schema(base_schema: dict[str, Any]) -> dict[str, Any]:
    """Add the closed narrative_duties array to each proposal in the base schema.

    The base schema must be the frozen compact response schema; the extension
    keeps every other constraint (additionalProperties stays False)."""
    extended = json.loads(json.dumps(base_schema))  # deep copy without aliasing
    proposals = extended.get("properties", {}).get("proposals")
    if not isinstance(proposals, dict):
        raise ValueError("base schema lacks proposals property")
    items = proposals.get("items")
    if not isinstance(items, dict):
        raise ValueError("base schema proposals lacks items")
    duty_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["duty_kind", "target_ref", "reason"],
        "properties": {
            "duty_kind": {"type": "string", "enum": list(NARRATIVE_DUTY_KINDS)},
            "target_ref": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
            "setup_ref": {"type": "string", "minLength": 1},
            "payoff_ref": {"type": "string", "minLength": 1},
        },
    }
    props = items.setdefault("properties", {})
    props["narrative_duties"] = {
        "type": "array",
        "items": duty_schema,
    }
    required = items.get("required")
    if isinstance(required, list):
        extended["properties"]["proposals"]["items"]["required"] = list(required)
    return extended


def is_paired_kind(duty_kind: str) -> bool:
    return duty_kind in _PAIRED_KINDS
