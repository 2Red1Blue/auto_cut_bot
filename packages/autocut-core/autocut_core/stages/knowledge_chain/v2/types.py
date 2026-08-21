"""Typed boundary contracts for the historical Knowledge Chain V2 pipeline.

The old Stage receives JSON decoded from artifacts and provider responses.  This
module is the single place where that untrusted shape becomes a constrained
Python value before a layer can consume it.  It deliberately models the
legacy payload only; it is not a v2.1.3 production-contract model.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from typing import NotRequired, Protocol, TypeAlias, TypedDict, cast

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]


class LLMProvider(Protocol):
    """The asynchronous JSON-producing provider used by all three layers."""

    def __call__(
        self,
        prompt: str,
        /,
        *,
        response_format: Mapping[str, str] | None = None,
        max_tokens: int | None = None,
    ) -> Awaitable[str]: ...


class TimelineSegment(TypedDict, total=False):
    mode: str
    summary: str


class StoryBeatHint(TypedDict, total=False):
    characters: list[str]
    function: str
    summary: str


class EpisodeSummary(TypedDict):
    ep: int
    summary: str
    raw_vlm_data: NotRequired[JSONObject]
    timeline_segments: NotRequired[list[TimelineSegment]]
    story_beats: NotRequired[list[StoryBeatHint]]
    scene_locations: NotRequired[list[JSONValue]]


class EventCard(TypedDict, total=False):
    id: str
    ep: int
    episode: int
    start_time: float | int
    summary: str
    description: str


class Chapter(TypedDict):
    start_ep: int
    end_ep: int
    title: str
    arc_type: str
    core_conflict: str
    climax_episode: int
    boundary_reason: str
    chapter_id: NotRequired[str]
    key_characters: NotRequired[list[str]]
    required_beats: NotRequired[list[str]]


class GlobalStoryThread(TypedDict, total=False):
    thread_id: str
    name: str
    description: str
    core_arc: str
    start_ep: int
    end_ep: int
    key_nodes: list[JSONValue]
    chapter_coverage: list[int]
    importance_tier: str
    importance: float | int


class GlobalCharacter(TypedDict, total=False):
    char_id: str
    canonical_name: str
    name: str
    aliases: list[str]
    identity: str
    faction: str
    first_ep: int
    last_ep: int
    core_relations: list[JSONObject]
    importance_tier: str
    importance: float | int
    arc_summary: str
    is_core: bool


class GlobalFramework(TypedDict, total=False):
    series_title: str
    chapters: list[Chapter]
    global_story_threads: list[JSONObject]
    global_characters: list[JSONObject]
    global_character_preview: list[JSONObject]
    global_world_rules: list[JSONObject]
    global_key_props: list[JSONObject]
    global_foreshadow_map: list[JSONObject]
    global_turning_points: list[JSONObject]
    highlight_seed_points: list[JSONObject]
    global_timeline_markers: list[JSONObject]
    tension_curve: list[JSONObject]
    themes: list[JSONObject]
    world_rules: list[JSONObject]
    foreshadow_payoff_pairs: list[JSONObject]


class StoryThreadUpdate(TypedDict, total=False):
    thread_id: str
    beats: list[JSONObject]


class Pass1Output(TypedDict, total=False):
    story_thread_updates: list[StoryThreadUpdate]
    beats: list[JSONObject]
    summary: str
    new_world_rules: list[JSONValue]
    new_open_questions: list[JSONValue]
    resolved_questions: list[JSONValue]
    foreshadow_payoffs: list[JSONValue]
    new_facts: list[JSONValue]
    excluded_episodes: list[JSONObject]


class Pass2Output(TypedDict, total=False):
    character_rollup: list[JSONObject]
    relationship_rollup: list[JSONObject]


class ChapterOutput(Pass1Output, Pass2Output, total=False):
    chapter_id: str
    chapter: Chapter
    overlap_eps: list[int]
    warnings: list[JSONObject]


RawLLMObject: TypeAlias = Mapping[str, JSONValue]
JSONSequence: TypeAlias = Sequence[JSONValue]


def decode_json_value(value: object) -> JSONValue | None:
    """Copy a decoded external value into the JSON domain or reject it.

    ``json.loads`` is typed as returning ``Any`` by typeshed.  Keeping that
    value behind this function prevents it from leaking into layer logic.
    """

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        raw_items = cast(list[object], value)
        decoded_items: list[JSONValue] = []
        for item in raw_items:
            decoded = decode_json_value(item)
            if decoded is None and item is not None:
                return None
            decoded_items.append(decoded)
        return decoded_items
    if isinstance(value, dict):
        raw_items = cast(dict[object, object], value)
        decoded_object: JSONObject = {}
        for key, item in raw_items.items():
            if not isinstance(key, str):
                return None
            decoded = decode_json_value(item)
            if decoded is None and item is not None:
                return None
            decoded_object[key] = decoded
        return decoded_object
    return None


def decode_json_object(value: object) -> JSONObject | None:
    """Return a copied JSON object, rejecting arrays/scalars and bad values."""

    decoded = decode_json_value(value)
    return decoded if isinstance(decoded, dict) else None


def json_object_list(value: JSONValue | None) -> list[JSONObject]:
    """Project only object members from a JSON array at a legacy boundary."""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
