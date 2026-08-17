"""validate 规则表化示范 — validate_story_artifacts.py 检查组的声明式移植。

示范对象: validate_story_artifacts.py 的 ``validate()`` (约 1346 行的顺序断言链)。
本模块把其中 **5 组纯数据检查** 移植为声明式规则, 并在
tests/unit/test_validate_rule_equivalence.py 中:

  1. 用快照固化当前实现的行为 (输入 → 违规输出);
  2. 证明移植后的规则对同一输入产生**逐字相同**的错误消息。

移植的 5 组:
  va_unique_ids             ← unique_ids()               (ID 非空 + 唯一)
  va_check_refs             ← check_refs()               (引用完整性)
  va_thread_beat_accounting ← thread_beat_accounting_findings()
  va_abstract_beat_content  ← is_abstract_only() 的 beat 循环
  va_script_role_structure  ← validate() 内联的 beat role 结构检查

validate() 保留不动, 它是行为基准; 这些规则的消息格式与 validate() 实现
逐字一致 (含 ``{where}``/``{name}`` 前缀), 保证同一输入下两者输出可 diff。

payload 约定: artifacts 中使用 key ``validate_target``, 值为 dict,
各规则所需字段见各 check 函数文档。
"""

from __future__ import annotations

from typing import Any, Mapping

from autocut_core.contracts.rules.builtin import is_abstract_only
from autocut_core.contracts.rules.engine import Finding, Rule

__all__ = ["LEGACY_VALIDATION_RULES", "TARGET_KEY"]

#: 示范规则统一消费的产物 key。
TARGET_KEY = "validate_target"


# ═══════════════════════════════════════════════════════════════════════════
# 组 1: unique_ids — records[].field 非空且唯一
# payload: {"records": [...], "field": str, "where": str}
# ═══════════════════════════════════════════════════════════════════════════


def _check_unique_ids(payloads: Mapping[str, Any]) -> list[Finding]:
    target = payloads[TARGET_KEY]
    records = target.get("records") or []
    field_name = target.get("field", "id")
    where = target.get("where", "records")
    findings: list[Finding] = []
    seen: set[str] = set()
    for index, item in enumerate(records):
        value = item.get(field_name) if isinstance(item, dict) else None
        if not isinstance(value, str) or not value:
            findings.append(
                Finding(
                    code="id_must_be_non_empty",
                    message=f"{where}[{index}].{field_name} must be non-empty",
                    location=f"{where}[{index}].{field_name}",
                )
            )
        elif value in seen:
            findings.append(
                Finding(
                    code="duplicate_id",
                    message=f"{where}: duplicate {field_name} {value}",
                    location=f"{where}[{index}].{field_name}",
                )
            )
        else:
            seen.add(value)
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# 组 2: check_refs — ID 列表引用完整性
# payload: {"values": Any, "known": [...], "where": str}
# ═══════════════════════════════════════════════════════════════════════════


def _check_refs(payloads: Mapping[str, Any]) -> list[Finding]:
    target = payloads[TARGET_KEY]
    values = target.get("values")
    known = {item for item in target.get("known") or [] if isinstance(item, str)}
    where = target.get("where", "refs")
    if not isinstance(values, list):
        return [
            Finding(
                code="refs_must_be_array",
                message=f"{where} must be an array",
                location=where,
            )
        ]
    unknown = sorted({item for item in values if isinstance(item, str)} - known)
    if unknown:
        return [
            Finding(
                code="unknown_reference",
                message=f"{where} contains unknown IDs: {unknown}",
                location=where,
            )
        ]
    return []


# ═══════════════════════════════════════════════════════════════════════════
# 组 3: thread_beat_accounting — Catalog 子弧归账 + F4 扩容
# payload: {"script": {...}, "source_thread_beat_ids": [...]}
# ═══════════════════════════════════════════════════════════════════════════


def _check_thread_beat_accounting(payloads: Mapping[str, Any]) -> list[Finding]:
    target = payloads[TARGET_KEY]
    script = target.get("script") or {}
    source_thread_beat_ids = {
        item
        for item in target.get("source_thread_beat_ids") or []
        if isinstance(item, str)
    }
    selected = {
        item
        for item in script.get("selected_thread_beat_ids", [])
        if isinstance(item, str)
    }
    omitted = {
        item.get("thread_beat_id")
        for item in script.get("omitted_thread_beats", [])
        if isinstance(item, dict)
        and isinstance(item.get("thread_beat_id"), str)
    }
    expanded = {
        beat_id
        for expansion in script.get("auto_scope_expansion", [])
        if isinstance(expansion, dict)
        for beat_id in expansion.get("added_thread_beat_ids", [])
        if isinstance(beat_id, str)
    }
    findings: list[Finding] = []
    expected = source_thread_beat_ids | expanded
    if selected | omitted != expected:
        findings.append(
            Finding(
                code="thread_beat_accounting_gap",
                message=(
                    "selected/omitted Thread Beats do not account for the "
                    "Catalog subarc and applied scope expansion"
                ),
                location="selected_thread_beat_ids/omitted_thread_beats",
            )
        )
    if not expanded <= selected:
        findings.append(
            Finding(
                code="scope_expansion_not_selected",
                message="applied scope expansion Thread Beats must remain selected",
                location="auto_scope_expansion",
            )
        )
    if selected & omitted:
        findings.append(
            Finding(
                code="selected_and_omitted_overlap",
                message="a Thread Beat cannot be both selected and omitted",
                location="selected_thread_beat_ids/omitted_thread_beats",
            )
        )
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# 组 4: abstract beat content — beats[].concrete_story_content 必须具体
# payload: {"script": {...}, "name": str (旧 path.name 前缀)}
# ═══════════════════════════════════════════════════════════════════════════


def _check_abstract_beat_content(payloads: Mapping[str, Any]) -> list[Finding]:
    target = payloads[TARGET_KEY]
    script = target.get("script") or {}
    name = target.get("name", "story-script")
    findings: list[Finding] = []
    for beat_index, beat in enumerate(script.get("beats") or []):
        if not isinstance(beat, dict):
            continue
        if is_abstract_only(beat.get("concrete_story_content")):
            findings.append(
                Finding(
                    code="abstract_story_content",
                    message=(
                        f"{name}.beats[{beat_index}].concrete_story_content "
                        "is abstract or too vague"
                    ),
                    location=f"{name}.beats[{beat_index}].concrete_story_content",
                )
            )
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# 组 5: script role structure — beat role 结构 (含 teaser mode 分支)
# payload: {"script": {...}, "name": str (旧 path.name 前缀)}
# ═══════════════════════════════════════════════════════════════════════════


def _check_script_role_structure(payloads: Mapping[str, Any]) -> list[Finding]:
    target = payloads[TARGET_KEY]
    script = target.get("script") or {}
    name = target.get("name", "story-script")
    roles = [beat.get("role") for beat in script.get("beats", [])]
    findings: list[Finding] = []
    script_teaser_mode = script.get("teaser_contract", {}).get(
        "mode", "single_highlight"
    )
    if script_teaser_mode == "single_highlight":
        for required_role in ("teaser_intent", "escalation", "payoff"):
            if required_role not in roles:
                findings.append(
                    Finding(
                        code="missing_required_beat",
                        message=f"{name}: missing required beat {required_role}",
                        location=f"{name}.beats",
                    )
                )
        if roles and roles[0] != "teaser_intent":
            findings.append(
                Finding(
                    code="first_beat_not_teaser_intent",
                    message=f"{name}: first beat must be teaser_intent",
                    location=f"{name}.beats[0]",
                )
            )
    else:
        for required_role in ("escalation", "payoff"):
            if required_role not in roles:
                findings.append(
                    Finding(
                        code="missing_required_beat",
                        message=f"{name}: missing required beat {required_role}",
                        location=f"{name}.beats",
                    )
                )
        if "teaser_intent" in roles:
            findings.append(
                Finding(
                    code="teaser_intent_incompatible",
                    message=(
                        f"{name}: teaser_contract.mode=none is incompatible "
                        "with a teaser_intent beat"
                    ),
                    location=f"{name}.beats",
                )
            )
    if not ({"orientation", "setup"} & set(roles)):
        findings.append(
            Finding(
                code="missing_orientation_or_setup",
                message=f"{name}: requires orientation or setup",
                location=f"{name}.beats",
            )
        )
    hook = script.get("ending_hook_intent", {})
    if hook.get("may_be_empty") is False and (
        not roles or roles[-1] != "end_hook"
    ):
        findings.append(
            Finding(
                code="last_beat_not_end_hook",
                message=f"{name}: last beat must be end_hook",
                location=f"{name}.beats",
            )
        )
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# 示范规则表
# ═══════════════════════════════════════════════════════════════════════════

LEGACY_VALIDATION_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="va_unique_ids",
        check_fn=_check_unique_ids,
        description="validate() 表化示范: 记录 ID 非空且唯一",
        group="legacy_validation",
        requires=(TARGET_KEY,),
        source="validate_story_artifacts.validate() unique_ids()",
    ),
    Rule(
        rule_id="va_check_refs",
        check_fn=_check_refs,
        description="validate() 表化示范: ID 列表引用完整性",
        group="legacy_validation",
        requires=(TARGET_KEY,),
        source="validate_story_artifacts.validate() check_refs()",
    ),
    Rule(
        rule_id="va_thread_beat_accounting",
        check_fn=_check_thread_beat_accounting,
        description="validate() 表化示范: Catalog 子弧 Thread Beat 归账",
        group="legacy_validation",
        requires=(TARGET_KEY,),
        source="validate_story_artifacts.thread_beat_accounting_findings()",
    ),
    Rule(
        rule_id="va_abstract_beat_content",
        check_fn=_check_abstract_beat_content,
        description="validate() 表化示范: Beat 内容不得只有抽象描述",
        group="legacy_validation",
        requires=(TARGET_KEY,),
        source="validate_story_artifacts.is_abstract_only() beat 循环",
    ),
    Rule(
        rule_id="va_script_role_structure",
        check_fn=_check_script_role_structure,
        description="validate() 表化示范: beat role 结构检查 (teaser mode 分支)",
        group="legacy_validation",
        requires=(TARGET_KEY,),
        source="validate_story_artifacts.validate() 内联 role 检查",
    ),
)
