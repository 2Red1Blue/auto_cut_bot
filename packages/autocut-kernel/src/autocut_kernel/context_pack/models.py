"""Closed canonical values for external narrative context.

They deliberately use primitive, JSON-shaped fields so either runtime can
persist the same canonical bytes.  Credentials and raw URL query strings never
belong to these values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from ..media.types import canonical_sha256, sha256_prefixed

_MAX_TEXT = 1_024
_MAX_IDENTIFIER = 256


def _text(
    value: object,
    name: str,
    *,
    maximum: int = _MAX_TEXT,
    allow_newlines: bool = False,
) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():  # noqa: E721
        raise ValueError(f"{name} must be non-empty canonical text")
    if len(value) > maximum or any(
        (ord(char) < 32 and not (allow_newlines and char == "\n")) or ord(char) == 127
        for char in value
    ):
        raise ValueError(f"{name} is too long or contains a control character")
    return value


def _optional_text(value: object, name: str, *, maximum: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    return _text(value, name, maximum=maximum)


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value < 1:  # noqa: E721
        raise ValueError(f"{name} must be a positive integer")
    return value


def _sorted_unique(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    parsed = tuple(_text(value, name, maximum=_MAX_IDENTIFIER) for value in values)
    if parsed != tuple(sorted(set(parsed))):
        raise ValueError(f"{name} must be sorted and unique")
    return parsed


@dataclass(frozen=True, slots=True)
class ExternalContextSnapshot:
    """One immutable capture of all API resources used for one series.

    ``raw_payload_sha256`` binds canonical bytes persisted by the caller.  The
    fetch identity intentionally keeps only endpoint origin/path labels and a
    non-secret credential scope identifier.
    """

    snapshot_id: str
    series_external_id: str
    resource_paths: tuple[str, ...]
    endpoint_origin: str
    credential_scope_id: str
    raw_payload_sha256: str

    def __post_init__(self) -> None:
        _text(self.snapshot_id, "snapshot_id", maximum=_MAX_IDENTIFIER)
        _text(self.series_external_id, "series_external_id", maximum=_MAX_IDENTIFIER)
        paths = _sorted_unique(self.resource_paths, "resource_paths")
        if not paths:
            raise ValueError("resource_paths must not be empty")
        if any(not value.startswith("/") for value in paths):
            raise ValueError("resource_paths must be paths, never complete URLs")
        _text(self.endpoint_origin, "endpoint_origin", maximum=_MAX_IDENTIFIER)
        if "://" not in self.endpoint_origin or "/" in self.endpoint_origin.split("://", 1)[1]:
            raise ValueError("endpoint_origin must be a normalized scheme and authority")
        _text(self.credential_scope_id, "credential_scope_id", maximum=_MAX_IDENTIFIER)
        sha256_prefixed(self.raw_payload_sha256, "raw_payload_sha256")
        object.__setattr__(self, "resource_paths", paths)

    def to_mapping(self) -> dict[str, object]:
        return {
            "credential_scope_id": self.credential_scope_id,
            "endpoint_origin": self.endpoint_origin,
            "raw_payload_sha256": self.raw_payload_sha256,
            "resource_paths": list(self.resource_paths),
            "series_external_id": self.series_external_id,
            "snapshot_id": self.snapshot_id,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EpisodeContextBindingSet:
    """Complete explicit local-to-external episode map for one series.

    A single binding proves one VLM request is attached to the intended
    episode.  The set adds the collection-level invariant: no local episode
    and no external episode may be silently bound twice.  It has no inference
    or conflict-resolution mode; an owner must correct an ambiguous map before
    API context is exposed to a model.
    """

    series_external_id: str
    bindings: tuple[EpisodeContextBinding, ...]

    def __post_init__(self) -> None:
        _text(self.series_external_id, "series_external_id", maximum=_MAX_IDENTIFIER)
        values = tuple(self.bindings)
        if not values:
            raise ValueError("episode binding set must not be empty")
        if any(type(item) is not EpisodeContextBinding for item in values):  # noqa: E721
            raise ValueError("bindings must contain exact EpisodeContextBinding values")
        if any(item.series_external_id != self.series_external_id for item in values):
            raise ValueError("binding set cannot span external series")
        local_keys = tuple(
            (item.local_source_id, item.local_source_sha256, item.local_episode_index)
            for item in values
        )
        external_keys = tuple(
            (item.external_episode_id, item.external_chapter_id)
            for item in values
        )
        if len(set(local_keys)) != len(local_keys):
            raise ValueError("binding set contains duplicate local episode identities")
        if len(set(external_keys)) != len(external_keys):
            raise ValueError("binding set contains duplicate external episode identities")
        if tuple(item.canonical_hash for item in values) != tuple(
            sorted(item.canonical_hash for item in values)
        ):
            raise ValueError("bindings must be sorted by canonical hash")
        object.__setattr__(self, "bindings", values)

    def to_mapping(self) -> dict[str, object]:
        return {
            "bindings": [item.to_mapping() for item in self.bindings],
            "series_external_id": self.series_external_id,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class NormalizedCharacter:
    character_ref: str
    name: str
    aliases: tuple[str, ...]
    role: str | None

    def __post_init__(self) -> None:
        _text(self.character_ref, "character_ref", maximum=_MAX_IDENTIFIER)
        _text(self.name, "character.name", maximum=160)
        object.__setattr__(self, "aliases", _sorted_unique(self.aliases, "character.aliases"))
        object.__setattr__(self, "role", _optional_text(self.role, "character.role", maximum=280))

    def to_mapping(self) -> dict[str, object]:
        return {"aliases": list(self.aliases), "character_ref": self.character_ref, "name": self.name, "role": self.role}


@dataclass(frozen=True, slots=True)
class NormalizedRelationship:
    relationship_ref: str
    subject_character_ref: str
    object_character_ref: str
    description: str
    known_from_external_episode_ordinal: int

    def __post_init__(self) -> None:
        for name in ("relationship_ref", "subject_character_ref", "object_character_ref"):
            _text(getattr(self, name), name, maximum=_MAX_IDENTIFIER)
        _text(self.description, "relationship.description", maximum=320)
        _positive(self.known_from_external_episode_ordinal, "relationship.known_from")

    def to_mapping(self) -> dict[str, object]:
        return {
            "description": self.description,
            "known_from_external_episode_ordinal": self.known_from_external_episode_ordinal,
            "object_character_ref": self.object_character_ref,
            "relationship_ref": self.relationship_ref,
            "subject_character_ref": self.subject_character_ref,
        }


@dataclass(frozen=True, slots=True)
class NormalizedEpisode:
    external_episode_id: str
    external_chapter_id: str | None
    external_episode_ordinal: int | None
    title: str | None
    summary: str | None
    character_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.external_episode_id, "external_episode_id", maximum=_MAX_IDENTIFIER)
        object.__setattr__(self, "external_chapter_id", _optional_text(self.external_chapter_id, "external_chapter_id", maximum=_MAX_IDENTIFIER))
        if self.external_episode_ordinal is not None:
            _positive(self.external_episode_ordinal, "external_episode_ordinal")
        object.__setattr__(self, "title", _optional_text(self.title, "episode.title", maximum=280))
        object.__setattr__(self, "summary", _optional_text(self.summary, "episode.summary", maximum=900))
        object.__setattr__(self, "character_refs", _sorted_unique(self.character_refs, "episode.character_refs"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "character_refs": list(self.character_refs),
            "external_chapter_id": self.external_chapter_id,
            "external_episode_id": self.external_episode_id,
            "external_episode_ordinal": self.external_episode_ordinal,
            "summary": self.summary,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class NormalizedNarrativeContext:
    snapshot_hash: str
    series_external_id: str
    series_title: str | None
    language: str | None
    stable_premise: str | None
    themes: tuple[str, ...]
    characters: tuple[NormalizedCharacter, ...]
    relationships: tuple[NormalizedRelationship, ...]
    episodes: tuple[NormalizedEpisode, ...]

    def __post_init__(self) -> None:
        sha256_prefixed(self.snapshot_hash, "snapshot_hash")
        _text(self.series_external_id, "series_external_id", maximum=_MAX_IDENTIFIER)
        object.__setattr__(self, "series_title", _optional_text(self.series_title, "series_title", maximum=280))
        object.__setattr__(self, "language", _optional_text(self.language, "language", maximum=64))
        object.__setattr__(self, "stable_premise", _optional_text(self.stable_premise, "stable_premise", maximum=420))
        object.__setattr__(self, "themes", _sorted_unique(self.themes, "themes"))
        for name, values, kind, key in (
            ("characters", self.characters, NormalizedCharacter, "character_ref"),
            ("relationships", self.relationships, NormalizedRelationship, "relationship_ref"),
            ("episodes", self.episodes, NormalizedEpisode, "external_episode_id"),
        ):
            values = tuple(values)
            if any(type(value) is not kind for value in values):  # noqa: E721
                raise ValueError(f"{name} must contain exact {kind.__name__} values")
            if tuple(getattr(value, key) for value in values) != tuple(sorted(getattr(value, key) for value in values)):
                raise ValueError(f"{name} must be sorted canonically")
            object.__setattr__(self, name, values)
        character_refs = {value.character_ref for value in self.characters}
        if any(ref not in character_refs for item in self.episodes for ref in item.character_refs):
            raise ValueError("episode character refs must resolve")
        if any(
            ref not in character_refs
            for item in self.relationships
            for ref in (item.subject_character_ref, item.object_character_ref)
        ):
            raise ValueError("relationship character refs must resolve")

    def to_mapping(self) -> dict[str, object]:
        return {
            "characters": [item.to_mapping() for item in self.characters],
            "episodes": [item.to_mapping() for item in self.episodes],
            "language": self.language,
            "relationships": [item.to_mapping() for item in self.relationships],
            "series_external_id": self.series_external_id,
            "series_title": self.series_title,
            "snapshot_hash": self.snapshot_hash,
            "stable_premise": self.stable_premise,
            "themes": list(self.themes),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EpisodeContextBinding:
    local_source_id: str
    local_source_sha256: str
    local_episode_index: int
    series_external_id: str
    external_episode_id: str
    external_chapter_id: str | None
    external_episode_ordinal: int
    mapping_method: Literal["owner_explicit"] = "owner_explicit"
    mapping_status: Literal["bound"] = "bound"

    def __post_init__(self) -> None:
        for name in ("local_source_id", "series_external_id", "external_episode_id"):
            _text(getattr(self, name), name, maximum=_MAX_IDENTIFIER)
        sha256_prefixed(self.local_source_sha256, "local_source_sha256")
        if type(self.local_episode_index) is not int or self.local_episode_index < 0:  # noqa: E721
            raise ValueError("local_episode_index must be non-negative")
        object.__setattr__(self, "external_chapter_id", _optional_text(self.external_chapter_id, "external_chapter_id", maximum=_MAX_IDENTIFIER))
        _positive(self.external_episode_ordinal, "external_episode_ordinal")
        if self.mapping_method != "owner_explicit" or self.mapping_status != "bound":
            raise ValueError("v1 accepts only an owner_explicit bound episode mapping")

    def to_mapping(self) -> dict[str, object]:
        return {
            "external_chapter_id": self.external_chapter_id,
            "external_episode_id": self.external_episode_id,
            "external_episode_ordinal": self.external_episode_ordinal,
            "local_episode_index": self.local_episode_index,
            "local_source_id": self.local_source_id,
            "local_source_sha256": self.local_source_sha256,
            "mapping_method": self.mapping_method,
            "mapping_status": self.mapping_status,
            "series_external_id": self.series_external_id,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class OwnerEpisodeMap:
    """Owner-supplied map before SourcePrep supplies content-bound identity.

    ``local_source_id`` contains a content hash and must never come from an
    HTTP caller. The owner names one exact relative path; Context Prepare
    derives both source ID and SHA-256 from committed SourcePrep output.
    """

    local_relative_path: str
    local_episode_index: int
    series_external_id: str
    external_episode_id: str
    external_chapter_id: str | None
    external_episode_ordinal: int

    def __post_init__(self) -> None:
        for name in ("series_external_id", "external_episode_id"):
            _text(getattr(self, name), name, maximum=_MAX_IDENTIFIER)
        _text(self.local_relative_path, "local_relative_path", maximum=_MAX_IDENTIFIER)
        if (
            self.local_relative_path.startswith("/")
            or any(part in ("", ".", "..") for part in self.local_relative_path.split("/"))
        ):
            raise ValueError("local_relative_path must be a canonical relative source path")
        if type(self.local_episode_index) is not int or self.local_episode_index < 0:  # noqa: E721
            raise ValueError("local_episode_index must be non-negative")
        object.__setattr__(self, "external_chapter_id", _optional_text(
            self.external_chapter_id, "external_chapter_id", maximum=_MAX_IDENTIFIER
        ))
        _positive(self.external_episode_ordinal, "external_episode_ordinal")

    def to_mapping(self) -> dict[str, object]:
        return {
            "external_chapter_id": self.external_chapter_id,
            "external_episode_id": self.external_episode_id,
            "external_episode_ordinal": self.external_episode_ordinal,
            "local_episode_index": self.local_episode_index,
            "local_relative_path": self.local_relative_path,
            "series_external_id": self.series_external_id,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def bind(self, *, local_source_id: str, local_source_sha256: str) -> EpisodeContextBinding:
        return EpisodeContextBinding(
            local_source_id=local_source_id,
            local_source_sha256=local_source_sha256,
            local_episode_index=self.local_episode_index,
            series_external_id=self.series_external_id,
            external_episode_id=self.external_episode_id,
            external_chapter_id=self.external_chapter_id,
            external_episode_ordinal=self.external_episode_ordinal,
        )


@dataclass(frozen=True, slots=True)
class OwnerEpisodeMapSet:
    """Closed owner input; no title/order/filename inference is available."""

    series_external_id: str
    mappings: tuple[OwnerEpisodeMap, ...]

    def __post_init__(self) -> None:
        _text(self.series_external_id, "series_external_id", maximum=_MAX_IDENTIFIER)
        values = tuple(self.mappings)
        if not values or any(type(item) is not OwnerEpisodeMap for item in values):  # noqa: E721
            raise ValueError("mappings must be a non-empty OwnerEpisodeMap tuple")
        if any(item.series_external_id != self.series_external_id for item in values):
            raise ValueError("owner map set cannot span external series")
        local_keys = tuple((item.local_relative_path, item.local_episode_index) for item in values)
        external_keys = tuple((item.external_episode_id, item.external_chapter_id) for item in values)
        if len(set(local_keys)) != len(local_keys):
            raise ValueError("owner map set contains duplicate local episode identities")
        if len(set(external_keys)) != len(external_keys):
            raise ValueError("owner map set contains duplicate external episode identities")
        if tuple(item.canonical_hash for item in values) != tuple(sorted(item.canonical_hash for item in values)):
            raise ValueError("mappings must be sorted by canonical hash")
        object.__setattr__(self, "mappings", values)

    def to_mapping(self) -> dict[str, object]:
        return {
            "mappings": [item.to_mapping() for item in self.mappings],
            "series_external_id": self.series_external_id,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class WindowContextPack:
    mode: Literal["api_assisted", "video_only"]
    source_binding_hash: str | None
    normalized_context_hash: str | None
    selection_policy_version: str
    selection_policy_hash: str
    known_through_external_episode_ordinal: int | None
    selected_refs: tuple[str, ...]
    suppressed_reason_counts: tuple[tuple[str, int], ...]
    rendered_context: str
    video_only_reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("api_assisted", "video_only"):
            raise ValueError("unsupported context pack mode")
        _text(self.selection_policy_version, "selection_policy_version", maximum=8_192)
        _text(self.rendered_context, "rendered_context", maximum=8_192, allow_newlines=True)
        sha256_prefixed(self.selection_policy_hash, "selection_policy_hash")
        for name in ("source_binding_hash", "normalized_context_hash"):
            value = getattr(self, name)
            if value is not None:
                sha256_prefixed(value, name)
        if self.known_through_external_episode_ordinal is not None:
            _positive(self.known_through_external_episode_ordinal, "known_through_external_episode_ordinal")
        object.__setattr__(self, "selected_refs", _sorted_unique(self.selected_refs, "selected_refs"))
        counts = tuple(self.suppressed_reason_counts)
        if tuple(key for key, _ in counts) != tuple(sorted(key for key, _ in counts)):
            raise ValueError("suppressed_reason_counts must be sorted")
        if any(type(key) is not str or type(count) is not int or count < 1 for key, count in counts):  # noqa: E721
            raise ValueError("suppressed_reason_counts must be positive canonical pairs")
        object.__setattr__(self, "suppressed_reason_counts", counts)
        if self.mode == "api_assisted":
            if None in (self.source_binding_hash, self.normalized_context_hash, self.known_through_external_episode_ordinal):
                raise ValueError("api_assisted pack requires binding, normalized context and ordinal")
            if self.video_only_reason_code is not None:
                raise ValueError("api_assisted pack cannot carry a video-only reason")
        else:
            if self.video_only_reason_code is None:
                raise ValueError("video_only pack requires a reason code")
            _text(self.video_only_reason_code, "video_only_reason_code", maximum=128)
            if self.selected_refs:
                raise ValueError("video_only pack must not expose selected API refs")

    def to_mapping(self) -> dict[str, object]:
        return {
            "known_through_external_episode_ordinal": self.known_through_external_episode_ordinal,
            "mode": self.mode,
            "normalized_context_hash": self.normalized_context_hash,
            "rendered_context": self.rendered_context,
            "selected_refs": list(self.selected_refs),
            "selection_policy_hash": self.selection_policy_hash,
            "selection_policy_version": self.selection_policy_version,
            "source_binding_hash": self.source_binding_hash,
            "suppressed_reason_counts": [list(item) for item in self.suppressed_reason_counts],
            "video_only_reason_code": self.video_only_reason_code,
        }

    @classmethod
    def from_mapping(cls, value: object) -> WindowContextPack:
        if type(value) is not dict:  # noqa: E721
            raise ValueError("window context pack must be an object")
        item = cast(dict[str, object], value)
        required = {
            "known_through_external_episode_ordinal", "mode", "normalized_context_hash",
            "rendered_context", "selected_refs", "selection_policy_hash",
            "selection_policy_version", "source_binding_hash", "suppressed_reason_counts",
            "video_only_reason_code",
        }
        if set(item) != required:
            raise ValueError("window context pack fields are not closed")
        selected = item["selected_refs"]
        suppressed = item["suppressed_reason_counts"]
        if type(selected) is not list or type(suppressed) is not list:  # noqa: E721
            raise ValueError("window context pack collections are invalid")
        selected_values = cast(list[object], selected)
        suppressed_values = cast(list[object], suppressed)
        counts_list: list[tuple[str, int]] = []
        for pair in suppressed_values:
            if type(pair) is not list:  # noqa: E721
                raise ValueError("window context suppression counts are invalid")
            pair_values = cast(list[object], pair)
            if len(pair_values) != 2:
                raise ValueError("window context suppression counts are invalid")
            key, count = pair_values
            if type(key) is not str or type(count) is not int:  # noqa: E721
                raise ValueError("window context suppression counts are invalid")
            counts_list.append((key, count))
        counts = tuple(counts_list)
        return cls(
            mode=item["mode"],  # type: ignore[arg-type]
            source_binding_hash=item["source_binding_hash"],  # type: ignore[arg-type]
            normalized_context_hash=item["normalized_context_hash"],  # type: ignore[arg-type]
            selection_policy_version=item["selection_policy_version"],  # type: ignore[arg-type]
            selection_policy_hash=item["selection_policy_hash"],  # type: ignore[arg-type]
            known_through_external_episode_ordinal=item["known_through_external_episode_ordinal"],  # type: ignore[arg-type]
            selected_refs=tuple(cast(str, entry) for entry in selected_values),
            suppressed_reason_counts=counts,
            rendered_context=item["rendered_context"],  # type: ignore[arg-type]
            video_only_reason_code=item["video_only_reason_code"],  # type: ignore[arg-type]
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())
