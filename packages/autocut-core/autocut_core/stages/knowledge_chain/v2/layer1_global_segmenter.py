"""Layer 1: build a validated legacy global framework from an LLM response."""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import TypedDict, cast

from .prompts import LAYER1_SEGMENTER_PROMPT
from .schemas import KnowledgeChainV2ExtraConfig
from .types import (
    Chapter,
    EpisodeSummary,
    GlobalFramework,
    JSONObject,
    JSONValue,
    LLMProvider,
    decode_json_object,
    json_object_list,
)

logger = logging.getLogger(__name__)


class VLMMetadata(TypedDict):
    characters: dict[str, dict[str, object]]
    flashback_eps: list[int]
    time_jumps: dict[int, str]
    highlight_candidates: dict[int, list[str]]
    scene_changes: int


class GlobalSegmenter:
    """Validate the external JSON boundary before exposing a framework."""

    def __init__(
        self,
        llm_provider: LLMProvider,
        extra_config: KnowledgeChainV2ExtraConfig | None = None,
    ) -> None:
        self.llm_provider = llm_provider
        self.extra_config = extra_config or KnowledgeChainV2ExtraConfig()
        self.ep2ch: dict[int, str] = {}
        self.global_framework: GlobalFramework = {}
        self.last_prompt = ""

    async def run(self, episode_summaries: list[EpisodeSummary]) -> GlobalFramework:
        """Run the provider once and never pass raw provider JSON downstream."""
        logger.info("Running Layer1 for %s episodes", len(episode_summaries))
        summaries_text = "\n".join(
            f"第{episode['ep']}集：{episode['summary']}" for episode in episode_summaries
        )
        self.last_prompt = self._build_prompt(
            summaries_text, len(episode_summaries), episode_summaries
        )
        try:
            response = await self.llm_provider(
                self.last_prompt, response_format={"type": "json_object"}
            )
            decoded: object = json.loads(response)
            raw_framework = decode_json_object(decoded)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Layer1 response rejected; using fallback: %s", exc)
            raw_framework = None

        self.global_framework = self._normalise_framework(raw_framework, len(episode_summaries))
        self._build_ep2ch_map()
        return self.global_framework

    def _build_prompt(
        self,
        summaries_text: str,
        total_episodes: int,
        episode_summaries: list[EpisodeSummary],
    ) -> str:
        prompt = LAYER1_SEGMENTER_PROMPT.replace("{{ episode_summaries }}", summaries_text)
        prompt = prompt.replace("{{ total_episodes }}", str(total_episodes))
        themes_requirement = ""
        if self.extra_config.enable_themes:
            themes_requirement = '\n额外要求：\n- 增加"themes"字段，输出3-7个全剧核心主题，包含名称和1句话内涵，按重要性排序'
        prompt = prompt.replace("<!-- THEMES_REQUIREMENT -->", themes_requirement)
        prompt = re.sub(r"<!-- [A-Z_]+ -->", "", prompt)
        supplement = self._render_vlm_supplement(self._extract_vlm_metadata(episode_summaries))
        insert_at = prompt.find("## 剧集摘要")
        return (
            f"{prompt[:insert_at]}{supplement}\n{prompt[insert_at:]}"
            if supplement and insert_at >= 0
            else prompt
        )

    @staticmethod
    def _fallback_framework(total_episodes: int) -> GlobalFramework:
        total = max(total_episodes, 1)
        return {
            "series_title": "未命名剧集",
            "chapters": [
                {
                    "start_ep": 1,
                    "end_ep": total,
                    "title": "全剧",
                    "arc_type": "default",
                    "core_conflict": "",
                    "climax_episode": max(total // 2, 1),
                    "boundary_reason": "fallback: invalid Layer1 LLM response",
                }
            ],
            "global_story_threads": [],
            "global_characters": [],
            "global_world_rules": [],
            "global_key_props": [],
            "global_foreshadow_map": [],
            "global_turning_points": [],
            "highlight_seed_points": [],
            "global_timeline_markers": [],
            "tension_curve": [{"ep": 1, "tension": 5, "keywords": []}],
            "themes": [],
        }

    def _normalise_framework(self, raw: JSONObject | None, total_episodes: int) -> GlobalFramework:
        if raw is None:
            return self._fallback_framework(total_episodes)
        aliases = {
            "t": "series_title",
            "th": "themes",
            "ch": "chapters",
            "st": "global_story_threads",
            "c": "global_characters",
            "wr": "global_world_rules",
            "kp": "global_key_props",
            "fm": "global_foreshadow_map",
            "tp": "global_turning_points",
            "hs": "highlight_seed_points",
            "tm": "global_timeline_markers",
            "tc": "tension_curve",
        }
        mapped = self._rename_fields(raw, aliases)
        chapters = [
            chapter
            for item in json_object_list(mapped.get("chapters"))
            if (chapter := self._normalise_chapter(item)) is not None
        ]
        if not chapters:
            logger.warning("Layer1 framework has no valid chapters; using fallback")
            return self._fallback_framework(total_episodes)
        if chapters[-1]["end_ep"] < total_episodes:
            chapters[-1]["end_ep"] = total_episodes
        framework = self._fallback_framework(total_episodes)
        framework["series_title"] = self._as_string(mapped.get("series_title"), "未命名剧集")
        framework["chapters"] = chapters
        framework["global_story_threads"] = [
            self._normalise_story_thread(item)
            for item in json_object_list(mapped.get("global_story_threads"))
        ]
        chars = mapped.get("global_characters") or mapped.get("global_character_preview")
        framework["global_characters"] = [
            self._normalise_character(item) for item in json_object_list(chars)
        ]
        for name in (
            "global_world_rules",
            "global_key_props",
            "global_foreshadow_map",
            "global_turning_points",
            "highlight_seed_points",
            "global_timeline_markers",
            "world_rules",
            "foreshadow_payoff_pairs",
        ):
            self._set_object_list(framework, name, mapped.get(name))
        framework["tension_curve"] = self._normalise_tension_curve(mapped.get("tension_curve"))
        framework["themes"] = (
            self._normalise_themes(mapped.get("themes")) if self.extra_config.enable_themes else []
        )
        return framework

    @staticmethod
    def _set_object_list(framework: GlobalFramework, name: str, value: JSONValue | None) -> None:
        # ``GlobalFramework`` is deliberately a closed set; dispatch avoids a
        # dynamic key escaping this validated boundary.
        objects = json_object_list(value)
        if name == "global_world_rules":
            framework["global_world_rules"] = objects
        elif name == "global_key_props":
            framework["global_key_props"] = objects
        elif name == "global_foreshadow_map":
            framework["global_foreshadow_map"] = objects
        elif name == "global_turning_points":
            framework["global_turning_points"] = objects
        elif name == "highlight_seed_points":
            framework["highlight_seed_points"] = objects
        elif name == "global_timeline_markers":
            framework["global_timeline_markers"] = objects
        elif name == "world_rules":
            framework["world_rules"] = objects
        elif name == "foreshadow_payoff_pairs":
            framework["foreshadow_payoff_pairs"] = objects

    @staticmethod
    def _normalise_chapter(value: JSONObject) -> Chapter | None:
        renamed = GlobalSegmenter._rename_fields(
            value,
            {
                "s": "start_ep",
                "e": "end_ep",
                "n": "title",
                "a": "arc_type",
                "cc": "core_conflict",
                "clx": "climax_episode",
                "kc": "key_characters",
                "rb": "required_beats",
            },
        )
        start, end = renamed.get("start_ep"), renamed.get("end_ep")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 1
            or end < start
        ):
            return None
        return {
            "start_ep": start,
            "end_ep": end,
            "title": GlobalSegmenter._as_string(renamed.get("title"), "未命名章节"),
            "arc_type": GlobalSegmenter._as_string(renamed.get("arc_type"), "default"),
            "core_conflict": GlobalSegmenter._as_string(renamed.get("core_conflict"), ""),
            "climax_episode": GlobalSegmenter._as_int(renamed.get("climax_episode"), start),
            "boundary_reason": GlobalSegmenter._as_string(renamed.get("boundary_reason"), ""),
            "key_characters": GlobalSegmenter._string_list(renamed.get("key_characters")),
            "required_beats": GlobalSegmenter._string_list(renamed.get("required_beats")),
        }

    @staticmethod
    def _rename_fields(value: JSONObject, aliases: dict[str, str]) -> JSONObject:
        return {aliases.get(key, key): item for key, item in value.items()}

    @staticmethod
    def _story_aliases() -> dict[str, str]:
        return {
            "id": "thread_id",
            "n": "name",
            "d": "description",
            "ca": "core_arc",
            "s": "start_ep",
            "e": "end_ep",
            "kn": "key_nodes",
            "it": "importance_tier",
            "i": "importance",
        }

    @staticmethod
    def _character_aliases() -> dict[str, str]:
        return {
            "id": "char_id",
            "n": "canonical_name",
            "al": "aliases",
            "idn": "identity",
            "f": "faction",
            "fs": "first_ep",
            "le": "last_ep",
            "cr": "core_relations",
            "it": "importance_tier",
            "i": "importance",
            "a": "arc_summary",
        }

    @classmethod
    def _normalise_story_thread(cls, value: JSONObject) -> JSONObject:
        thread = cls._rename_fields(value, cls._story_aliases())
        importance_tier = thread.get("importance_tier")
        if isinstance(importance_tier, str):
            thread["importance_tier"] = {
                "P": "primary",
                "S": "secondary",
                "T": "tertiary",
            }.get(importance_tier, importance_tier)
        if "chapter_coverage" not in thread:
            thread["chapter_coverage"] = []
        return thread

    @classmethod
    def _normalise_character(cls, value: JSONObject) -> JSONObject:
        character = cls._rename_fields(value, cls._character_aliases())
        relations: list[JSONValue] = []
        for relation in json_object_list(character.get("core_relations")):
            normalized = cls._rename_fields(relation, {"r": "rel_type", "t": "target_char_id"})
            relation_type = normalized.get("rel_type")
            if isinstance(relation_type, str):
                normalized["rel_type"] = {
                    "父": "father",
                    "母": "mother",
                    "女": "daughter",
                    "妻": "wife",
                    "敌": "enemy",
                    "友": "ally",
                    "爱": "lover",
                }.get(relation_type, relation_type)
            relations.append(normalized)
        character["core_relations"] = relations
        importance_tier = character.get("importance_tier")
        if isinstance(importance_tier, str):
            character["importance_tier"] = {
                "P": "protagonist",
                "S": "supporting",
                "M": "minor",
            }.get(importance_tier, importance_tier)
        if "chapter_coverage" not in character:
            character["chapter_coverage"] = []
        return character

    @staticmethod
    def _normalise_tension_curve(value: JSONValue | None) -> list[JSONObject]:
        if not isinstance(value, list):
            return []
        if all(isinstance(point, int) and not isinstance(point, bool) for point in value):
            return [
                {"ep": index + 1, "tension": point, "keywords": []}
                for index, point in enumerate(value)
            ]
        return json_object_list(value)

    @staticmethod
    def _normalise_themes(value: JSONValue | None) -> list[JSONObject]:
        if not isinstance(value, list):
            return []
        themes: list[JSONObject] = []
        for index, theme in enumerate(value[:7]):
            if isinstance(theme, str) and theme.strip():
                themes.append(
                    {"name": theme.strip(), "weight": max(0.3, 1.0 - index * 0.1), "first_ep": 1}
                )
            elif isinstance(theme, dict):
                name = theme.get("n", theme.get("name", ""))
                if isinstance(name, str) and name.strip():
                    themes.append(
                        {
                            "name": name.strip(),
                            "description": GlobalSegmenter._as_string(
                                theme.get("d", theme.get("description")), ""
                            ),
                            "weight": max(0.3, 1.0 - index * 0.1),
                            "first_ep": 1,
                        }
                    )
        return themes

    def _extract_vlm_metadata(self, episodes: list[EpisodeSummary]) -> VLMMetadata:
        characters: dict[str, dict[str, object]] = {}
        flashbacks: set[int] = set()
        jumps: dict[int, str] = {}
        highlights: dict[int, list[str]] = defaultdict(list)
        scenes = 0
        for episode in episodes:
            source: JSONObject = episode.get("raw_vlm_data") or cast(JSONObject, episode)
            for segment in json_object_list(source.get("timeline_segments")):
                mode, summary = (
                    self._as_string(segment.get("mode"), "present"),
                    self._as_string(segment.get("summary"), ""),
                )
                if mode in {"flashback", "recall"}:
                    flashbacks.add(episode["ep"])
                if mode == "time_jump" or "年后" in summary or "跳转" in summary:
                    jumps[episode["ep"]] = summary or "时间跳跃"
            for beat in json_object_list(source.get("story_beats")):
                if any(
                    marker in self._as_string(beat.get("function"), "")
                    for marker in ("高潮", "转折", "冲突", "爆发", "名场面")
                ):
                    highlights[episode["ep"]].append(self._as_string(beat.get("summary"), "")[:100])
            locations = source.get("scene_locations")
            scenes += len(locations) if isinstance(locations, list) else 0
        return {
            "characters": characters,
            "flashback_eps": sorted(flashbacks),
            "time_jumps": jumps,
            "highlight_candidates": dict(highlights),
            "scene_changes": scenes,
        }

    @staticmethod
    def _render_vlm_supplement(metadata: VLMMetadata) -> str:
        lines: list[str] = []
        if metadata["flashback_eps"]:
            lines.append(f"- 含闪回/回忆的集数：{metadata['flashback_eps']}")
        lines.extend(
            f"- 第{episode}集：{summary}" for episode, summary in metadata["time_jumps"].items()
        )
        for episode, beats in metadata["highlight_candidates"].items():
            lines.extend(f"- 第{episode}集：{beat}" for beat in beats[:3])
        if metadata["scene_changes"]:
            lines.append(f"- 视觉场景切换提示：共识别到{metadata['scene_changes']}个主要场景切换")
        return (
            "\n## VLM视觉识别补充信息（请结合文本摘要参考）\n" + "\n".join(lines) if lines else ""
        )

    def _build_ep2ch_map(self) -> None:
        self.ep2ch = {}
        for index, chapter in enumerate(self.global_framework.get("chapters", [])):
            chapter_id = f"ch{index + 1:02d}-{chapter['start_ep']}-{chapter['end_ep']}"
            chapter["chapter_id"] = chapter_id
            for episode in range(chapter["start_ep"], chapter["end_ep"] + 1):
                self.ep2ch[episode] = chapter_id

    def get_chapter_for_ep(self, episode: int) -> str:
        return self.ep2ch.get(episode, "unknown")

    @staticmethod
    def _as_string(value: JSONValue | None, default: str) -> str:
        return value if isinstance(value, str) else default

    @staticmethod
    def _as_int(value: JSONValue | None, default: int) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    @staticmethod
    def _string_list(value: JSONValue | None) -> list[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
