"""Layer 3: deterministic, zero-LLM assembly for the legacy V2 pipeline."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import cast

from .schemas import KnowledgeChainV2Output
from .types import ChapterOutput, EventCard, GlobalFramework, JSONObject, JSONValue
from .utils import exact_name_match, generate_fact_id, generate_question_id, phase_sort_key

logger = logging.getLogger(__name__)


def _json_value(value: object) -> JSONValue:
    """Return an owned JSON copy instead of retaining caller-owned dictionaries."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in cast(list[object], value)]
    if isinstance(value, Mapping):
        result: JSONObject = {}
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise TypeError(f"JSON key must be str, got {type(key).__name__}")
            result[key] = _json_value(item)
        return result
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _object(value: object, label: str) -> JSONObject:
    copied = _json_value(value)
    if not isinstance(copied, dict):
        raise TypeError(f"{label} must be a JSON object")
    return copied


def _objects(value: JSONValue | None, label: str) -> list[JSONObject]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    result: list[JSONObject] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise TypeError(f"{label}[{index}] must be an object")
        result.append(item)
    return result


def _values(value: JSONValue | None, label: str) -> list[JSONValue]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    return value


def _str(value: JSONValue | None, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _req_str(obj: JSONObject, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _int(value: JSONValue | None, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _req_int(obj: JSONObject, key: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _float(value: JSONValue | None, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _bool(value: JSONValue | None, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _strings(value: JSONValue | None, label: str) -> list[str]:
    values = _values(value, label)
    if not all(isinstance(item, str) for item in values):
        raise TypeError(f"{label} must contain only strings")
    return cast(list[str], values)


def _chapter(output: JSONObject) -> JSONObject:
    value = output.get("chapter")
    if not isinstance(value, dict):
        raise ValueError("chapter output is missing chapter")
    return value


def _content(value: JSONValue) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        content = value.get("content")
        return content if isinstance(content, str) else str(value)
    return str(value)


def _deduplicate_overlap_events(
    outputs: list[JSONObject],
) -> tuple[list[JSONObject], list[str]]:
    """Apply the old overlap rule to the assembler's private input copy."""
    warnings: list[str] = []
    for index in range(len(outputs) - 1):
        previous_output = outputs[index]
        next_output = outputs[index + 1]
        previous = _chapter(previous_output)
        following = _chapter(next_output)
        overlap_start = _req_int(following, "start_ep")
        overlap_end = min(_req_int(previous, "end_ep"), _req_int(following, "end_ep"))
        if overlap_start > overlap_end:
            continue
        overlap = set(range(overlap_start, overlap_end + 1))
        if len(overlap) > 1:
            warnings.append(
                "[OVERLAP_TOO_MANY_EPS] "
                f"章节{_req_str(previous, 'chapter_id')}和"
                f"{_req_str(following, 'chapter_id')}重叠{len(overlap)}集，超过1集的建议阈值"
            )
        overlap_event_ids = set(
            _strings(previous_output.get("_overlap_event_ids"), "_overlap_event_ids")
        )
        kept: list[JSONObject] = []
        removed = 0
        for beat in _objects(previous_output.get("beats"), "beats"):
            if _req_int(beat, "episode") in overlap:
                removed += 1
                continue
            beat["evidence_event_ids"] = [
                event_id
                for event_id in _strings(beat.get("evidence_event_ids"), "beat.evidence_event_ids")
                if event_id not in overlap_event_ids
            ]
            kept.append(beat)
        previous_output["beats"] = _json_value(kept)
        previous_output["excluded_episodes"] = _json_value(
            [
                excluded
                for excluded in _objects(
                    previous_output.get("excluded_episodes"), "excluded_episodes"
                )
                if _req_int(excluded, "episode") not in overlap
            ]
        )
        if removed:
            logger.info(
                "Removed %d beats from %s overlap eps %s, kept in %s",
                removed,
                _req_str(previous, "chapter_id"),
                sorted(overlap),
                _req_str(following, "chapter_id"),
            )
    return outputs, warnings


class GlobalAssembler:
    """Assemble typed layer outputs while keeping all caller inputs immutable."""

    def __init__(
        self,
        global_framework: GlobalFramework,
        chapter_outputs: Sequence[ChapterOutput],
        event_cards: Sequence[EventCard],
    ) -> None:
        self.global_framework = _object(global_framework, "global_framework")
        self.chapter_outputs = [_object(item, "chapter_output") for item in chapter_outputs]
        events = [_object(item, "event_card") for item in event_cards]
        self.event_cards = {_req_str(event, "id"): event for event in events}
        self.warnings: list[str] = []
        self.id_mapping: dict[str, str] = {}

    def assemble(self) -> KnowledgeChainV2Output:
        """Build the blueprint and validate the complete result with Pydantic."""
        logger.info("Running Layer3 Global Assembly (zero-LLM)...")
        self.warnings = []
        self.id_mapping = {}
        self.chapter_outputs, overlap_warnings = _deduplicate_overlap_events(self.chapter_outputs)
        self.warnings.extend(overlap_warnings)

        threads = self._init_threads()
        characters = self._init_characters()
        turning_points = _objects(
            self.global_framework.get("global_turning_points"), "global_turning_points"
        )
        tension_curve = _objects(self.global_framework.get("tension_curve"), "tension_curve")
        beats: list[JSONObject] = []
        relationships: list[JSONObject] = []
        facts: list[JSONObject] = []
        open_questions: list[JSONObject] = []
        resolved_questions: list[JSONObject] = []
        excluded_episodes: list[JSONObject] = []
        world_rules: list[JSONObject] = []
        foreshadow_pairs: list[JSONObject] = []
        state_changes: defaultdict[str, list[JSONObject]] = defaultdict(list)
        character_aliases: dict[str, str] = {}

        for chapter_output in self.chapter_outputs:
            chapter = _chapter(chapter_output)
            start_ep = _req_int(chapter, "start_ep")
            end_ep = _req_int(chapter, "end_ep")
            beats.extend(_objects(chapter_output.get("beats"), "beats"))
            excluded_episodes.extend(
                _objects(chapter_output.get("excluded_episodes"), "excluded_episodes")
            )
            self._collect_characters(
                chapter_output, characters, character_aliases, state_changes, start_ep, end_ep
            )
            self._collect_relationships(
                chapter_output, characters, relationships, character_aliases
            )
            for fact in _values(chapter_output.get("new_facts"), "new_facts"):
                facts.append(
                    {
                        "id": generate_fact_id(len(facts) + 1),
                        "content": _content(fact),
                        "episode": start_ep,
                        "evidence_event_ids": [],
                    }
                )
            for question in _values(chapter_output.get("new_open_questions"), "new_open_questions"):
                open_questions.append(
                    {
                        "id": generate_question_id(len(open_questions) + 1),
                        "content": _content(question),
                        "first_ep": start_ep,
                    }
                )
            self._collect_resolutions(chapter_output, open_questions, resolved_questions, end_ep)
            self._collect_world_rules(chapter_output, world_rules, start_ep, end_ep)
            self._collect_foreshadows(chapter_output, foreshadow_pairs, start_ep, end_ep)
            for warning in _objects(chapter_output.get("warnings"), "warnings"):
                self.warnings.append(
                    f"[{_req_str(warning, 'code')}] {_req_str(warning, 'message')}"
                )

        characters = self._strict_merge_characters(characters)
        threads = self._strict_merge_threads(threads)
        for relation in relationships:
            source = _req_str(relation, "from_char_id")
            target = _req_str(relation, "to_char_id")
            relation["from_char_id"] = self.id_mapping.get(source, source)
            relation["to_char_id"] = self.id_mapping.get(target, target)
        self._attach_beats(threads, beats)
        self._apply_importance_fallbacks(characters, threads)
        self._apply_state_changes(characters, state_changes)
        beats.sort(
            key=lambda beat: (
                _req_int(beat, "episode"),
                phase_sort_key(_req_str(beat, "phase")),
            )
        )
        tension_curve.sort(key=lambda point: -_int(point.get("tension")))
        tension_peaks = [_req_int(point, "ep") for point in tension_curve[:3]]
        chapters = _objects(self.global_framework.get("chapters"), "chapters")
        total_episodes = self._total_episodes(chapters)
        self._fill_missing_coverage(beats, excluded_episodes, total_episodes)

        themes = self._themes()
        if not world_rules:
            world_rules = _objects(self.global_framework.get("world_rules"), "world_rules")
        if not foreshadow_pairs:
            foreshadow_pairs = _objects(
                self.global_framework.get("foreshadow_payoff_pairs"),
                "foreshadow_payoff_pairs",
            )
        metadata = self._metadata(
            total_episodes,
            tension_peaks,
            threads,
            characters,
            beats,
            excluded_episodes,
            world_rules,
            foreshadow_pairs,
            open_questions,
            chapters,
        )
        output_chapters = self._complete_chapters()
        payload = _object(
            {
                "metadata": metadata,
                "chapters": output_chapters,
                "story_threads": threads,
                "characters": characters,
                "beats": beats,
                "relationships": relationships,
                "facts": facts,
                "turning_points": turning_points,
                "tension_curve": tension_curve,
                "open_questions": open_questions,
                "resolved_questions": resolved_questions,
                "excluded_episodes": excluded_episodes,
                "foreshadow_pairs": foreshadow_pairs,
                "world_rules": world_rules,
                "themes": themes,
            },
            "knowledge_chain_v2 output",
        )
        logger.info(
            "Layer3 complete: %d threads, %d chars, %d beats, %d warnings",
            len(threads),
            len(characters),
            len(beats),
            len(self.warnings),
        )
        return KnowledgeChainV2Output.model_validate(payload)

    def _collect_characters(
        self,
        chapter_output: JSONObject,
        characters: list[JSONObject],
        aliases_by_id: dict[str, str],
        state_changes: defaultdict[str, list[JSONObject]],
        start_ep: int,
        end_ep: int,
    ) -> None:
        for rollup in _objects(chapter_output.get("character_rollup"), "character_rollup"):
            key = _req_str(rollup, "character_key")
            name = _req_str(rollup, "name")
            aliases = _strings(rollup.get("aliases"), "character.aliases")
            existing = next(
                (
                    character
                    for character in characters
                    if exact_name_match(
                        name,
                        _req_str(character, "name"),
                        aliases,
                        _strings(character.get("aliases"), "character.aliases"),
                    )
                ),
                None,
            )
            state_at_end = _str(rollup.get("state_at_end"))
            evidence = _strings(rollup.get("evidence_event_ids"), "character.evidence_event_ids")
            if existing is not None:
                canonical_id = _req_str(existing, "id")
                aliases_by_id[key] = canonical_id
                if state_at_end:
                    state_changes[canonical_id].append({"ep": end_ep, "state": state_at_end})
                existing_aliases = _strings(existing.get("aliases"), "character.aliases")
                for alias in aliases:
                    if alias not in existing_aliases:
                        existing_aliases.append(alias)
                existing["aliases"] = _json_value(existing_aliases)
                existing["last_seen_ep"] = max(_req_int(existing, "last_seen_ep"), end_ep)
                existing_evidence = _strings(
                    existing.get("evidence_event_ids"), "character.evidence_event_ids"
                )
                for event_id in evidence:
                    if event_id not in existing_evidence and len(existing_evidence) < 10:
                        existing_evidence.append(event_id)
                existing["evidence_event_ids"] = _json_value(existing_evidence)
                continue
            aliases_by_id[key] = key
            milestones: list[JSONObject] = [
                {"ep": start_ep, "state": _str(rollup.get("state_at_start"), "出场")}
            ]
            if state_at_end:
                milestones.append({"ep": end_ep, "state": state_at_end})
            characters.append(
                _object(
                    {
                        "id": key,
                        "name": name,
                        "aliases": list(aliases),
                        "importance_tier": _str(rollup.get("importance_tier"), "minor"),
                        "importance": 0.2,
                        "is_core": False,
                        "first_seen_ep": start_ep,
                        "last_seen_ep": end_ep,
                        "final_state": state_at_end,
                        "state_milestones": milestones,
                        "evidence_event_ids": evidence[:10],
                    },
                    "character",
                )
            )

    def _collect_relationships(
        self,
        chapter_output: JSONObject,
        characters: list[JSONObject],
        relationships: list[JSONObject],
        aliases_by_id: dict[str, str],
    ) -> None:
        for rollup in _objects(chapter_output.get("relationship_rollup"), "relationship_rollup"):
            relationship_id = _req_str(rollup, "relationship_key")
            raw_source = _req_str(rollup, "character_key_a")
            raw_target = _req_str(rollup, "character_key_b")
            source = aliases_by_id.get(raw_source, raw_source)
            target = aliases_by_id.get(raw_target, raw_target)
            character_ids = {_req_str(character, "id") for character in characters}
            if source not in character_ids or target not in character_ids:
                self.warnings.append(
                    f"Relationship {relationship_id} has invalid character IDs "
                    f"(a={raw_source}→{source}, b={raw_target}→{target}), skipped"
                )
                continue
            summary = _req_str(rollup, "summary")
            evidence = _strings(rollup.get("evidence_event_ids"), "relationship.evidence_event_ids")
            existing = next(
                (
                    relation
                    for relation in relationships
                    if _req_str(relation, "id") == relationship_id
                ),
                None,
            )
            if existing is None:
                relationships.append(
                    {
                        "id": relationship_id,
                        "from_char_id": source,
                        "to_char_id": target,
                        "type": _str(rollup.get("type"), "其他"),
                        "summary": summary,
                        "importance": _float(rollup.get("importance"), 0.2),
                        "evidence_event_ids": list(evidence),
                    }
                )
                continue
            existing["summary"] = _str(existing.get("summary")) + "；" + summary
            existing_evidence = _strings(
                existing.get("evidence_event_ids"), "relationship.evidence_event_ids"
            )
            for event_id in evidence:
                if event_id not in existing_evidence:
                    existing_evidence.append(event_id)
            existing["evidence_event_ids"] = _json_value(existing_evidence)

    def _collect_resolutions(
        self,
        chapter_output: JSONObject,
        open_questions: list[JSONObject],
        resolved_questions: list[JSONObject],
        end_ep: int,
    ) -> None:
        for question in _values(chapter_output.get("resolved_questions"), "resolved_questions"):
            content = _content(question)
            matched = next(
                (
                    candidate
                    for candidate in open_questions
                    if _str(candidate.get("content")) == content
                ),
                None,
            )
            if matched is None:
                continue
            resolution = _str(question.get("resolution")) if isinstance(question, dict) else ""
            resolved_questions.append(
                {
                    "id": _req_str(matched, "id"),
                    "content": content,
                    "first_ep": _req_int(matched, "first_ep"),
                    "resolved_ep": end_ep,
                    "resolution": resolution,
                }
            )
            open_questions.remove(matched)

    def _collect_world_rules(
        self,
        chapter_output: JSONObject,
        world_rules: list[JSONObject],
        start_ep: int,
        end_ep: int,
    ) -> None:
        for rule in _values(chapter_output.get("new_world_rules"), "new_world_rules"):
            content = _content(rule)
            if not content or any(_str(item.get("content")) == content for item in world_rules):
                continue
            rule_type = _str(rule.get("type"), "other") if isinstance(rule, dict) else "other"
            world_rules.append(
                {
                    "id": f"wr-{len(world_rules) + 1:03d}",
                    "content": content,
                    "type": rule_type,
                    "first_ep": start_ep,
                    "mentioned_eps": [start_ep, end_ep],
                }
            )

    def _collect_foreshadows(
        self,
        chapter_output: JSONObject,
        pairs: list[JSONObject],
        start_ep: int,
        end_ep: int,
    ) -> None:
        for pair in _values(chapter_output.get("foreshadow_payoffs"), "foreshadow_payoffs"):
            if not isinstance(pair, dict):
                continue
            setup_id = _str(pair.get("setup_beat_sid"))
            description = _str(pair.get("description"))
            if setup_id and description:
                pairs.append(
                    {
                        "id": f"fs-{len(pairs) + 1:03d}",
                        "description": description,
                        "setup_ep": start_ep,
                        "setup_beat_id": setup_id,
                        "payoff_ep": end_ep,
                        "is_resolved": True,
                        "importance": _float(pair.get("importance"), 0.5),
                    }
                )

    def _init_threads(self) -> list[JSONObject]:
        characters = _objects(
            self.global_framework.get("global_characters"), "global_characters"
        ) or _objects(
            self.global_framework.get("global_character_preview"), "global_character_preview"
        )
        core_ids = [_req_str(character, "char_id") for character in characters[:2]]
        result: list[JSONObject] = []
        for thread in _objects(
            self.global_framework.get("global_story_threads"), "global_story_threads"
        ):
            name = _str(thread.get("name"), "未命名故事线") or "未命名故事线"
            description = _str(thread.get("description"), name) or name
            tier = _str(thread.get("importance_tier"), "supporting") or "supporting"
            result.append(
                _object(
                    {
                        "id": _req_str(thread, "thread_id"),
                        "name": name,
                        "description": description,
                        "summary": description,
                        "importance": _float(thread.get("importance"), 0.5) or 0.5,
                        "is_primary": _bool(thread.get("is_primary"), tier == "primary"),
                        "importance_tier": tier,
                        "chapter_coverage": _values(
                            thread.get("chapter_coverage"), "thread.chapter_coverage"
                        ),
                        "key_episodes": _values(thread.get("key_episodes"), "thread.key_episodes"),
                        "beat_ids": [],
                        "character_ids": list(core_ids),
                    },
                    "character",
                )
            )
        return result

    def _init_characters(self) -> list[JSONObject]:
        source = _objects(
            self.global_framework.get("global_characters"), "global_characters"
        ) or _objects(
            self.global_framework.get("global_character_preview"), "global_character_preview"
        )
        result: list[JSONObject] = []
        for character in source:
            importance = _float(character.get("importance"), 0.5)
            coverage = [
                _int(value)
                for value in _values(
                    character.get("chapter_coverage"), "character.chapter_coverage"
                )
            ]
            result.append(
                _object(
                    {
                        "id": _str(character.get("char_id"), _str(character.get("id"))),
                        "name": _str(character.get("name"), _str(character.get("canonical_name"))),
                        "aliases": _strings(character.get("aliases"), "character.aliases"),
                        "importance_tier": _str(character.get("importance_tier"), "supporting"),
                        "importance": importance,
                        "is_core": _bool(character.get("is_core"), importance >= 0.8),
                        "first_seen_ep": min(coverage or [_int(character.get("first_ep"), 1)]),
                        "last_seen_ep": max(coverage or [_int(character.get("last_ep"), 1)]),
                        "final_state": _str(character.get("final_state"), "状态未知") or "状态未知",
                        "state_milestones": [],
                        "evidence_event_ids": [],
                        "arc_summary": _str(character.get("arc_summary"), "角色剧情线")
                        or "角色剧情线",
                    },
                    "character",
                )
            )
        return result

    def _strict_merge_characters(self, characters: list[JSONObject]) -> list[JSONObject]:
        merged: list[JSONObject] = []
        for character in characters:
            if _bool(character.get("is_core")):
                merged.append(character)
                continue
            character_eps = set(
                range(_req_int(character, "first_seen_ep"), _req_int(character, "last_seen_ep") + 1)
            )
            target: JSONObject | None = None
            for existing in merged:
                if _bool(existing.get("is_core")) or not exact_name_match(
                    _req_str(character, "name"),
                    _req_str(existing, "name"),
                    _strings(character.get("aliases"), "character.aliases"),
                    _strings(existing.get("aliases"), "character.aliases"),
                ):
                    continue
                existing_eps = set(
                    range(
                        _req_int(existing, "first_seen_ep"), _req_int(existing, "last_seen_ep") + 1
                    )
                )
                union = character_eps | existing_eps
                if union and len(character_eps & existing_eps) / len(union) >= 0.3:
                    target = existing
                    break
            if target is None:
                merged.append(character)
                continue
            character_id = _req_str(character, "id")
            target_id = _req_str(target, "id")
            self.warnings.append(
                f"Merged duplicate minor character: {_req_str(character, 'name')} into {target_id}"
            )
            self.id_mapping[character_id] = target_id
            target["first_seen_ep"] = min(
                _req_int(target, "first_seen_ep"), _req_int(character, "first_seen_ep")
            )
            target["last_seen_ep"] = max(
                _req_int(target, "last_seen_ep"), _req_int(character, "last_seen_ep")
            )
            for key in ("aliases", "evidence_event_ids"):
                current = _strings(target.get(key), f"character.{key}")
                for item in _strings(character.get(key), f"character.{key}"):
                    if item not in current and (key != "evidence_event_ids" or len(current) < 10):
                        current.append(item)
                target[key] = _json_value(current)
            milestones = _objects(target.get("state_milestones"), "character.state_milestones")
            for item in _objects(character.get("state_milestones"), "character.state_milestones"):
                if item not in milestones:
                    milestones.append(item)
            target["state_milestones"] = _json_value(milestones)
            target["importance"] = max(
                _float(target.get("importance")), _float(character.get("importance"), 0.2)
            )
        return merged

    def _strict_merge_threads(self, threads: list[JSONObject]) -> list[JSONObject]:
        def keywords(text: str) -> set[str]:
            return {
                word for word in text.replace("，", " ").replace("。", " ").split() if len(word) > 1
            }

        merged: list[JSONObject] = []
        for thread in threads:
            if _str(thread.get("importance_tier")) != "background":
                merged.append(thread)
                continue
            thread_eps = {
                _int(value) for value in _values(thread.get("key_episodes"), "thread.key_episodes")
            }
            thread_words = keywords(_str(thread.get("summary")) + _str(thread.get("name")))
            target: JSONObject | None = None
            for existing in merged:
                if _str(existing.get("importance_tier")) != "background" or _str(
                    thread.get("name")
                ) != _str(existing.get("name")):
                    continue
                existing_eps = {
                    _int(value)
                    for value in _values(existing.get("key_episodes"), "thread.key_episodes")
                }
                episode_union = thread_eps | existing_eps
                if episode_union and len(thread_eps & existing_eps) / len(episode_union) < 0.5:
                    continue
                existing_words = keywords(
                    _str(existing.get("summary")) + _str(existing.get("name"))
                )
                word_union = thread_words | existing_words
                if word_union and len(thread_words & existing_words) / len(word_union) >= 0.3:
                    target = existing
                    break
            if target is None:
                merged.append(thread)
                continue
            thread_id = _req_str(thread, "id")
            target_id = _req_str(target, "id")
            self.warnings.append(
                f"Merged duplicate background thread: {_req_str(thread, 'name')} into {target_id}"
            )
            self.id_mapping[thread_id] = target_id
            for key in ("chapter_coverage", "key_episodes"):
                target[key] = _json_value(
                    sorted(
                        {
                            _int(value)
                            for value in [
                                *_values(target.get(key), f"thread.{key}"),
                                *_values(thread.get(key), f"thread.{key}"),
                            ]
                        }
                    )
                )
            target["description"] = (
                _str(target.get("description")) + "；" + _str(thread.get("description"))
            )
            target["summary"] = _str(target.get("summary")) + "；" + _str(thread.get("summary"))
            target["importance"] = max(
                _float(target.get("importance")), _float(thread.get("importance"), 0.2)
            )
        for output in self.chapter_outputs:
            for beat in _objects(output.get("beats"), "beats"):
                thread_id = _req_str(beat, "thread_id")
                beat["thread_id"] = self.id_mapping.get(thread_id, thread_id)
        return merged

    def _attach_beats(self, threads: list[JSONObject], beats: list[JSONObject]) -> None:
        for beat in beats:
            thread = next(
                (item for item in threads if _req_str(item, "id") == _req_str(beat, "thread_id")),
                None,
            )
            if thread is None:
                continue
            beat_ids = _strings(thread.get("beat_ids"), "thread.beat_ids")
            beat_id = _req_str(beat, "id")
            if beat_id not in beat_ids:
                beat_ids.append(beat_id)
            thread["beat_ids"] = _json_value(beat_ids)
            character_ids = _strings(thread.get("character_ids"), "thread.character_ids")
            for event_id in _strings(beat.get("evidence_event_ids"), "beat.evidence_event_ids"):
                event = self.event_cards.get(event_id)
                if event is None:
                    continue
                for character_id in _strings(event.get("character_ids"), "event.character_ids"):
                    if character_id not in character_ids:
                        character_ids.append(character_id)
            thread["character_ids"] = _json_value(character_ids)

    def _apply_importance_fallbacks(
        self, characters: list[JSONObject], threads: list[JSONObject]
    ) -> None:
        for character in characters:
            importance = _float(character.get("importance"))
            if _bool(character.get("is_core")) and importance < 0.7:
                self.warnings.append(
                    f"Core character {_req_str(character, 'name')} importance too low "
                    f"({importance}), fallback to 0.8"
                )
                character["importance"] = 0.8
            if (
                not _bool(character.get("is_core"))
                and _req_int(character, "last_seen_ep") - _req_int(character, "first_seen_ep") <= 3
                and importance > 0.8
            ):
                self.warnings.append(
                    f"Minor character {_req_str(character, 'name')} importance too high "
                    f"({importance}), fallback to 0.3"
                )
                character["importance"] = 0.3
        for thread in threads:
            importance = _float(thread.get("importance"))
            if _bool(thread.get("is_primary")) and importance < 0.8:
                self.warnings.append(
                    f"Primary thread {_req_str(thread, 'name')} importance too low "
                    f"({importance}), fallback to 0.8"
                )
                thread["importance"] = 0.8

    def _apply_state_changes(
        self,
        characters: list[JSONObject],
        state_changes: defaultdict[str, list[JSONObject]],
    ) -> None:
        for character in characters:
            milestones = [
                *_objects(character.get("state_milestones"), "character.state_milestones"),
                *state_changes.get(_req_str(character, "id"), []),
            ]
            unique: list[JSONObject] = []
            seen: set[int] = set()
            for milestone in milestones:
                episode = _int(milestone.get("ep"))
                if episode not in seen:
                    unique.append(milestone)
                    seen.add(episode)
            unique.sort(key=lambda item: _int(item.get("ep")))
            character["state_milestones"] = _json_value(unique[:5])
            if unique:
                character["final_state"] = _str(unique[-1].get("state"))
            character.setdefault("arc_summary", "")

    def _total_episodes(self, chapters: list[JSONObject]) -> int:
        if chapters:
            return _req_int(chapters[-1], "end_ep")
        return max(
            (
                _int(event.get("ep"), _int(event.get("episode")))
                for event in self.event_cards.values()
            ),
            default=12,
        )

    def _fill_missing_coverage(
        self,
        beats: list[JSONObject],
        excluded: list[JSONObject],
        total_episodes: int,
    ) -> None:
        covered = {_req_int(beat, "episode") for beat in beats} | {
            _req_int(item, "episode") for item in excluded
        }
        missing = set(range(1, total_episodes + 1)) - covered
        if not missing:
            return
        warning = f"Missing coverage for episodes: {sorted(missing)}, auto-marked as water content"
        logger.warning(warning)
        self.warnings.append(warning)
        for episode in sorted(missing):
            excluded.append(
                {
                    "episode": episode,
                    "event_ids": [],
                    "reason_type": "water_content",
                    "explanation": "LLM漏处理自动标记",
                }
            )

    def _themes(self) -> list[JSONObject]:
        themes: list[JSONObject] = []
        for index, theme in enumerate(_values(self.global_framework.get("themes"), "themes")):
            if isinstance(theme, str):
                themes.append({"name": theme, "weight": max(0.3, 1.0 - index * 0.1), "first_ep": 1})
            elif isinstance(theme, dict):
                themes.append(theme)
        if not themes:
            self.warnings.append("Themes字段为空，已降级为空列表")
        return themes

    def _metadata(
        self,
        total_episodes: int,
        tension_peaks: list[int],
        threads: list[JSONObject],
        characters: list[JSONObject],
        beats: list[JSONObject],
        excluded: list[JSONObject],
        world_rules: list[JSONObject],
        foreshadows: list[JSONObject],
        questions: list[JSONObject],
        chapters: list[JSONObject],
    ) -> JSONObject:
        duplicate_count = sum("Merged duplicate" in warning for warning in self.warnings)
        background_count = sum(
            _str(thread.get("importance_tier")) == "background" for thread in threads
        )
        overlap_count = sum(
            len(_values(output.get("overlap_eps"), "overlap_eps"))
            for output in self.chapter_outputs
        )
        unassigned_count = sum(
            len(_strings(item.get("event_ids"), "excluded.event_ids"))
            for item in excluded
            if _str(item.get("reason_type")) == "water_content"
        )
        metrics: JSONObject = {
            "total_episodes": total_episodes,
            "total_chapters": len(chapters),
            "total_llm_calls": 1 + len(self.chapter_outputs) * 2,
            "id_error_rate": duplicate_count / max(1, len(characters) + len(threads)),
            "fragmentation_rate": background_count / max(1, len(threads)),
            "warning_count": len(self.warnings),
            "overlap_ep_count": overlap_count,
            "unassigned_event_count": unassigned_count,
            "retry_count": sum("retrying" in warning.lower() for warning in self.warnings),
            "beat_count": len(beats),
            "character_count": len(characters),
            "thread_count": len(threads),
            "world_rule_count": len(world_rules),
            "foreshadow_count": len(foreshadows),
            "open_question_count": len(questions),
            "warnings": list(self.warnings),
        }
        return _object(
            {
                "series_title": _str(self.global_framework.get("series_title"), "未命名剧集")
                or "未命名剧集",
                "total_episodes": total_episodes,
                "tension_peaks": tension_peaks,
                "core_character_count": sum(
                    _bool(character.get("is_core")) for character in characters
                )
                or len(characters),
                "primary_thread_count": sum(_bool(thread.get("is_primary")) for thread in threads)
                or len(threads),
                "warnings": list(self.warnings),
                "metrics": metrics,
            },
            "metadata",
        )

    def _complete_chapters(self) -> list[JSONObject]:
        chapters: list[JSONObject] = []
        for output in self.chapter_outputs:
            chapter = _object(_chapter(output), "chapter")
            start_ep = _req_int(chapter, "start_ep")
            end_ep = _req_int(chapter, "end_ep")
            chapter.setdefault("title", f"第{start_ep}-{end_ep}集")
            chapter.setdefault("arc_type", "default")
            chapter.setdefault("core_conflict", "")
            chapter.setdefault("climax_episode", (start_ep + end_ep) // 2)
            chapter.setdefault("boundary_reason", "auto-generated")
            chapters.append(chapter)
        return chapters
