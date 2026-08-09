#!/usr/bin/env python3
"""Deterministic checks derived from the editorial and technical knowledge bases."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parent.parent
    / "_references"
    / "editorial-knowledge-base.json"
)
DEFAULT_GOLDEN_CASE_PATH = (
    Path(__file__).resolve().parent.parent
    / "_references"
    / "editorial-golden-case-arya.json"
)


def validate_golden_case(path: Path | None = None) -> list[str]:
    """Validate the project-level positive/negative editorial fixture.

    This is intentionally structural. It never treats the fixture as source
    evidence and therefore cannot make a model hallucinate project facts.
    """
    target = (path or DEFAULT_GOLDEN_CASE_PATH).expanduser().resolve()
    errors: list[str] = []
    try:
        with target.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot load golden case {target}: {exc}"]
    if not isinstance(value, dict):
        return ["golden case must be an object"]
    for key in (
        "case_id",
        "transferable_contract",
        "approved_story_sequence",
        "approved_cut",
        "negative_examples",
        "regression_assertions",
    ):
        if key not in value:
            errors.append(f"golden case missing {key}")
    negatives = value.get("negative_examples", [])
    if not isinstance(negatives, list) or len(negatives) < 3:
        errors.append("golden case must contain at least 3 negative examples")
    else:
        for index, item in enumerate(negatives):
            if not isinstance(item, dict):
                errors.append(f"negative_examples[{index}] must be an object")
                continue
            if item.get("expected_status") != "blocked":
                errors.append(
                    f"negative_examples[{index}] must have expected_status=blocked"
                )
            if not item.get("violations"):
                errors.append(f"negative_examples[{index}] has no violations")
    contract = value.get("transferable_contract", {})
    if contract.get("primary_thread") == contract.get("secondary_thread"):
        errors.append("golden case primary and secondary threads must differ")
    if not contract.get("integrated_support_thread"):
        errors.append("golden case must explicitly mark integrated_support_thread")
    return errors


def load_policy(path: Path | None = None) -> dict[str, Any]:
    policy_path = (path or DEFAULT_POLICY_PATH).expanduser().resolve()
    with policy_path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or value.get("schema_version") not in {"1.0", "1.1", "1.2"}:
        raise ValueError(f"invalid editorial knowledge base: {policy_path}")
    return value


def load_knowledge_section(section: str, *, path: Path | None = None) -> Any:
    """Load a top-level section from the editorial knowledge base.

    Returns the section value if present, or ``None`` if the section does not exist.
    Modules can use this to read their data-driven configuration with a hardcoded
    fallback for backward compatibility.
    """
    policy = load_policy(path=path)
    return policy.get(section)


def planning_context(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the small, stage-relevant policy subset sent to model contexts."""
    value = policy or load_policy()
    # Golden cases are conditional resources.  Do not put Arya's case ID or
    # lesson in the generic context: the selected Bible genre adapter is the
    # only legal way to load a positive/negative sample.
    golden_context = {
        "mode": "conditional_by_genre_route",
        "selection_field": "golden_case_ids",
        "project_specific_details_excluded": True,
        "negative_examples_are_regression_only": True,
    }
    return {
        "priority_order": value["principles"]["priority_order"],
        "editorial_discovery_order": value["principles"].get(
            "editorial_discovery_order", []
        ),
        "highlight_is_candidate_before_story_validation": value["principles"].get(
            "highlight_is_candidate_before_story_validation", True
        ),
        "opening_is_joint_selection_not_clip_first": value["principles"].get(
            "opening_is_joint_selection_not_clip_first", True
        ),
        "mainline_policy": value["mainline_policy"],
        "arc_policy": value["arc_policy"],
        "source_order_policy": value["source_order_policy"],
        "hook_policy": value["hook_policy"],
        "transition_policy": value["transition_policy"],
        "story_contract": value.get("story_contract", {}),
        "edit_mode_selection": value.get("story_contract", {}).get(
            "edit_mode_selection", {}
        ),
        "opening_strategy": value.get("story_contract", {}).get(
            "opening_strategy", {}
        ),
        "opening_selection": value.get("story_contract", {}).get(
            "opening_selection", {}
        ),
        "continuity_contract": value.get("story_contract", {}).get(
            "continuity_contract", {}
        ),
        "ending_policy": value.get("story_contract", {}).get(
            "ending_policy", {}
        ),
        "duration_extension_policy": value.get("story_contract", {}).get(
            "duration_extension_policy", {}
        ),
        "hallucination_policy": value.get("hallucination_policy", {}),
        "genre_routing": value.get("genre_routing", {}),
        "golden_sample": golden_context,
        "genre_overrides": {
            "selection_rule": "只读取 Bible genre_profile 路由出的类型适配器；禁止把任一类型样例作为全局默认。",
            "unknown_profile": "human_review_required",
        },
        "duration_policy": {
            "minimum_seconds": 0,
            "preferred_target_seconds": 0,
            "maximum_seconds": 1200,
            "same_line_extension_only": True,
            "no_functionless_duration_fill": True,
            "extension_contract_source": "story_contract.duration_extension_policy",
        },
    }


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _beat_line(beat: dict[str, Any]) -> tuple[str, ...]:
    retrieval = beat.get("retrieval_requirements", {})
    if isinstance(retrieval, dict):
        # Story Thread is the editorial unit. Relationship IDs remain a
        # compatibility fallback for older scripts, but must not override an
        # explicit Thread assignment (this was the source of several false
        # "mainline" classifications in mixed relationship stories).
        values = retrieval.get("story_thread_ids") or retrieval.get("relationship_ids", [])
    else:
        values = []
    values = _unique(values)
    return tuple(sorted(values)) or ("__mainline__",)


def _text(record: dict[str, Any]) -> str:
    keys = ("description", "reason", "anchor", "lead_in", "payoff_or_open_question", "dialogue_excerpt")
    return " ".join(str(record.get(key, "")) for key in keys).lower()


def _finding(code: str, status: str, description: str, route: str) -> dict[str, str]:
    return {"code": code, "status": status, "description": description, "repair_route": route}


def opening_strategy_diagnostics(script: dict[str, Any]) -> dict[str, Any]:
    """Validate the minimal causal-opening contract.

    The existing teaser/span/render pipeline remains unchanged. This check only
    decides whether a selected teaser is allowed to be replayed in the body,
    and whether the body contains an evidence-backed return to the teaser's
    earlier cause before continuing forward.
    """
    teaser_contract = script.get("teaser_contract")
    beats = [item for item in script.get("beats", []) if isinstance(item, dict)]
    if not isinstance(teaser_contract, dict) or not beats:
        return {"strategy": "not_declared", "findings": [], "status": "pass"}

    # Missing metadata is an artifact. Preserve its reprise contract;
    # newly generated Story Scripts are required by the prompt to write the
    # explicit causal_explanatory_no_reprise or delayed_reprise value.
    strategy = teaser_contract.get("opening_strategy", "future_preview_reprise")
    findings: list[dict[str, str]] = []
    allowed_strategies = set(
        (load_knowledge_section("editorial_knowledge") or {}).get(
            "allowed_strategies", []
        )
    )
    if not allowed_strategies:
        allowed_strategies = {
            "causal_explanatory_opening",
            "causal_explanatory_no_reprise",
            "causal_explanatory_delayed_reprise",
            "original_chronological_opening",
            "future_preview_reprise",  # backward compatibility
        }
    if strategy not in allowed_strategies:
        findings.append(_finding(
            "opening_strategy_unknown",
            "blocked",
            f"opening_strategy={strategy!r} 不在允许的开场策略中。",
            "story_script",
        ))
        return {"strategy": strategy, "findings": findings, "status": "blocked"}

    if strategy == "future_preview_reprise":
        return {"strategy": strategy, "findings": [], "status": "pass"}

    edit_mode = script.get("edit_mode")
    if edit_mode == "original_chronological" and strategy != "original_chronological_opening":
        findings.append(_finding(
            "edit_mode_opening_strategy_mismatch",
            "blocked",
            "original_chronological 模式必须使用 original_chronological_opening。",
            "story_script",
        ))
    if edit_mode == "montage" and strategy == "original_chronological_opening":
        findings.append(_finding(
            "edit_mode_opening_strategy_mismatch",
            "blocked",
            "montage 模式不能使用 original_chronological_opening。",
            "story_script",
        ))

    first = beats[0]
    if first.get("role") != "teaser_intent":
        findings.append(_finding(
            "opening_teaser_missing",
            "blocked",
            "开场 Beat 必须是 teaser_intent；原剧情顺剪时它代表自然主线开场，不是未来预告。",
            "story_script",
        ))

    if strategy == "original_chronological_opening":
        if first.get("temporal_position") != "mainline":
            findings.append(_finding(
                "original_opening_not_mainline",
                "blocked",
                "原剧情顺剪开场必须位于 mainline，不能使用 future_preview。",
                "story_script",
            ))
        status = "blocked" if any(item["status"] == "blocked" for item in findings) else "pass"
        return {
            "strategy": strategy,
            "edit_mode": edit_mode or "original_chronological",
            "explanation_beat_ids": [],
            "repeated_in_body": False,
            "findings": findings,
            "status": status,
        }

    declared_explanations = teaser_contract.get("explanation_beat_ids", [])
    explanation_ids = [
        item for item in declared_explanations
        if isinstance(item, str) and item
    ]
    if not explanation_ids:
        explanation_ids = [
            beat.get("id")
            for beat in beats[1:]
            if beat.get("temporal_position") == "earlier_context"
            or (
                isinstance(beat.get("causal_transition"), dict)
                and beat["causal_transition"].get("type") == "causal"
            )
        ]
    explanation_positions = [
        index for index, beat in enumerate(beats)
        if beat.get("id") in explanation_ids
    ]
    if not explanation_positions:
        findings.append(_finding(
            "opening_causal_explanation_missing",
            "blocked",
            "开场高光后没有可验证的 earlier_context/causal Beat 回到前因解释。",
            "story_script",
        ))
    else:
        if any(position == 0 for position in explanation_positions):
            findings.append(_finding(
                "opening_explanation_not_after_teaser",
                "blocked",
                "开场解释 Beat 必须位于冷开场之后，不能把 teaser 自己标成解释。",
                "story_script",
            ))
        if any(
            not (
                beats[position].get("temporal_position") == "earlier_context"
                or (
                    isinstance(beats[position].get("causal_transition"), dict)
                    and beats[position]["causal_transition"].get("type")
                    == "causal"
                )
            )
            for position in explanation_positions
        ):
            findings.append(_finding(
                "opening_explanation_not_causal",
                "blocked",
                "声明的开场解释 Beat 没有 earlier_context 或 causal 承接证据。",
                "story_script",
            ))
        first_beat_id = first.get("id")
        if not any(
            (
                beats[position].get("explains_opening_highlight") is True
                or (
                    isinstance(beats[position].get("causal_transition"), dict)
                    and beats[position]["causal_transition"].get("from_beat_id")
                    == first_beat_id
                )
            )
            for position in explanation_positions
        ):
            findings.append(_finding(
                "opening_causal_relation_weak",
                "blocked",
                "正文解释 Beat 没有明确指向开场高光的因果关系；必须通过 from_beat_id 或 explains_opening_highlight 绑定。",
                "story_script",
            ))
        last_explanation = max(explanation_positions)
        if not any(
            index > last_explanation
            and beats[index].get("role")
            in {"escalation", "turn_or_reveal", "payoff", "end_hook"}
            for index in range(len(beats))
        ):
            findings.append(_finding(
                "opening_explanation_has_no_new_progression",
                "blocked",
                "前因解释后没有新的升级、揭示、兑现或结尾悬念。",
                "story_plan",
            ))

    teaser_events = {
        event_id
        for event_id in first.get("event_ids", [])
        if isinstance(event_id, str)
    }
    repeated_positions = [
        index
        for index, beat in enumerate(beats[1:], start=1)
        if teaser_events & {
            event_id
            for event_id in beat.get("event_ids", [])
            if isinstance(event_id, str)
        }
    ]
    repeated_in_body = bool(repeated_positions)
    repetition_function = teaser_contract.get("repetition_function")
    allowed_repetition = set(
        (load_knowledge_section("editorial_knowledge") or {}).get(
            "allowed_repetition", []
        )
    )
    if not allowed_repetition:
        allowed_repetition = {
            "new_causal_context",
            "relationship_reinterpretation",
            "consequence_recontextualization",
            "suspense_recovery",
        }
    no_reprise_strategy = strategy in {
        "causal_explanatory_opening",
        "causal_explanatory_no_reprise",
    }
    delayed_reprise_strategy = strategy == "causal_explanatory_delayed_reprise"
    if repeated_in_body and no_reprise_strategy:
        findings.append(_finding(
            "teaser_reprise_not_allowed",
            "blocked",
            "当前开场策略要求正文不重放开场高光；如确需重现，改用 delayed_reprise 并声明新增功能。",
            "story_script",
        ))
    if repeated_in_body and delayed_reprise_strategy:
        first_repeat_position = min(repeated_positions)
        last_explanation = max(explanation_positions) if explanation_positions else 0
        minimum_progression_beats = int(
            teaser_contract.get("reprise_delay_minimum_progression_beats", 1)
        )
        progression_before_reprise = max(
            0, first_repeat_position - last_explanation - 1
        )
        if progression_before_reprise < minimum_progression_beats:
            findings.append(_finding(
                "teaser_reprise_too_early",
                "blocked",
                f"正文重现开场高光前只有 {progression_before_reprise} 个新推进 Beat，至少需要 {minimum_progression_beats} 个；不能紧接重复。",
                "story_plan",
            ))
        if repetition_function not in allowed_repetition:
            findings.append(_finding(
                "mechanical_teaser_repetition",
                "blocked",
                "延后重现开场高光时，必须声明新的因果、关系、后果或悬念功能。",
                "story_script",
            ))

    status = "blocked" if any(item["status"] == "blocked" for item in findings) else "pass"
    return {
        "strategy": strategy,
        "explanation_beat_ids": explanation_ids,
        "repeated_in_body": repeated_in_body,
        "first_repeated_beat_id": (
            beats[min(repeated_positions)].get("id")
            if repeated_positions
            else ""
        ),
        "findings": findings,
        "status": status,
    }


def continuity_contract_diagnostics(
    script: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check cross-beat continuity without inventing plot evidence.

    The contract is optional for persisted scripts.  Preflight writes it
    for newly generated scripts, so new work gets the stricter checks while old
    approved work remains readable and idempotent.
    """
    value = policy or load_policy()
    contract = script.get("editorial_contract", {})
    continuity = contract.get("continuity_contract") if isinstance(contract, dict) else None
    beats = [item for item in script.get("beats", []) if isinstance(item, dict)]
    if not isinstance(continuity, dict) or not beats:
        return {"status": "pass", "enforced": False, "findings": []}

    configured = value.get("story_contract", {}).get("continuity_contract", {})
    allowed_types = set(
        configured.get("allowed_bridge_types", [])
        or continuity.get("allowed_bridge_types", [])
        or value.get("transition_policy", {}).get("allowed_bridge_types", [])
    )
    primary = script.get("primary_story_thread_id") or contract.get(
        "primary_story_thread_id"
    )
    integrated_ids = set(
        item
        for item in (
            script.get("integrated_support_thread_ids", [])
            or contract.get("integrated_support_thread_ids", [])
        )
        if isinstance(item, str) and item
    )
    findings: list[dict[str, str]] = []

    def line_for(beat: dict[str, Any]) -> set[str]:
        return set(_beat_line(beat))

    def is_integrated(beat: dict[str, Any]) -> bool:
        return beat.get("thread_role") == "integrated_support" or bool(
            line_for(beat) & integrated_ids
        )

    def transition_for(beat: dict[str, Any]) -> dict[str, Any] | None:
        transition = beat.get("causal_transition")
        return transition if isinstance(transition, dict) else None

    # A future preview is legal only as the single teaser.  A later future
    # preview is an unapproved arc injection, even if its thread label matches.
    for index, beat in enumerate(beats):
        if index > 0 and beat.get("temporal_position") == "future_preview":
            findings.append(_finding(
                "future_body_preview_injection",
                "blocked",
                "除首个 teaser 外，正文不得再次使用 future_preview；这会把未来完整弧注入当前成片。",
                "story_plan",
            ))

    # A lookback must say what it explains and must have an explicit bridge
    # type.  This prevents montage from becoming arbitrary time jumping.
    lookback_positions: list[int] = []
    for index, beat in enumerate(beats[1:], start=1):
        if beat.get("temporal_position") != "earlier_context":
            continue
        lookback_positions.append(index)
        transition = transition_for(beat)
        if transition is None or transition.get("type") not in allowed_types:
            findings.append(_finding(
                "lookback_bridge_missing",
                "blocked",
                f"Beat {beat.get('id', index)} 回溯前因但没有合法 causal/time/same-scene 等桥接证据。",
                "story_script",
            ))
        if not (
            beat.get("explains_opening_highlight") is True
            or (transition and transition.get("from_beat_id"))
        ):
            findings.append(_finding(
                "lookback_purpose_missing",
                "blocked",
                f"Beat {beat.get('id', index)} 的回溯没有说明它解释哪个已知冲突或关系状态。",
                "story_script",
            ))

    if lookback_positions and primary:
        for position in lookback_positions:
            returned = any(
                primary in line_for(later)
                for later in beats[position + 1:]
            )
            if not returned:
                findings.append(_finding(
                    "lookback_not_returned_to_mainline",
                    "blocked",
                    "回溯解释后没有回到 primary_story_thread_id 继续向前推进。",
                    "story_plan",
                ))

    # Any independent cross-line jump needs a bridge.  Integrated support is
    # allowed only when it explicitly states how it supports the primary line;
    # its direct evidence is checked by story_coherence_diagnostics.
    for previous, current in zip(beats, beats[1:]):
        previous_line = line_for(previous)
        current_line = line_for(current)
        if previous_line == current_line or is_integrated(current):
            continue
        transition = transition_for(current)
        bridge_ids = set(
            item for item in script.get("required_bridge_beat_ids", [])
            if isinstance(item, str) and item
        )
        current_thread_beats = set(
            item
            for item in current.get("retrieval_requirements", {}).get(
                "thread_beat_ids", []
            )
            if isinstance(item, str)
        )
        if not (
            transition and transition.get("type") in allowed_types
        ) and not (bridge_ids & current_thread_beats):
            findings.append(_finding(
                "cross_segment_bridge_missing",
                "blocked",
                f"{previous.get('id', 'previous')}→{current.get('id', 'current')} 跨主线/次线但没有因果、时间、同场或主题桥接。",
                "story_plan",
            ))

    ending = script.get("ending_hook_intent", {})
    ending_policy = contract.get("ending_policy", {})
    if isinstance(ending, dict) and ending.get("may_be_empty") is True:
        fallback = ending_policy.get("no_hook_fallback")
        if fallback != "current_story_line_episode_tail":
            findings.append(_finding(
                "ending_fallback_not_declared",
                "blocked",
                "没有合适 Hook 时必须显式声明 current_story_line_episode_tail，不得随意结束或接入未来弧。",
                "story_script",
            ))

    extension = contract.get("duration_extension_policy", {})
    if isinstance(extension, dict) and extension:
        required_values = {
            "same_primary_thread_only": True,
            "must_be_forward_chronological": True,
            "no_cross_thread_fill": True,
            "no_duplicate_or_functionless_fill": True,
            "stop_without_evidence": True,
        }
        for key, expected in required_values.items():
            if extension.get(key) is not expected:
                findings.append(_finding(
                    "duration_extension_policy_invalid",
                    "blocked",
                    f"duration_extension_policy.{key} 必须为 {expected}。",
                    "story_plan",
                ))

    status = "blocked" if any(item["status"] == "blocked" for item in findings) else "pass"
    return {
        "status": status,
        "enforced": True,
        "lookback_positions": lookback_positions,
        "findings": findings,
    }


def story_coherence_diagnostics(
    script: dict[str, Any],
    *,
    candidates: dict[str, dict[str, Any]] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check editorial structure without guessing semantic facts or timecodes.

    Hard failures are restricted to quantitative rules from the technical KB.
    Ambiguous language and video-only judgments remain review signals.
    """
    value = policy or load_policy()
    mainline_policy = value["mainline_policy"]
    story_contract = value.get("story_contract", {})
    beats = [item for item in script.get("beats", []) if isinstance(item, dict)]
    lines = [_beat_line(beat) for beat in beats]
    integrated_support_ids = set(
        item for item in (
            script.get("integrated_support_thread_ids", [])
            or script.get("editorial_contract", {}).get("integrated_support_thread_ids", [])
        )
        if isinstance(item, str) and item
    )

    def is_integrated_support(line: tuple[str, ...], beat: dict[str, Any]) -> bool:
        return (
            beat.get("thread_role") == "integrated_support"
            or bool(set(line) & integrated_support_ids)
        )

    effective_lines = [
        ("__integrated_support__",)
        if is_integrated_support(line, beat)
        else line
        for line, beat in zip(lines, beats)
    ]
    # An integrated emotional beat is part of the primary causal chain. A
    # primary → support → primary sequence therefore does not count as two
    # independent thread switches; an unrelated secondary line still does.
    switches = 0
    previous_effective: tuple[str, ...] | None = None
    for line, beat in zip(effective_lines, beats):
        if line == ("__integrated_support__",):
            continue
        if previous_effective is not None and line != previous_effective:
            switches += 1
        previous_effective = line
    weights: dict[str, float] = {}
    beat_counts: dict[str, int] = {}
    for line, beat in zip(lines, beats):
        key = "+".join(line)
        beat_counts[key] = beat_counts.get(key, 0) + 1
        estimate = beat.get("estimated_source_duration_seconds", {})
        weight = estimate.get("maximum", 1.0) if isinstance(estimate, dict) else 1.0
        try:
            weights[key] = weights.get(key, 0.0) + max(0.0, float(weight))
        except (TypeError, ValueError):
            weights[key] = weights.get(key, 0.0) + 1.0
    ordered_lines = list(dict.fromkeys("+".join(line) for line in lines))
    declared_primary = script.get("primary_story_thread_id")
    primary_error = None
    script_thread_ids = set(
        item for item in script.get("story_thread_ids", [])
        if isinstance(item, str) and item
    )
    if declared_primary:
        if declared_primary not in script_thread_ids and script_thread_ids:
            primary_error = (
                f"primary_story_thread_id={declared_primary} 不在 Story Script 的 story_thread_ids 中。"
            )
        elif script.get("primary_story_thread_id_source") == "preflight_inferred" and script_thread_ids:
            primary_error = (
                "未显式声明 primary_story_thread_id；新 Story Script 必须先锁定主线。"
            )
        mainline = str(declared_primary)
    else:
        # Backward-compatible inference for old persisted drafts. New contexts
        # always provide the explicit field and preflight writes it back.
        mainline = max(
            ordered_lines,
            key=lambda key: (weights.get(key, 0.0), beat_counts.get(key, 0)),
            default="__mainline__",
        )
        if script_thread_ids:
            primary_error = (
                "未显式声明 primary_story_thread_id；新 Story Script 必须先锁定主线。"
            )
    total_weight = sum(weights.values()) or 1.0
    secondary = {
        key: round(weight / total_weight, 4)
        for key, weight in weights.items()
        if key != mainline
    }
    independent_weights: dict[str, float] = {}
    integrated_weight = 0.0
    for line, beat in zip(lines, beats):
        key = "+".join(line)
        if is_integrated_support(line, beat):
            integrated_weight += weights.get(key, 0.0) / max(1, beat_counts.get(key, 1))
        elif key != mainline:
            independent_weights[key] = independent_weights.get(key, 0.0) + (
                weights.get(key, 0.0) / max(1, beat_counts.get(key, 1))
            )
    independent_share = max(
        (weight / total_weight for weight in independent_weights.values()),
        default=0.0,
    )
    integrated_share = integrated_weight / total_weight if total_weight else 0.0
    secondary_share = max(secondary.values(), default=0.0)
    findings: list[dict[str, str]] = []
    opening_diagnostics = opening_strategy_diagnostics(script)
    findings.extend(opening_diagnostics.get("findings", []))
    continuity_diagnostics = continuity_contract_diagnostics(script, policy=value)
    findings.extend(continuity_diagnostics.get("findings", []))
    if primary_error and declared_primary and script.get("primary_story_thread_id_source") != "preflight_inferred":
        findings.append(_finding(
            "primary_thread_invalid",
            "blocked",
            primary_error,
            "story_script",
        ))
    elif primary_error:
        findings.append(_finding(
            "primary_thread_implicit",
            "blocked" if len(script_thread_ids) > 1 else "review",
            primary_error,
            "story_script",
        ))
    primary_line_key = str(declared_primary) if declared_primary else mainline
    if declared_primary and not any(
        primary_line_key in line for line in lines
    ):
        findings.append(_finding(
            "primary_thread_not_used",
            "blocked",
            f"声明的主线 {primary_line_key} 没有被任何 Beat 实际承接。",
            "story_script",
        ))
    if switches >= int(story_contract.get("switches_at_or_above", int(mainline_policy["max_thread_switches"]) + 1)):
        findings.append(_finding(
            "oscillating_cross_thread",
            "blocked",
            f"关系线切换 {switches} 次，超过允许的单次边界切换；不能用支线来填充时长。",
            "story_plan",
        ))
    if independent_share > float(mainline_policy["secondary_line_max_share"]):
        findings.append(_finding(
            "secondary_line_overweight",
            "blocked",
            f"独立次线最高占比 {independent_share:.3f} 超过 1/3，主辅关系没有收敛。",
            "story_plan",
        ))
    if integrated_share > float(story_contract.get("secondary_thread_max_share", 1 / 3)):
        findings.append(_finding(
            "integrated_support_overweight",
            "review",
            f"整合型情感支撑线占比 {integrated_share:.3f} 超过 1/3；不能直接删除，但需人工确认其每段都改变主线关系或动机。",
            "story_plan",
        ))
    forbidden_roles = set(mainline_policy["secondary_line_forbidden_roles"])
    for line, beat in zip(lines, beats):
        key = "+".join(line)
        integrated = is_integrated_support(line, beat)
        if key != primary_line_key and not integrated and beat.get("causal_role") in forbidden_roles:
            findings.append(_finding(
                "secondary_line_causal_role",
                "blocked",
                f"次线 {key} 承担 {beat.get('causal_role')}，不能独立完成转折/揭示/结果。",
                "story_plan",
            ))
        if integrated and beat.get("causal_role") in {"escalation", "reveal"}:
            findings.append(_finding(
                "integrated_support_replaces_primary_turn",
                "blocked",
                "整合型情感支撑线不能独立承担主线 escalation/reveal；应由复仇/身份主线完成转折。",
                "story_plan",
            ))
        if integrated and beat.get("causal_role") == "payoff" and not beat.get("supports_primary_thread"):
            findings.append(_finding(
                "integrated_support_payoff_unlinked",
                "blocked",
                "感情兑现必须明确说明它如何改变主线关系、动机或复仇行动；不能只是独立甜戏。",
                "story_script",
            ))
        if integrated:
            direct_event_ids = set(
                item for item in beat.get("event_ids", [])
                if isinstance(item, str) and item
            )
            direct_evidence_ids = direct_event_ids | {
                item
                for show in beat.get("must_show", [])
                if isinstance(show, dict)
                for item in (
                    show.get("evidence_event_ids", [])
                    + show.get("evidence_fact_ids", [])
                )
                if isinstance(item, str) and item
            }
            if not direct_evidence_ids:
                findings.append(_finding(
                    "integrated_support_no_evidence",
                    "blocked",
                    "整合型感情桥段没有直接 Event/Fact 证据，不能凭关系设定或类型模板保留/补写。",
                    "story_script",
                ))
    bridge_ids = set(script.get("required_bridge_beat_ids", []))
    selected_thread_beats = set(script.get("selected_thread_beat_ids", []))
    retrieved_thread_beats = {
        thread_beat_id
        for beat in beats
        for thread_beat_id in beat.get("retrieval_requirements", {}).get("thread_beat_ids", [])
        if isinstance(thread_beat_id, str)
    }
    if switches == 1 and bool(mainline_policy["bridge_required_for_single_switch"]) and not bridge_ids:
        findings.append(_finding(
            "missing_mainline_bridge",
            "blocked",
            "主线发生一次关系切换，但没有声明 required_bridge_beat_ids。",
            "story_plan",
        ))
    if bridge_ids and not bridge_ids <= selected_thread_beats:
        findings.append(_finding(
            "bridge_not_selected",
            "blocked",
            "声明的 required_bridge_beat_ids 没有全部进入 selected_thread_beat_ids。",
            "story_script",
        ))
    if bridge_ids and not bridge_ids <= retrieved_thread_beats:
        findings.append(_finding(
            "bridge_not_retrieved",
            "blocked",
            "声明的 required_bridge_beat_ids 没有被任何 Editorial Beat 的检索条件承接。",
            "story_script",
        ))

    roles = {beat.get("role") for beat in beats}
    missing_arc = []
    for node, required in value["arc_policy"]["required_roles"].items():
        if not roles.intersection(required):
            missing_arc.append(node)
    if missing_arc:
        findings.append(_finding(
            "arc_node_missing",
            "review",
            "起承转合缺少可识别节点：" + "、".join(missing_arc),
            "story_script",
        ))

    question = str(script.get("central_question", ""))
    question_clauses = len(re.findall(r"[?？]", question))
    question_markers = len(re.findall(r"(?:是否|能否|能不能|会不会)", question))
    if (
        question_clauses > int(story_contract.get("central_question_count", 1))
        or question_markers > 1
        or re.search(r"是否[^?？。]*[?？].*是否", question)
    ):
        findings.append(_finding(
            "central_question_not_converged",
            "blocked",
            "central_question 同时容纳多个问题，需收敛到一条主线因果问题。",
            "story_script",
        ))

    if beats:
        teaser_line = _beat_line(beats[0])
        hook_beats = [beat for beat in beats if beat.get("role") == "end_hook"]
        if hook_beats and _beat_line(hook_beats[-1]) != teaser_line and teaser_line != ("__mainline__",):
            findings.append(_finding(
                "ending_hook_cross_thread",
                "blocked",
                "结尾 Hook 与开场 Hook 不在同一条关系因果链上。",
                "story_plan",
            ))
        if hook_beats and declared_primary:
            hook_line = _beat_line(hook_beats[-1])
            if declared_primary not in hook_line:
                findings.append(_finding(
                    "ending_hook_not_primary_thread",
                    "blocked",
                    f"结尾 Hook 没有落在声明的主线 {declared_primary} 上。",
                    "story_plan",
                ))
    hook_type = script.get("ending_hook_intent", {}).get("hook_type")
    if hook_type and hook_type not in set(value["arc_policy"]["end_hook_allowed_landing"]):
        findings.append(_finding(
            "ending_hook_type_invalid",
            "blocked",
            f"结尾 Hook 类型 {hook_type} 不属于允许的未完成态。",
            "story_script",
        ))

    candidate_id = (script.get("teaser_contract") or {}).get("primary_highlight_candidate_id")
    candidate = (candidates or {}).get(candidate_id, {}) if isinstance(candidate_id, str) else {}
    teaser = beats[0] if beats and beats[0].get("role") == "teaser_intent" else {}
    must_show = teaser.get("must_show", []) if isinstance(teaser, dict) else []
    observable = any(item.get("observable_via") in {"action", "dialogue", "mixed"} for item in must_show if isinstance(item, dict))
    conflict_words = re.compile(r"冲突|对峙|争吵|威胁|羞辱|反击|打斗|揭穿|真相|betray|fight|threat|confront|reveal", re.I)
    conflict_visible = observable and bool(conflict_words.search(_text(candidate)))
    informative = bool(must_show) and any(len(str(item.get("description", ""))) >= 8 for item in must_show if isinstance(item, dict))
    open_question = bool(str(candidate.get("payoff_or_open_question", "")).strip()) or bool(str(script.get("ending_hook_intent", {}).get("question", "")).strip())
    hook_signals = sum([conflict_visible, informative, open_question])
    opening_signal_audit: dict[str, Any] = {
        "signal_types": list(candidate.get("opening_signal_types", []))
        if isinstance(candidate.get("opening_signal_types"), list)
        else [],
        "first_three_seconds_signal": candidate.get("first_three_seconds_signal"),
        "action_or_speech_complete": candidate.get("action_or_speech_complete"),
        "context_within_8_seconds": candidate.get("context_within_8_seconds"),
        "lead_in_artifact": candidate.get("lead_in_artifact"),
        "lead_in_duration_seconds": candidate.get("lead_in_duration_seconds"),
        "source_start_is_effective_opening_frame": candidate.get(
            "source_start_is_effective_opening_frame"
        ),
        "effective_opening_frame_note": candidate.get(
            "effective_opening_frame_note", ""
        ),
        "cut_risk": candidate.get("cut_risk"),
    }
    # These checks activate when the new candidate audit fields are present;
    # absent fields remain compatible with old cached candidate catalogs.
    opening_audit_declared = candidate and any(
        key in candidate
        for key in (
            "opening_signal_types",
            "first_three_seconds_signal",
            "action_or_speech_complete",
            "context_within_8_seconds",
            "cut_risk",
            "lead_in_artifact",
            "lead_in_duration_seconds",
            "source_start_is_effective_opening_frame",
            "effective_opening_frame_note",
        )
    )
    if opening_audit_declared:
        if candidate.get("first_three_seconds_signal") is False or not opening_signal_audit["signal_types"]:
            findings.append(_finding(
                "opening_signal_missing",
                "blocked",
                "高光候选在第0秒/前3秒没有可验证的强视觉或听觉信号，不能作为冷开场。",
                "story_script",
            ))
        if candidate.get("action_or_speech_complete") is False:
            findings.append(_finding(
                "opening_action_or_speech_incomplete",
                "blocked",
                "高光候选的动作或台词在硬切处不完整；必须向前/向后扩展到安全语义边界。",
                "boundary_repair",
            ))
        if candidate.get("context_within_8_seconds") is False:
            findings.append(_finding(
                "opening_context_unreadable",
                "blocked",
                "高光虽有刺激信号，但前8秒无法看明白人物、对象或事件关系。",
                "story_script",
            ))
        if candidate.get("cut_risk") == "high":
            findings.append(_finding(
                "opening_cut_risk_high",
                "blocked",
                "高光核心帧会造成吞动作、吞台词或孤帧，不能直接硬切通过。",
                "boundary_repair",
            ))
        lead_in_artifact = candidate.get("lead_in_artifact")
        invalid_lead_in = {
            "black_flash",
            "packaging",
            "static_or_frozen",
            "non_narrative",
        }
        if lead_in_artifact in invalid_lead_in and candidate.get(
            "source_start_is_effective_opening_frame"
        ) is not True:
            findings.append(_finding(
                "opening_lead_in_artifact_not_trimmed",
                "blocked",
                "候选起点前存在无功能闪黑/包装/静帧/空白前导，且 source_start 尚未对齐第一帧有效剧情；不能直接渲染。",
                "boundary_repair",
            ))
        if lead_in_artifact == "uncertain":
            findings.append(_finding(
                "opening_effective_frame_unresolved",
                "blocked",
                "无法确认第一帧有效剧情画面；必须补做连续源片边界核对，不能按分析窗口起点猜切点。",
                "boundary_repair",
            ))
        if (
            lead_in_artifact == "intentional_story_black"
            and not str(candidate.get("effective_opening_frame_note", "")).strip()
        ):
            findings.append(_finding(
                "opening_black_frame_without_narrative_function",
                "blocked",
                "保留故事功能黑场必须提供可核对的叙事功能说明；普通闪黑不得以氛围名义保留。",
                "story_script",
            ))
    if candidate and hook_signals < int(value["hook_policy"]["minimum_strength_signals"]):
        findings.append(_finding(
            "weak_primary_hook",
            (
                "blocked"
                if opening_diagnostics.get("strategy")
                in {
                    "causal_explanatory_opening",
                    "causal_explanatory_no_reprise",
                    "causal_explanatory_delayed_reprise",
                    "original_chronological_opening",
                }
                else "review"
            ),
            f"开场 Hook 仅满足 {hook_signals}/3 项强度信号，需人工或回到 Story Script 重新遴选。",
            "story_script",
        ))

    has_fill_language = any(
        re.search(r"凑时长|填充|扩充时长|filler|padding", str(beat.get("dramatic_purpose", "")), re.I)
        for beat in beats
    )
    if has_fill_language:
        findings.append(_finding(
            "functionless_duration_fill",
            "blocked",
            "Beat 明确以凑时长/填充为目的，必须删除或改为同一主线的叙事功能。",
            "story_plan",
        ))

    status = "blocked" if any(item["status"] == "blocked" for item in findings) else ("review" if findings else "pass")
    return {
        "policy_version": value["schema_version"],
        "status": status,
        "mainline": mainline,
        "primary_story_thread_id": declared_primary or (mainline if mainline != "__mainline__" else ""),
        "thread_sequence": ["+".join(line) for line in lines],
        "thread_switch_count": switches,
        "secondary_line_share": secondary_share,
        "secondary_line_shares": secondary,
        "independent_secondary_line_share": round(independent_share, 4),
        "integrated_support_line_share": round(integrated_share, 4),
        "integrated_support_thread_ids": sorted(integrated_support_ids),
        "arc_nodes_present": sorted(roles.intersection({"orientation", "setup", "escalation", "turn_or_reveal", "payoff", "end_hook"})),
        "hook_strength": {
            "conflict_is_observable": conflict_visible,
            "relationship_and_stakes_are_understandable": informative,
            "open_question_remains": open_question,
            "signals": hook_signals,
        },
        "opening_signal_audit": opening_signal_audit,
        "opening_strategy": opening_diagnostics,
        "continuity_contract": continuity_diagnostics,
        "findings": findings,
        "failure_codes": [item["code"] for item in findings if item["status"] == "blocked"],
        "repair_routes": _unique(item["repair_route"] for item in findings),
        "duration_policy_applied": "coherence_first_no_functionless_fill",
    }


def diagnostics_as_qc_checks(diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for item in diagnostics.get("findings", []):
        status = item.get("status", "review")
        checks.append({
            "id": f"editorial-{item.get('code', 'finding')}",
            "status": "block" if status == "blocked" else "review",
            "description": item.get("description", "编导规则需要复核"),
            "related_ids": [item.get("code", "editorial")],
        })
    return checks