"""Layer2: Chapter Processor 逐章填充层 - 每章2次LLM调用填充细节"""

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from .prompts import LAYER2_PASS1_BEAT_PROMPT, LAYER2_PASS2_ENTITY_PROMPT
from .schemas import KnowledgeChainV2ExtraConfig
from .types import (
    Chapter,
    ChapterOutput,
    EventCard,
    GlobalFramework,
    JSONObject,
    JSONValue,
    LLMProvider,
    Pass1Output,
    Pass2Output,
)
from .utils import (
    build_short_id_map,
    exact_name_match,
    generate_beat_id,
    generate_char_id,
    generate_rel_id,
    map_short_id,
    phase_sort_key,
)
from .validation import (
    ValidationWarning,
    validate_extension_fields,
    validate_pass1_output,
    validate_pass2_output,
)

logger = logging.getLogger(__name__)

PHASE_ORDER = ["setup", "escalation", "turn", "reveal", "payoff", "consequence", "coda"]


DebugValue = str | JSONObject | Pass1Output | Pass2Output | ChapterOutput
DebugCallback = Callable[[str, DebugValue], None]


class RawBeat(TypedDict):
    beat_sid: str
    episode: int
    phase: str
    event_eids: list[str]
    summary: str
    depends_on_beat_sids: list[str]


class RawStoryThreadUpdate(TypedDict):
    thread_id: str
    beats: list[RawBeat]


class RawExcludedEpisode(TypedDict):
    episode: int
    event_eids: list[str]
    reason_type: str
    explanation: str


class RawCharacter(TypedDict):
    character_key: str
    name: str
    aliases: list[str]
    state_at_start: str
    state_at_end: str
    importance_tier: str
    evidence_eids: list[str]


class RawRelationship(TypedDict):
    relationship_key: str
    character_key_a: str
    character_key_b: str
    summary: str
    importance: float
    evidence_eids: list[str]


class NormalizedPass1(TypedDict):
    summary: str
    story_thread_updates: list[RawStoryThreadUpdate]
    excluded_episodes: list[RawExcludedEpisode]
    new_facts: list[JSONValue]
    resolved_questions: list[JSONValue]
    new_open_questions: list[JSONValue]
    new_world_rules: list[JSONValue]
    foreshadow_payoffs: list[JSONValue]


class NormalizedPass2(TypedDict):
    character_rollup: list[RawCharacter]
    relationship_rollup: list[RawRelationship]
    new_character_reasoning: str


class ProcessedBeat(TypedDict):
    id: str
    chapter_id: str
    thread_id: str
    episode: int
    phase: str
    summary: str
    evidence_event_ids: list[str]
    requires_beat_ids: list[str]
    depends_on_beat_sids: NotRequired[list[str]]


class ProcessedExcludedEpisode(TypedDict):
    episode: int
    event_ids: list[str]
    reason_type: str
    explanation: str


class RollingCharacter(RawCharacter):
    last_seen_chapter: int


def _string(value: JSONValue | None, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _integer(value: JSONValue | None, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _number(value: JSONValue | None, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _string_list(value: JSONValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _json_list(value: JSONValue | None) -> list[JSONValue]:
    return list(value) if isinstance(value, list) else []


def _object_list(value: JSONValue | None) -> list[JSONObject]:
    return [item for item in _json_list(value) if isinstance(item, dict)]


def _raw_beat(value: JSONObject) -> RawBeat | None:
    beat_sid = _string(value.get("beat_sid"))
    episode = _integer(value.get("episode"))
    phase = _string(value.get("phase"))
    summary = _string(value.get("summary"))
    if not beat_sid or episode < 1 or phase not in PHASE_ORDER or not summary:
        return None
    return {
        "beat_sid": beat_sid,
        "episode": episode,
        "phase": phase,
        "summary": summary,
        "event_eids": _string_list(value.get("event_eids")),
        "depends_on_beat_sids": _string_list(value.get("depends_on_beat_sids")),
    }


def _normalise_pass1(value: JSONObject) -> NormalizedPass1:
    """Decode provider JSON once; malformed semantic objects are discarded.

    A parseable JSON document is not sufficient evidence that it is a usable
    Chapter Processor response.  This normalization returns a complete,
    explicit fallback shape and never mutates the provider-owned object.
    """

    updates: list[RawStoryThreadUpdate] = []
    for update in _object_list(value.get("story_thread_updates")):
        thread_id = _string(update.get("thread_id"))
        beats = [
            beat
            for item in _object_list(update.get("beats"))
            if (beat := _raw_beat(item)) is not None
        ]
        if thread_id:
            updates.append({"thread_id": thread_id, "beats": beats})

    excluded: list[RawExcludedEpisode] = []
    for item in _object_list(value.get("excluded_episodes")):
        episode = _integer(item.get("episode"))
        reason_type = _string(item.get("reason_type"))
        explanation = _string(item.get("explanation"))
        if episode >= 1 and reason_type and explanation:
            excluded.append(
                {
                    "episode": episode,
                    "event_eids": _string_list(item.get("event_eids")),
                    "reason_type": reason_type,
                    "explanation": explanation,
                }
            )

    return {
        "summary": _string(value.get("summary")),
        "story_thread_updates": updates,
        "excluded_episodes": excluded,
        "new_facts": _json_list(value.get("new_facts")),
        "resolved_questions": _json_list(value.get("resolved_questions")),
        "new_open_questions": _json_list(value.get("new_open_questions")),
        "new_world_rules": _json_list(value.get("new_world_rules")),
        "foreshadow_payoffs": _json_list(value.get("foreshadow_payoffs")),
    }


def _normalise_pass2(value: JSONObject) -> NormalizedPass2:
    characters: list[RawCharacter] = []
    for item in _object_list(value.get("character_rollup")):
        name = _string(item.get("name"))
        if not name:
            continue
        characters.append(
            {
                "character_key": _string(item.get("character_key")),
                "name": name,
                "aliases": _string_list(item.get("aliases")),
                "state_at_start": _string(item.get("state_at_start")),
                "state_at_end": _string(item.get("state_at_end")),
                "importance_tier": _string(item.get("importance_tier"), "minor"),
                "evidence_eids": _string_list(item.get("evidence_eids")),
            }
        )

    relationships: list[RawRelationship] = []
    for item in _object_list(value.get("relationship_rollup")):
        relationship_key = _string(item.get("relationship_key"))
        character_key_a = _string(item.get("character_key_a"))
        character_key_b = _string(item.get("character_key_b"))
        summary = _string(item.get("summary"))
        if not all((relationship_key, character_key_a, character_key_b, summary)):
            continue
        relationships.append(
            {
                "relationship_key": relationship_key,
                "character_key_a": character_key_a,
                "character_key_b": character_key_b,
                "summary": summary,
                "importance": _number(item.get("importance"), 0.2),
                "evidence_eids": _string_list(item.get("evidence_eids")),
            }
        )

    return {
        "character_rollup": characters,
        "relationship_rollup": relationships,
        "new_character_reasoning": _string(value.get("new_character_reasoning")),
    }


def _json_object(value: dict[str, object]) -> JSONObject:
    converted: JSONObject = {}
    for key, item in value.items():
        accepted, json_item = _json_value(item)
        if not accepted:
            raise ValueError(f"cannot encode non-JSON value for {key}")
        converted[key] = json_item
    return converted


def _pass1_as_json(value: NormalizedPass1) -> JSONObject:
    payload: dict[str, object] = {
        "summary": value["summary"],
        "story_thread_updates": [
            {
                "thread_id": update["thread_id"],
                "beats": [
                    {
                        "beat_sid": beat["beat_sid"],
                        "episode": beat["episode"],
                        "phase": beat["phase"],
                        "event_eids": beat["event_eids"],
                        "summary": beat["summary"],
                        "depends_on_beat_sids": beat["depends_on_beat_sids"],
                    }
                    for beat in update["beats"]
                ],
            }
            for update in value["story_thread_updates"]
        ],
        "excluded_episodes": [dict(item) for item in value["excluded_episodes"]],
        "new_facts": list(value["new_facts"]),
        "resolved_questions": list(value["resolved_questions"]),
        "new_open_questions": list(value["new_open_questions"]),
        "new_world_rules": list(value["new_world_rules"]),
        "foreshadow_payoffs": list(value["foreshadow_payoffs"]),
    }
    return _json_object(payload)


def _pass2_as_json(value: NormalizedPass2) -> JSONObject:
    payload: dict[str, object] = {
        "character_rollup": [dict(item) for item in value["character_rollup"]],
        "relationship_rollup": [dict(item) for item in value["relationship_rollup"]],
        "new_character_reasoning": value["new_character_reasoning"],
    }
    return _json_object(payload)


def _processed_beat_as_json(value: ProcessedBeat) -> JSONObject:
    payload: dict[str, object] = {
        "id": value["id"],
        "chapter_id": value["chapter_id"],
        "thread_id": value["thread_id"],
        "episode": value["episode"],
        "phase": value["phase"],
        "summary": value["summary"],
        "evidence_event_ids": list(value["evidence_event_ids"]),
        "requires_beat_ids": list(value["requires_beat_ids"]),
    }
    return _json_object(payload)


class ChapterProcessor:
    MAX_RETRIES = 1  # 每个阶段最多重试1次，失败则回退到fallback

    def __init__(
        self,
        llm_provider: LLMProvider,
        global_framework: GlobalFramework,
        extra_config: KnowledgeChainV2ExtraConfig | None = None,
        debug_callback: DebugCallback | None = None,
        debug_dir: Path | None = None,
    ) -> None:
        self.llm_provider = llm_provider
        self.global_framework = global_framework
        self.extra_config = extra_config or KnowledgeChainV2ExtraConfig()
        self.debug_callback = debug_callback  # callable(filename, content) or None
        self.debug_dir = debug_dir  # Path object for per-chapter debug directories
        self.rolling_chars: list[RollingCharacter] = []
        self.current_chapter_idx: int = 0
        self.last_ch_summary: str = ""
        # 扩展字段收集器
        # 预注入Layer1提取的全局核心世界观规则，避免逐章冷启动、重复提取
        self.collected_world_rules: list[JSONObject] = []
        for rule in global_framework.get("global_world_rules", []):
            rule_id = _string(rule.get("rule_id"))
            description = _string(rule.get("description"))
            established_ep = _integer(rule.get("established_ep"), 1)
            if rule_id or description:
                self.collected_world_rules.append(
                    {
                        "rule_id": rule_id,
                        "description": description,
                        "established_ep": established_ep,
                        "is_global": True,  # 标记为全局核心规则
                    }
                )
        self.open_questions: list[JSONValue] = []
        self.resolved_questions: list[JSONValue] = []
        self.foreshadow_pairs: list[JSONValue] = []
        # 警告收集
        self.warnings: list[ValidationWarning] = []

    def update_rolling_context(self, chapter_output: ChapterOutput) -> None:
        """更新rolling context：跟踪last_seen_chapter，淘汰3章未出现的角色"""
        new_chars = _normalise_pass2(
            _json_object({"character_rollup": chapter_output.get("character_rollup", [])})
        )["character_rollup"]
        current_idx = self.current_chapter_idx

        # 更新已有角色的last_seen
        for c in new_chars:
            matched = False
            for existing in self.rolling_chars:
                if exact_name_match(c["name"], existing["name"], c["aliases"], existing["aliases"]):
                    existing["last_seen_chapter"] = current_idx
                    existing["state_at_end"] = c["state_at_end"] or existing["state_at_end"]
                    for alias in c["aliases"]:
                        if alias not in existing["aliases"]:
                            existing["aliases"].append(alias)
                    matched = True
                    break
            if not matched:
                # 新角色加入
                self.rolling_chars.append({**c, "last_seen_chapter": current_idx})

        # 淘汰超过3章未出现的角色（last_seen ≤ current_idx - 3）
        self.rolling_chars = [
            c for c in self.rolling_chars if c["last_seen_chapter"] >= current_idx - 2
        ]
        self.last_ch_summary = _string(chapter_output.get("summary"))[:100]
        self.current_chapter_idx = current_idx + 1

    async def process_chapter(
        self,
        chapter: Chapter,
        chapter_events: list[EventCard],
        ch_index: int,
        prev_summary: str = "",
    ) -> JSONObject:
        """处理单个章节：Pass1节拍填充 + Pass2实体提取，带校验和重试"""
        chapter_id = _string(chapter.get("chapter_id"), f"ch{ch_index:02d}")
        # 重置章节警告，避免累积
        chapter_warnings: list[ValidationWarning] = []
        overlap_eps = self._get_overlap_eps(ch_index)
        logger.info(f"Processing chapter {chapter_id}, overlap eps: {overlap_eps}")
        self._current_ch_index = ch_index

        # 1. Pass1: 节拍填充 + 校验重试
        pass1_result: JSONObject = {}
        sid_to_eid: dict[str, str] = {}
        for retry in range(self.MAX_RETRIES + 1):
            pass1_result = await self._run_pass1(
                chapter, chapter_events, overlap_eps, prev_summary or self.last_ch_summary
            )
            need_retry, warnings = validate_pass1_output(
                pass1_result, chapter, self.global_framework
            )
            chapter_warnings.extend(warnings)

            if not need_retry or retry == self.MAX_RETRIES:
                break
            logger.info(
                f"Pass1 validation failed for {chapter_id}, retrying ({retry + 1}/{self.MAX_RETRIES})..."
            )

        # 2. Pass1本地校验修复
        pass1_result, sid_to_eid = self._validate_pass1(
            pass1_result, chapter, chapter_events, overlap_eps, chapter_id
        )

        # 保存解析后的pass1_output.json
        ch_debug_dir: Path | None = self.debug_dir / chapter_id if self.debug_dir else None
        if ch_debug_dir:
            import json

            (ch_debug_dir / "pass1_output.json").write_text(
                json.dumps(pass1_result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        elif self.debug_callback:
            self.debug_callback(f"layer2_{chapter_id}_pass1_output.json", pass1_result)

        # 3. 扩展字段校验
        ext_warnings = validate_extension_fields(pass1_result, {}, self.extra_config, chapter)
        chapter_warnings.extend(ext_warnings)

        # 4. Pass2: 实体提取 + 校验重试
        pass2_result: JSONObject = {}
        for retry in range(self.MAX_RETRIES + 1):
            pass2_result = await self._run_pass2(chapter, chapter_events, pass1_result)
            need_retry, warnings = validate_pass2_output(
                pass2_result, chapter, self.global_framework
            )
            chapter_warnings.extend(warnings)

            if not need_retry or retry == self.MAX_RETRIES:
                break
            logger.info(
                f"Pass2 validation failed for {chapter_id}, retrying ({retry + 1}/{self.MAX_RETRIES})..."
            )

        # 5. Pass2本地校验修复
        # Pass2复用同一个短ID映射表
        pass2_result = self._validate_pass2(pass2_result, chapter_events, sid_to_eid)

        # 保存解析后的pass2_output.json
        ch_debug_dir = self.debug_dir / chapter_id if self.debug_dir else None
        if ch_debug_dir:
            import json

            (ch_debug_dir / "pass2_output.json").write_text(
                json.dumps(pass2_result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        elif self.debug_callback:
            self.debug_callback(f"layer2_{chapter_id}_pass2_output.json", pass2_result)

        # 6. 收集章节警告并保存error.log
        chapter_warnings_dict: list[JSONObject] = [
            {
                "code": warning.code,
                "message": warning.message,
                "chapter_id": warning.chapter_id,
                "severity": warning.severity,
            }
            for warning in chapter_warnings
        ]
        if ch_debug_dir and chapter_warnings_dict:
            import json

            (ch_debug_dir / "error.log").write_text(
                json.dumps(chapter_warnings_dict, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        # 合并结果
        result: JSONObject = _json_object(
            {
                "chapter_id": chapter_id,
                "chapter": chapter,
                "overlap_eps": overlap_eps,
                "warnings": chapter_warnings_dict,
                **pass1_result,
                **pass2_result,
            }
        )
        return result

    async def _run_pass1(
        self,
        chapter: Chapter,
        chapter_events: list[EventCard],
        overlap_eps: list[int],
        prev_summary: str,
    ) -> JSONObject:
        """运行Pass1节拍填充"""
        # 构建事件DSL
        events_dsl_lines: list[str] = []
        for idx, e in enumerate(chapter_events):
            episode = _integer(e.get("ep"), _integer(e.get("episode"), 0))
            content = _string(
                e.get("content"), _string(e.get("summary"), _string(e.get("description")))
            )
            events_dsl_lines.append(f"E{idx + 1} [第{episode}集]: {content}")
        events_dsl = "\n".join(events_dsl_lines)
        overlap_eps_str = "、".join([f"第{ep}集" for ep in overlap_eps]) if overlap_eps else "无"
        # 构建故事线prompt
        ch_index = getattr(self, "_current_ch_index", 0)
        threads_prompt = self._build_threads_prompt(ch_index)

        # 渲染Pass1 Prompt
        pass1_prompt = LAYER2_PASS1_BEAT_PROMPT.format(
            chapter_start_ep=_integer(chapter.get("start_ep")),
            chapter_end_ep=_integer(chapter.get("end_ep")),
            chapter_core_conflict=chapter.get("core_conflict", ""),
            overlap_eps=overlap_eps_str or "无",
            global_threads_prompt=threads_prompt,
            prev_summary=prev_summary or "这是第一章",
            chapter_events_dsl=events_dsl,
        )
        # 扩展字段要求
        foreshadow_req = ""
        world_rules_req = ""
        questions_req = ""

        if self.extra_config.enable_foreshadows:
            foreshadow_req = '- 增加"foreshadow_payoffs"字段，如果本章出现了之前章节伏笔的回收，列出每个回收项：setup_beat_sid(对应之前章节伏笔的beat短ID)、description(伏笔内容+回收说明)'

        if self.extra_config.enable_world_rules:
            world_rules_req = (
                '- 增加"new_world_rules"字段，列出本章新揭示的世界观规则/设定，不要重复已有规则'
            )

        if self.extra_config.enable_questions:
            questions_req = '- 增加"new_open_questions"字段，列出本章留下的新悬念/疑问；增加"resolved_questions"字段，列出本章解答的之前的疑问'

        # 替换所有占位符和清理残留
        prompt = pass1_prompt.replace("<!-- FORESHADOW_REQUIREMENT -->", foreshadow_req)
        prompt = prompt.replace("<!-- WORLD_RULES_REQUIREMENT -->", world_rules_req)
        prompt = prompt.replace("<!-- QUESTIONS_REQUIREMENT -->", questions_req)
        prompt = re.sub(r"<!-- [A-Z_]+ -->", "", prompt)
        prompt = re.sub(r"___PLACEHOLDER___", "", prompt)  # 清理残留占位符
        # 按章节分目录保存调试产物
        ch_debug_dir = None
        if self.debug_dir:
            ch_debug_dir = self.debug_dir / _string(chapter.get("chapter_id"), "unknown")
            ch_debug_dir.mkdir(parents=True, exist_ok=True)
            # 保存pass1_prompt.md
            (ch_debug_dir / "pass1_prompt.md").write_text(prompt, encoding="utf-8")
        elif self.debug_callback:
            self.debug_callback(
                f"layer2_{_string(chapter.get('chapter_id'), 'unknown')}_pass1_prompt.md", prompt
            )

        llm_result = await self.llm_provider(prompt, response_format={"type": "json_object"})

        # 保存pass1_raw.json
        if ch_debug_dir:
            (ch_debug_dir / "pass1_raw.json").write_text(llm_result, encoding="utf-8")
        elif self.debug_callback:
            self.debug_callback(
                f"layer2_{_string(chapter.get('chapter_id'), 'unknown')}_pass1_raw.json", llm_result
            )

        parsed = _robust_parse_json(llm_result)
        # 第一次16K解析失败/不完整，自动用32K tokens重试一次
        ch_id = _string(chapter.get("chapter_id"), "unknown")
        ch_ep_count = _integer(chapter.get("end_ep")) - _integer(chapter.get("start_ep")) + 1
        is_incomplete = parsed is None
        if parsed is not None:
            normalised = _normalise_pass1(parsed)
            total_beats = sum(len(update["beats"]) for update in normalised["story_thread_updates"])
            required_fields = ["summary", "story_thread_updates"]
            missing_fields = [f for f in required_fields if f not in parsed]
            is_incomplete = (
                total_beats < 3
                or (ch_ep_count >= 5 and "excluded_episodes" not in parsed)
                or missing_fields
            )

        if is_incomplete:
            logger.warning(
                f"Pass1 16K output incomplete/parse failed for {ch_id}, retrying with 32K tokens..."
            )
            # 兼容调用llm_provider，优先传max_tokens，不支持则用默认参数
            try:
                llm_result_32k = await self.llm_provider(
                    prompt, response_format={"type": "json_object"}, max_tokens=32768
                )
            except TypeError:
                llm_result_32k = await self.llm_provider(
                    prompt, response_format={"type": "json_object"}
                )
            parsed = _robust_parse_json(llm_result_32k)
            # 保存32K结果到debug文件
            if ch_debug_dir:
                (ch_debug_dir / "pass1_32k_raw.json").write_text(llm_result_32k, encoding="utf-8")
            elif self.debug_callback:
                self.debug_callback(f"layer2_{ch_id}_pass1_32k_raw.json", llm_result_32k)
        if parsed is not None:
            return _pass1_as_json(_normalise_pass1(parsed))
        logger.warning(
            "Pass1 parse failed for %s even with 32K tokens; using explicit empty fallback. "
            "Raw output prefix: %s",
            ch_id,
            llm_result[:500],
        )
        return _pass1_as_json(_normalise_pass1({}))

    def _validate_pass1(
        self,
        pass1_out: JSONObject,
        chapter: Chapter,
        chapter_events: list[EventCard],
        overlap_eps: list[int],
        chapter_id: str,
    ) -> tuple[JSONObject, dict[str, str]]:
        """Map validated Pass1 output without changing the caller's retry input."""
        normalised = _normalise_pass1(pass1_out)
        sid_to_eid = build_short_id_map(chapter_events)
        valid_thread_ids = [
            _string(thread.get("thread_id"))
            for thread in self.global_framework.get("global_story_threads", [])
            if _string(thread.get("thread_id"))
        ]
        fallback_thread_id = valid_thread_ids[0] if valid_thread_ids else "thread-main"
        chapter_start = _integer(chapter.get("start_ep"), 1)
        beat_sid_to_gid: dict[str, str] = {}
        beats: list[ProcessedBeat] = []
        used_event_ids: set[str] = set()

        for update in normalised["story_thread_updates"]:
            thread_id = update["thread_id"]
            if thread_id not in valid_thread_ids:
                logger.warning("Unknown thread_id %s; using %s", thread_id, fallback_thread_id)
                thread_id = fallback_thread_id
            for sequence, beat in enumerate(
                sorted(
                    update["beats"],
                    key=lambda item: (item["episode"], phase_sort_key(item["phase"])),
                )
            ):
                event_ids = [
                    event_id
                    for short_id in beat["event_eids"]
                    if (event_id := map_short_id(short_id, sid_to_eid)) is not None
                ]
                used_event_ids.update(event_ids)
                beat_id = generate_beat_id(chapter_id, beat["episode"], beat["phase"], sequence)
                beat_sid_to_gid[beat["beat_sid"]] = beat_id
                beats.append(
                    {
                        "id": beat_id,
                        "chapter_id": chapter_id,
                        "thread_id": thread_id,
                        "episode": beat["episode"] + chapter_start - 1,
                        "phase": beat["phase"],
                        "summary": beat["summary"],
                        "evidence_event_ids": event_ids,
                        "depends_on_beat_sids": list(beat["depends_on_beat_sids"]),
                        "requires_beat_ids": [],
                    }
                )

        for beat in beats:
            beat["requires_beat_ids"] = [
                beat_sid_to_gid[short_id]
                for short_id in beat.pop("depends_on_beat_sids", [])
                if short_id in beat_sid_to_gid
            ]

        event_positions: dict[str, int] = {
            _string(event.get("id")): index
            for index, event in enumerate(chapter_events, start=1)
            if _string(event.get("id"))
        }
        excluded = list(normalised["excluded_episodes"])
        for event in chapter_events:
            event_id = _string(event.get("id"))
            event_episode = _integer(event.get("ep"), _integer(event.get("episode"), 0))
            if not event_id or event_id in used_event_ids or event_episode in overlap_eps:
                continue
            same_episode_beats = [beat for beat in beats if beat["episode"] == event_episode]
            if same_episode_beats:
                same_episode_beats[-1]["evidence_event_ids"].append(event_id)
                continue
            relative_episode = event_episode - chapter_start + 1
            short_event_id = f"E{event_positions.get(event_id, len(chapter_events))}"
            existing = next(
                (item for item in excluded if item["episode"] == relative_episode), None
            )
            if existing is None:
                excluded.append(
                    {
                        "episode": relative_episode,
                        "event_eids": [short_event_id],
                        "reason_type": "water_content",
                        "explanation": "未分配到节拍自动标记为水内容",
                    }
                )
            else:
                existing["event_eids"].append(short_event_id)

        excluded_final: list[ProcessedExcludedEpisode] = []
        for item in excluded:
            episode = chapter_start + item["episode"] - 1
            if episode in overlap_eps:
                continue
            excluded_final.append(
                {
                    "episode": episode,
                    "event_ids": [
                        event_id
                        for short_id in item["event_eids"]
                        if (event_id := map_short_id(short_id, sid_to_eid)) is not None
                    ],
                    "reason_type": item["reason_type"],
                    "explanation": item["explanation"],
                }
            )

        result = _pass1_as_json(normalised)
        result["beats"] = [_processed_beat_as_json(beat) for beat in beats]
        result["excluded_episodes"] = [_json_object(dict(item)) for item in excluded_final]
        return result, sid_to_eid

    async def _run_pass2(
        self, chapter: Chapter, chapter_events: list[EventCard], pass1_result: JSONObject
    ) -> JSONObject:
        """运行Pass2实体提取"""
        # 构建角色prompt
        global_chars_prompt = self._build_global_chars_prompt()
        rolling_prompt = self._build_rolling_chars_prompt()
        # 构建节拍DSL
        beats_dsl_lines: list[str] = []
        for b in _object_list(pass1_result.get("beats")):
            beats_dsl_lines.append(
                f"[{_string(b.get('thread_id'), 'unknown')}][{_string(b.get('phase'))}] "
                f"第{_integer(b.get('episode'))}集：{_string(b.get('summary'))}"
            )
        beats_dsl = "\n".join(beats_dsl_lines)

        # 动态填充占位符，彻底解决KeyError
        pass2_placeholders = set(re.findall(r"\{(\w+)\}", LAYER2_PASS2_ENTITY_PROMPT))
        pass2_format_args: dict[str, str | int] = {}
        for ph in pass2_placeholders:
            if ph == "chapter_start_ep":
                pass2_format_args[ph] = _integer(chapter.get("start_ep"))
            elif ph == "chapter_end_ep":
                pass2_format_args[ph] = _integer(chapter.get("end_ep"))
            elif ph == "beats_dsl":
                pass2_format_args[ph] = beats_dsl
            elif ph == "rolling_chars_prompt":
                pass2_format_args[ph] = rolling_prompt
            elif ph == "global_characters_prompt":
                pass2_format_args[ph] = global_chars_prompt
            elif ph == "rolling_context_prompt":
                pass2_format_args[ph] = rolling_prompt
            elif ph == "chapter_beats_dsl":
                pass2_format_args[ph] = beats_dsl
            else:
                pass2_format_args[ph] = ""
        prompt = LAYER2_PASS2_ENTITY_PROMPT.format(**pass2_format_args)
        # 清理残留占位符
        prompt = re.sub(r"___PLACEHOLDER___", "", prompt)
        prompt = re.sub(r"<!-- [A-Z_]+ -->", "", prompt)
        # 保存pass2_prompt.md
        chapter_id = _string(chapter.get("chapter_id"), "unknown")
        ch_debug_dir = self.debug_dir / chapter_id if self.debug_dir else None
        if ch_debug_dir:
            (ch_debug_dir / "pass2_prompt.md").write_text(prompt, encoding="utf-8")
        elif self.debug_callback:
            self.debug_callback(f"layer2_{chapter_id}_pass2_prompt.md", prompt)

        llm_result = await self.llm_provider(prompt, response_format={"type": "json_object"})

        # 保存pass2_raw.json
        if ch_debug_dir:
            (ch_debug_dir / "pass2_raw.json").write_text(llm_result, encoding="utf-8")
        elif self.debug_callback:
            self.debug_callback(f"layer2_{chapter_id}_pass2_raw.json", llm_result)

        parsed = _robust_parse_json(llm_result)
        if parsed is not None:
            return _pass2_as_json(_normalise_pass2(parsed))
        logger.warning("Pass2 parse failed for %s; using explicit empty fallback", chapter_id)
        return _pass2_as_json(_normalise_pass2({}))

    def _validate_pass2(
        self, pass2_out: JSONObject, chapter_events: list[EventCard], sid_to_eid: dict[str, str]
    ) -> JSONObject:
        """Normalize pass-two entities without mutating provider JSON or checkpoints."""
        normalised = _normalise_pass2(pass2_out)
        characters: list[JSONObject] = []
        for character in normalised["character_rollup"]:
            character_key = character["character_key"]
            if not character_key.startswith("char-"):
                character_key = generate_char_id(character["name"])
            evidence_ids = [
                event_id
                for short_id in character["evidence_eids"]
                if (event_id := map_short_id(short_id, sid_to_eid)) is not None
            ]
            characters.append(
                _json_object(
                    {
                        "character_key": character_key,
                        "name": character["name"],
                        "aliases": list(character["aliases"]),
                        "state_at_start": character["state_at_start"],
                        "state_at_end": character["state_at_end"],
                        "importance_tier": character["importance_tier"],
                        "evidence_event_ids": evidence_ids,
                    }
                )
            )

        relationships: list[JSONObject] = []
        for relationship in normalised["relationship_rollup"]:
            relationship_key = relationship["relationship_key"]
            if not relationship_key.startswith("rel-"):
                relationship_key = generate_rel_id(
                    relationship["character_key_a"], relationship["character_key_b"]
                )
            evidence_ids = [
                event_id
                for short_id in relationship["evidence_eids"]
                if (event_id := map_short_id(short_id, sid_to_eid)) is not None
            ]
            relationships.append(
                _json_object(
                    {
                        "relationship_key": relationship_key,
                        "character_key_a": relationship["character_key_a"],
                        "character_key_b": relationship["character_key_b"],
                        "summary": relationship["summary"],
                        "importance": relationship["importance"],
                        "evidence_event_ids": evidence_ids,
                    }
                )
            )

        return _json_object(
            {
                "character_rollup": characters,
                "relationship_rollup": relationships,
                "new_character_reasoning": normalised["new_character_reasoning"],
            }
        )

    def _get_overlap_eps(self, ch_index: int) -> list[int]:
        """获取当前章与前一章的重叠集数（全剧集数）"""
        if ch_index == 0:
            return []
        chapters = self.global_framework.get("chapters", [])
        if ch_index >= len(chapters):
            return []
        prev_ch = chapters[ch_index - 1]
        curr_ch = chapters[ch_index]
        overlap_start = _integer(curr_ch.get("start_ep"))
        overlap_end = min(_integer(prev_ch.get("end_ep")), _integer(curr_ch.get("end_ep")))
        if overlap_start > overlap_end:
            return []
        return list(range(overlap_start, overlap_end + 1))

    def _build_threads_prompt(self, ch_index: int) -> str:
        threads: list[JSONObject] = []
        for thread in self.global_framework.get("global_story_threads", []):
            coverage = thread.get("chapter_coverage", [])
            if isinstance(coverage, list) and (ch_index + 1) in [
                item for item in coverage if isinstance(item, int)
            ]:
                threads.append(thread)
        lines: list[str] = []
        for t in threads:
            lines.append(
                f"- {_string(t.get('thread_id'))}: {_string(t.get('name'))}"
                f"（{_string(t.get('importance_tier'))}）- {_string(t.get('description'))}"
            )
        return "\n".join(lines) if lines else "- thread-main: 主线"

    def _build_global_chars_prompt(self) -> str:
        lines: list[str] = []
        for c in self.global_framework.get("global_characters") or self.global_framework.get(
            "global_character_preview", []
        ):
            aliases = "、".join(_string_list(c.get("aliases")))
            cid = _string(c.get("char_id"))
            cname = _string(c.get("name"), _string(c.get("canonical_name")))
            lines.append(f"- {cid}: {cname}（别名：{aliases}）")
        return "\n".join(lines) if lines else "无"

    def _build_rolling_chars_prompt(self) -> str:
        lines: list[str] = []
        for c in self.rolling_chars:
            aliases = "、".join(c["aliases"])
            cid = c["character_key"] or generate_char_id(c["name"])
            fresh_mark = " (本章活跃)" if c["last_seen_chapter"] == self.current_chapter_idx else ""
            lines.append(f"- {cid}: {c['name']}（别名：{aliases}）{fresh_mark}")
        return "\n".join(lines)


def _json_value(value: object) -> tuple[bool, JSONValue]:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True, value
    if isinstance(value, list):
        sequence = cast(Sequence[object], value)
        converted_list: list[JSONValue] = []
        for item in sequence:
            accepted, json_item = _json_value(item)
            if not accepted:
                return False, None
            converted_list.append(json_item)
        return True, converted_list
    if isinstance(value, dict):
        mapping = cast(Mapping[object, object], value)
        converted_object: JSONObject = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                return False, None
            accepted, json_item = _json_value(item)
            if not accepted:
                return False, None
            converted_object[key] = json_item
        return True, converted_object
    return False, None


def _decode_object(text: str) -> JSONObject | None:
    try:
        decoded: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    accepted, converted = _json_value(decoded)
    return converted if accepted and isinstance(converted, dict) else None


def _robust_parse_json(text: str) -> JSONObject | None:
    """Decode complete JSON only; repair of semantic truncation is forbidden.

    Markdown fences and a trailing comma are presentation defects.  Inventing
    closing brackets for a truncated LLM response would instead create an
    unobservable partial semantic result, so it is deliberately rejected.
    """
    import re as _re

    text = text.strip()

    # 1. 去除 markdown code fence
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1 if lines[0].startswith("```") else 0
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[start:end]).strip()

    # 2. 直接解析
    if result := _decode_object(text):
        return result

    # 3. 去除尾部逗号
    cleaned = _re.sub(r",\s*([}\]])", r"\1", text)
    if result := _decode_object(cleaned):
        return result

    # 4. 提取第一个 {...} 块
    brace_match = _re.search(r"\{.*\}", text, _re.DOTALL)
    if brace_match:
        extracted = _re.sub(r",\s*([}\]])", r"\1", brace_match.group())
        if result := _decode_object(extracted):
            return result

    return None
