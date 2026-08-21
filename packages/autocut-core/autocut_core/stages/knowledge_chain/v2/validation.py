"""Validated legacy warnings and overlap handling for Knowledge Chain V2."""

from __future__ import annotations

import logging

from .schemas import KnowledgeChainV2ExtraConfig
from .types import Chapter, GlobalFramework, JSONObject, json_object_list

logger = logging.getLogger(__name__)


class ValidationWarning:
    def __init__(
        self, code: str, message: str, chapter_id: str | None = None, severity: str = "warning"
    ) -> None:
        self.code = code
        self.message = message
        self.chapter_id = chapter_id
        self.severity = severity

    def to_dict(self) -> JSONObject:
        return {
            "code": self.code,
            "message": self.message,
            "chapter_id": self.chapter_id,
            "severity": self.severity,
        }


def _chapter_id(chapter: Chapter) -> str:
    return chapter.get("chapter_id", "unknown")


def _warn(warnings: list[ValidationWarning]) -> None:
    for warning in warnings:
        logger.warning("[Validation][%s] %s", warning.code, warning.message)


def validate_pass1_output(
    pass1_out: JSONObject, chapter: Chapter, global_framework: GlobalFramework
) -> tuple[bool, list[ValidationWarning]]:
    warnings: list[ValidationWarning] = []
    updates = pass1_out.get("story_thread_updates")
    if not isinstance(updates, list) or not all(isinstance(update, dict) for update in updates):
        warnings.append(
            ValidationWarning(
                "PASS1_MISSING_STORY_THREADS",
                "Pass1输出缺少story_thread_updates字段或格式错误",
                _chapter_id(chapter),
                "error",
            )
        )
        _warn(warnings)
        return True, warnings
    update_objects = json_object_list(updates)
    beats = sum(
        len(beat_list)
        for update in update_objects
        if isinstance((beat_list := update.get("beats")), list)
    )
    if beats < 3:
        warnings.append(
            ValidationWarning(
                "PASS1_TOO_FEW_BEATS",
                f"章节beat数量过少，仅{beats}个，可能遗漏核心剧情",
                _chapter_id(chapter),
            )
        )
    summary = pass1_out.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        warnings.append(
            ValidationWarning(
                "PASS1_MISSING_SUMMARY", "Pass1输出缺少chapter summary", _chapter_id(chapter)
            )
        )
    valid_ids: set[str] = set()
    for thread in global_framework.get("global_story_threads", []):
        thread_id = thread.get("thread_id")
        if isinstance(thread_id, str):
            valid_ids.add(thread_id)
    for update in update_objects:
        thread_id = update.get("thread_id")
        if isinstance(thread_id, str) and thread_id and thread_id not in valid_ids:
            warnings.append(
                ValidationWarning(
                    "PASS1_INVALID_THREAD_ID",
                    f"使用了不存在的故事线ID: {thread_id}，将自动回退到主线",
                    _chapter_id(chapter),
                )
            )
    _warn(warnings)
    return any(warning.severity == "error" for warning in warnings), warnings


def validate_pass2_output(
    pass2_out: JSONObject, chapter: Chapter, global_framework: GlobalFramework
) -> tuple[bool, list[ValidationWarning]]:
    warnings: list[ValidationWarning] = []
    characters, relationships = (
        pass2_out.get("character_rollup"),
        pass2_out.get("relationship_rollup"),
    )
    if not isinstance(characters, list) or not all(isinstance(item, dict) for item in characters):
        warnings.append(
            ValidationWarning(
                "PASS2_MISSING_CHARACTERS",
                "Pass2输出缺少character_rollup字段或格式错误",
                _chapter_id(chapter),
                "error",
            )
        )
    if not isinstance(relationships, list) or not all(
        isinstance(item, dict) for item in relationships
    ):
        warnings.append(
            ValidationWarning(
                "PASS2_MISSING_RELATIONSHIPS",
                "Pass2输出缺少relationship_rollup字段或格式错误",
                _chapter_id(chapter),
                "error",
            )
        )
    if warnings:
        _warn(warnings)
        return True, warnings
    character_objects = json_object_list(characters)
    if not character_objects:
        warnings.append(
            ValidationWarning(
                "PASS2_NO_CHARACTERS", "章节输出未提取到任何角色", _chapter_id(chapter)
            )
        )
    core_names = {
        str(char.get("name", "")).lower()
        for char in global_framework.get("global_characters", [])
        if char.get("is_core") is True or char.get("importance") in (0.8, 0.9, 1, 1.0)
    }
    names = {str(char.get("name", "")).lower() for char in character_objects}
    if core_names and not core_names.intersection(names):
        warnings.append(
            ValidationWarning(
                "PASS2_NO_CORE_CHARACTERS",
                "章节未出现任何核心角色，请检查是否提取失败",
                _chapter_id(chapter),
            )
        )
    _warn(warnings)
    return False, warnings


def validate_extension_fields(
    pass1_out: JSONObject,
    _pass2_out: JSONObject,
    extra_config: KnowledgeChainV2ExtraConfig,
    chapter: Chapter,
) -> list[ValidationWarning]:
    warnings: list[ValidationWarning] = []
    fields = (
        (extra_config.enable_world_rules, "new_world_rules", "EXT_WORLD_RULES_INVALID"),
        (extra_config.enable_questions, "new_open_questions", "EXT_NEW_QUESTIONS_INVALID"),
        (extra_config.enable_questions, "resolved_questions", "EXT_RESOLVED_QUESTIONS_INVALID"),
        (extra_config.enable_foreshadows, "foreshadow_payoffs", "EXT_FORESHADOWS_INVALID"),
    )
    for enabled, field, code in fields:
        if enabled and not isinstance(pass1_out.get(field, []), list):
            pass1_out[field] = []
            warnings.append(
                ValidationWarning(
                    code, f"{field}字段格式错误，已降级为空数组", _chapter_id(chapter)
                )
            )
    _warn(warnings)
    return warnings


def deduplicate_overlap_events(
    chapter_outputs: list[JSONObject], _global_framework: GlobalFramework
) -> tuple[list[JSONObject], list[ValidationWarning]]:
    warnings: list[ValidationWarning] = []
    for index in range(len(chapter_outputs) - 1):
        previous, following = chapter_outputs[index], chapter_outputs[index + 1]
        previous_chapter, next_chapter = previous.get("chapter"), following.get("chapter")
        if not isinstance(previous_chapter, dict) or not isinstance(next_chapter, dict):
            continue
        start, end = next_chapter.get("start_ep"), previous_chapter.get("end_ep")
        if not isinstance(start, int) or not isinstance(end, int) or start > end:
            continue
        overlap = set(range(start, end + 1))
        if len(overlap) > 1:
            warnings.append(
                ValidationWarning(
                    "OVERLAP_TOO_MANY_EPS",
                    f"章节重叠{len(overlap)}集，超过1集的建议阈值",
                    str(next_chapter.get("chapter_id", "unknown")),
                )
            )
        previous["beats"] = [
            beat
            for beat in json_object_list(previous.get("beats"))
            if beat.get("episode") not in overlap
        ]
        previous["excluded_episodes"] = [
            item
            for item in json_object_list(previous.get("excluded_episodes"))
            if item.get("episode") not in overlap
        ]
    _warn(warnings)
    return chapter_outputs, warnings
