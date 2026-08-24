"""Closed immutable Stage 1--3 production artifact values.

These values deliberately stop at semantic and coarse-duration boundaries.
They contain no transcript, VAD, filesystem location, floating-point seconds,
or physical media endpoint.  A compiler may only create a :class:`Candidate`
from an owner-bound VLM observation capability, so editing modes and the
observation content hash cannot be supplied independently by a draft/provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, Mapping, Sequence, cast

from ..contracts.compiler.canonical import canonical_json_hash
from ..contracts.compiler.refs import ArtifactRef, DomainRef

_SHA256: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*\Z")
_SAFE_TOKEN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_NARRATIVE_FUNCTION: Final = re.compile(r"[a-z][a-z0-9_]*\Z")
_FORBIDDEN_FIELD_PARTS: Final = (
    "asr",
    "transcript",
    "vad",
    "start_pts",
    "end_pts",
    "in_tick",
    "out_tick",
    "start_seconds",
    "end_seconds",
    "physical_endpoint",
    "cut_endpoint",
)


class ProductionModelError(ValueError):
    """A Stage 1--3 artifact violates its closed production contract."""


class EditingMode(str, Enum):
    DIALOGUE = "dialogue"
    ACTION = "action"


class NarrativeNodeType(str, Enum):
    FACT = "fact"
    EVENT = "event"
    BEAT = "beat"
    OBLIGATION = "obligation"
    STORY_THREAD = "story_thread"
    CHARACTER = "character"
    RELATIONSHIP = "relationship"
    QUESTION = "question"
    FORESHADOW = "foreshadow"


class CoverageUnitType(str, Enum):
    VLM_OBSERVATION = "vlm_observation"
    VLM_WINDOW = "vlm_window"
    EVENT = "event"
    OBLIGATION = "obligation"


class CoverageResolution(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    CONFLICTED = "conflicted"


class CoverageDisposition(str, Enum):
    NARRATIVE = "narrative"
    SUPPORTING = "supporting"
    INTENTIONALLY_EXCLUDED = "intentionally_excluded"
    UNASSIGNED = "unassigned"


_EDITING_MODE_ORDER: Final = (EditingMode.DIALOGUE, EditingMode.ACTION)
_EDGE_TYPES: Final = frozenset(
    {
        "supports",
        "satisfies",
        "requires",
        "precedes",
        "causes",
        "contradicts",
        "involves",
        "resolves",
    }
)
_PHYSICAL_REQUIREMENTS: Final = {
    "dialogue_integrity": "complete",
    "subtitle_clearance": "protect_detected_cues",
    "visual_validity": "endpoint_and_stable_region",
}


class _CanonicalModel:
    def to_mapping(self) -> dict[str, object]:
        raise NotImplementedError

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def _mapping(value: object, expected: set[str], label: str) -> Mapping[str, object]:
    _reject_forbidden_fields(value, label)
    if type(value) is not dict:  # noqa: E721 - wire mappings are exact JSON objects.
        raise ProductionModelError(f"{label} must be an object")
    raw_mapping = cast(dict[object, object], value)
    if set(raw_mapping) != expected:
        raise ProductionModelError(f"{label} must have exactly {sorted(expected)}")
    return cast(Mapping[str, object], raw_mapping)


def _reject_forbidden_fields(value: object, label: str) -> None:
    if type(value) is dict:  # noqa: E721
        for key, child in cast(dict[object, object], value).items():
            if type(key) is not str:  # noqa: E721
                raise ProductionModelError(f"{label} field names must be strings")
            folded = key.casefold()
            if any(part in folded for part in _FORBIDDEN_FIELD_PARTS):
                raise ProductionModelError(f"{label}.{key} is forbidden in Stage 1-3")
            _reject_forbidden_fields(child, f"{label}.{key}")
    elif type(value) is list:  # noqa: E721
        for index, child in enumerate(cast(list[object], value)):
            _reject_forbidden_fields(child, f"{label}[{index}]")
    elif type(value) is float:  # noqa: E721
        raise ProductionModelError(f"{label} must not contain float values")


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ProductionModelError(f"{label} must be a non-empty string")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label)
    if not _IDENTIFIER.fullmatch(text):
        raise ProductionModelError(f"{label} must be an opaque identifier, not a path")
    return text


def _safe_token(value: object, label: str) -> str:
    text = _text(value, label)
    if not _SAFE_TOKEN.fullmatch(text):
        raise ProductionModelError(f"{label} must be a safe opaque token")
    return text


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):  # noqa: E721
        raise ProductionModelError(f"{label} must be a lowercase sha256 digest")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:  # noqa: E721 - bool is forbidden.
        raise ProductionModelError(f"{label} must be an integer >= {minimum}")
    return value


def _typed_tuple(
    values: Sequence[object], expected_type: type[object], label: str, *, nonempty: bool = False
) -> tuple[object, ...]:
    result = tuple(values)
    if nonempty and not result:
        raise ProductionModelError(f"{label} must not be empty")
    if any(type(item) is not expected_type for item in result):  # noqa: E721
        raise ProductionModelError(f"{label} contains an invalid value")
    return result


def _ref_key(reference: DomainRef) -> bytes:
    return str(reference.to_mapping()).encode("utf-8")


def _unique_domain_refs(
    values: Sequence[DomainRef], label: str, *, nonempty: bool = False
) -> tuple[DomainRef, ...]:
    result = cast(tuple[DomainRef, ...], _typed_tuple(values, DomainRef, label, nonempty=nonempty))
    keys = tuple(canonical_json_hash(item.to_mapping()) for item in result)
    if len(keys) != len(set(keys)):
        raise ProductionModelError(f"{label} must not contain duplicates")
    return result


def _unique_ids(values: Sequence[str], label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    result = tuple(_identifier(item, label) for item in values)
    if nonempty and not result:
        raise ProductionModelError(f"{label} must not be empty")
    if len(result) != len(set(result)):
        raise ProductionModelError(f"{label} must not contain duplicates")
    return result


def _domain_ref(value: object, label: str) -> DomainRef:
    try:
        return DomainRef.from_mapping(value)
    except ValueError as error:
        raise ProductionModelError(f"{label} is invalid") from error


def _artifact_ref(value: object, label: str) -> ArtifactRef:
    try:
        return ArtifactRef.from_mapping(value)
    except ValueError as error:
        raise ProductionModelError(f"{label} is invalid") from error


def _object_list(value: object, label: str) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise ProductionModelError(f"{label} must be an array")
    return cast(list[object], value)


@dataclass(frozen=True, slots=True)
class OwnerBoundVlmObservationRef(_CanonicalModel):
    observation_ref: DomainRef
    vlm_observation_sha256: str
    source_ref: DomainRef
    window_ref: DomainRef
    capability_policy_ref: ArtifactRef
    editing_modes: tuple[EditingMode, ...]

    def __post_init__(self) -> None:
        if (
            type(self.observation_ref) is not DomainRef
            or self.observation_ref.object_type != "vlm_observation"
        ):  # noqa: E721
            raise ProductionModelError("observation_ref must be an owner-bound vlm_observation")
        _sha256(self.vlm_observation_sha256, "vlm_observation_sha256")
        if type(self.source_ref) is not DomainRef or self.source_ref.object_type != "source":  # noqa: E721
            raise ProductionModelError("source_ref must be an owner-bound source")
        if type(self.window_ref) is not DomainRef or self.window_ref.object_type != "vlm_window":  # noqa: E721
            raise ProductionModelError("window_ref must be an owner-bound vlm_window")
        if type(self.capability_policy_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("capability_policy_ref must be an ArtifactRef")
        modes = tuple(self.editing_modes)
        if not modes or any(type(mode) is not EditingMode for mode in modes):  # noqa: E721
            raise ProductionModelError(
                "editing_modes must be a non-empty subset of dialogue/action"
            )
        if len(modes) != len(set(modes)):
            raise ProductionModelError("editing_modes must not contain duplicates")
        expected = tuple(mode for mode in _EDITING_MODE_ORDER if mode in modes)
        if modes != expected:
            raise ProductionModelError("editing_modes must use canonical dialogue/action order")
        object.__setattr__(self, "editing_modes", modes)

    @classmethod
    def from_mapping(cls, value: object) -> OwnerBoundVlmObservationRef:
        item = _mapping(
            value,
            {
                "observation_ref",
                "vlm_observation_sha256",
                "source_ref",
                "window_ref",
                "capability_policy_ref",
                "editing_modes",
            },
            "owner_bound_vlm_observation_ref",
        )
        raw_modes = _object_list(item["editing_modes"], "editing_modes")
        try:
            modes = tuple(EditingMode(_text(mode, "editing_modes")) for mode in raw_modes)
        except ValueError as error:
            raise ProductionModelError("editing_modes contains an unknown mode") from error
        return cls(
            observation_ref=_domain_ref(item["observation_ref"], "observation_ref"),
            vlm_observation_sha256=_sha256(
                item["vlm_observation_sha256"], "vlm_observation_sha256"
            ),
            source_ref=_domain_ref(item["source_ref"], "source_ref"),
            window_ref=_domain_ref(item["window_ref"], "window_ref"),
            capability_policy_ref=_artifact_ref(
                item["capability_policy_ref"], "capability_policy_ref"
            ),
            editing_modes=modes,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "capability_policy_ref": self.capability_policy_ref.to_mapping(),
            "editing_modes": [mode.value for mode in self.editing_modes],
            "observation_ref": self.observation_ref.to_mapping(),
            "source_ref": self.source_ref.to_mapping(),
            "vlm_observation_sha256": self.vlm_observation_sha256,
            "window_ref": self.window_ref.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class DurationRangeSeconds(_CanonicalModel):
    minimum: int
    target: int
    maximum: int

    def __post_init__(self) -> None:
        minimum = _integer(self.minimum, "duration.minimum", minimum=1)
        target = _integer(self.target, "duration.target", minimum=1)
        maximum = _integer(self.maximum, "duration.maximum", minimum=1)
        if not minimum <= target <= maximum:
            raise ProductionModelError("duration must satisfy minimum <= target <= maximum")

    @classmethod
    def from_mapping(cls, value: object) -> DurationRangeSeconds:
        item = _mapping(value, {"min", "target", "max"}, "duration_seconds")
        return cls(
            _integer(item["min"], "duration.min", minimum=1),
            _integer(item["target"], "duration.target", minimum=1),
            _integer(item["max"], "duration.max", minimum=1),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"max": self.maximum, "min": self.minimum, "target": self.target}


@dataclass(frozen=True, slots=True)
class EpisodeDigest(_CanonicalModel):
    episode_id: str
    ordinal: int
    summary: str
    source_window_refs: tuple[DomainRef, ...]
    evidence_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        _identifier(self.episode_id, "episode_id")
        _integer(self.ordinal, "ordinal", minimum=1)
        _text(self.summary, "summary")
        object.__setattr__(
            self,
            "source_window_refs",
            _unique_domain_refs(self.source_window_refs, "source_window_refs", nonempty=True),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _unique_domain_refs(self.evidence_refs, "evidence_refs", nonempty=True),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "ordinal": self.ordinal,
            "source_window_refs": [item.to_mapping() for item in self.source_window_refs],
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class EpisodeDigestSet(_CanonicalModel):
    episode_digest_set_id: str
    digests: tuple[EpisodeDigest, ...]

    def __post_init__(self) -> None:
        _identifier(self.episode_digest_set_id, "episode_digest_set_id")
        values = cast(
            tuple[EpisodeDigest, ...],
            _typed_tuple(self.digests, EpisodeDigest, "digests", nonempty=True),
        )
        if len({item.episode_id for item in values}) != len(values):
            raise ProductionModelError("digests episode IDs must be unique")
        if len({item.ordinal for item in values}) != len(values):
            raise ProductionModelError("digests ordinals must be unique")
        if tuple(sorted(values, key=lambda item: item.ordinal)) != values:
            raise ProductionModelError("digests must use canonical ordinal order")
        object.__setattr__(self, "digests", values)

    def to_mapping(self) -> dict[str, object]:
        return {
            "digests": [item.to_mapping() for item in self.digests],
            "episode_digest_set_id": self.episode_digest_set_id,
        }


@dataclass(frozen=True, slots=True)
class EventCard(_CanonicalModel):
    event_id: str
    episode_id: str
    content: str
    observation_refs: tuple[OwnerBoundVlmObservationRef, ...]

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        _identifier(self.episode_id, "episode_id")
        _text(self.content, "content")
        observations = cast(
            tuple[OwnerBoundVlmObservationRef, ...],
            _typed_tuple(
                self.observation_refs,
                OwnerBoundVlmObservationRef,
                "observation_refs",
                nonempty=True,
            ),
        )
        hashes = tuple(item.vlm_observation_sha256 for item in observations)
        if len(hashes) != len(set(hashes)):
            raise ProductionModelError("observation_refs must not contain duplicate observations")
        object.__setattr__(self, "observation_refs", observations)

    def to_mapping(self) -> dict[str, object]:
        return {
            "content": self.content,
            "episode_id": self.episode_id,
            "event_id": self.event_id,
            "observation_refs": [item.to_mapping() for item in self.observation_refs],
        }


@dataclass(frozen=True, slots=True)
class EventCardSet(_CanonicalModel):
    event_card_set_id: str
    events: tuple[EventCard, ...]

    def __post_init__(self) -> None:
        _identifier(self.event_card_set_id, "event_card_set_id")
        events = cast(
            tuple[EventCard, ...], _typed_tuple(self.events, EventCard, "events", nonempty=True)
        )
        if len({item.event_id for item in events}) != len(events):
            raise ProductionModelError("events must have unique IDs")
        if tuple(sorted(events, key=lambda item: item.event_id)) != events:
            raise ProductionModelError("events must use canonical event_id order")
        object.__setattr__(self, "events", events)

    def to_mapping(self) -> dict[str, object]:
        return {
            "event_card_set_id": self.event_card_set_id,
            "events": [item.to_mapping() for item in self.events],
        }


@dataclass(frozen=True, slots=True)
class NarrativeNode(_CanonicalModel):
    node_id: str
    node_type: NarrativeNodeType
    label: str
    evidence_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node_id")
        if type(self.node_type) is not NarrativeNodeType:  # noqa: E721
            raise ProductionModelError("node_type is unknown")
        _text(self.label, "label")
        evidence = _unique_domain_refs(
            self.evidence_refs,
            "evidence_refs",
            nonempty=self.node_type is not NarrativeNodeType.STORY_THREAD,
        )
        object.__setattr__(self, "evidence_refs", evidence)

    def to_mapping(self) -> dict[str, object]:
        return {
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "label": self.label,
            "node_id": self.node_id,
            "node_type": self.node_type.value,
        }


@dataclass(frozen=True, slots=True)
class NarrativeEdge(_CanonicalModel):
    edge_id: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    evidence_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        _identifier(self.edge_id, "edge_id")
        if self.edge_type not in _EDGE_TYPES:
            raise ProductionModelError("edge_type is unknown")
        _identifier(self.from_node_id, "from_node_id")
        _identifier(self.to_node_id, "to_node_id")
        object.__setattr__(
            self, "evidence_refs", _unique_domain_refs(self.evidence_refs, "evidence_refs")
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "edge_id": self.edge_id,
            "edge_type": self.edge_type,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
        }


@dataclass(frozen=True, slots=True)
class NarrativeGraph(_CanonicalModel):
    graph_id: str
    nodes: tuple[NarrativeNode, ...]
    edges: tuple[NarrativeEdge, ...]

    def __post_init__(self) -> None:
        _identifier(self.graph_id, "graph_id")
        nodes = cast(
            tuple[NarrativeNode, ...],
            _typed_tuple(self.nodes, NarrativeNode, "nodes", nonempty=True),
        )
        edges = cast(tuple[NarrativeEdge, ...], _typed_tuple(self.edges, NarrativeEdge, "edges"))
        if len({item.node_id for item in nodes}) != len(nodes):
            raise ProductionModelError("nodes must have unique IDs")
        if len({item.edge_id for item in edges}) != len(edges):
            raise ProductionModelError("edges must have unique IDs")
        node_ids = {item.node_id for item in nodes}
        if any(
            edge.from_node_id not in node_ids or edge.to_node_id not in node_ids for edge in edges
        ):
            raise ProductionModelError("edge endpoints must resolve to graph nodes")
        if tuple(sorted(nodes, key=lambda item: item.node_id)) != nodes:
            raise ProductionModelError("nodes must use canonical node_id order")
        if tuple(sorted(edges, key=lambda item: item.edge_id)) != edges:
            raise ProductionModelError("edges must use canonical edge_id order")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)

    def to_mapping(self) -> dict[str, object]:
        return {
            "edges": [item.to_mapping() for item in self.edges],
            "graph_id": self.graph_id,
            "nodes": [item.to_mapping() for item in self.nodes],
        }


@dataclass(frozen=True, slots=True)
class CoverageRow(_CanonicalModel):
    coverage_id: str
    unit_type: CoverageUnitType
    unit_ref: DomainRef
    resolution_status: CoverageResolution
    disposition: CoverageDisposition
    graph_node_refs: tuple[DomainRef, ...]
    evidence_refs: tuple[DomainRef, ...]
    taint_seed_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.coverage_id, "coverage_id")
        if type(self.unit_type) is not CoverageUnitType:  # noqa: E721
            raise ProductionModelError("unit_type is unknown")
        if type(self.unit_ref) is not DomainRef:  # noqa: E721
            raise ProductionModelError("unit_ref must be a DomainRef")
        if type(self.resolution_status) is not CoverageResolution:  # noqa: E721
            raise ProductionModelError("resolution_status is unknown")
        if type(self.disposition) is not CoverageDisposition:  # noqa: E721
            raise ProductionModelError("disposition is unknown")
        if self.resolution_status is CoverageResolution.UNRESOLVED:
            if self.disposition is not CoverageDisposition.UNASSIGNED:
                raise ProductionModelError("unresolved coverage must remain unassigned")
        if self.resolution_status is CoverageResolution.RESOLVED:
            if self.disposition is CoverageDisposition.UNASSIGNED:
                raise ProductionModelError("resolved coverage must be disposed")
            if self.taint_seed_ids:
                raise ProductionModelError("resolved coverage cannot carry taint seeds")
        if (
            self.resolution_status is CoverageResolution.CONFLICTED
            and self.disposition is CoverageDisposition.INTENTIONALLY_EXCLUDED
        ):
            raise ProductionModelError("conflicted coverage cannot be intentionally excluded")
        object.__setattr__(
            self, "graph_node_refs", _unique_domain_refs(self.graph_node_refs, "graph_node_refs")
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _unique_domain_refs(self.evidence_refs, "evidence_refs", nonempty=True),
        )
        seeds = _unique_ids(self.taint_seed_ids, "taint_seed_ids")
        if self.resolution_status is not CoverageResolution.RESOLVED and len(seeds) != 1:
            raise ProductionModelError(
                "unresolved/conflicted coverage requires exactly one taint seed"
            )
        object.__setattr__(self, "taint_seed_ids", seeds)

    def to_mapping(self) -> dict[str, object]:
        return {
            "coverage_id": self.coverage_id,
            "disposition": self.disposition.value,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "graph_node_refs": [item.to_mapping() for item in self.graph_node_refs],
            "resolution_status": self.resolution_status.value,
            "taint_seed_ids": list(self.taint_seed_ids),
            "unit_ref": self.unit_ref.to_mapping(),
            "unit_type": self.unit_type.value,
        }


@dataclass(frozen=True, slots=True)
class CoverageConservation(_CanonicalModel):
    input_unit_count: int
    ledger_unit_count: int
    duplicate_unit_refs: tuple[DomainRef, ...]
    missing_unit_refs: tuple[DomainRef, ...]
    unexpected_unit_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        _integer(self.input_unit_count, "input_unit_count")
        _integer(self.ledger_unit_count, "ledger_unit_count")
        for name in ("duplicate_unit_refs", "missing_unit_refs", "unexpected_unit_refs"):
            object.__setattr__(self, name, _unique_domain_refs(getattr(self, name), name))

    def to_mapping(self) -> dict[str, object]:
        return {
            "duplicate_unit_refs": [item.to_mapping() for item in self.duplicate_unit_refs],
            "input_unit_count": self.input_unit_count,
            "ledger_unit_count": self.ledger_unit_count,
            "missing_unit_refs": [item.to_mapping() for item in self.missing_unit_refs],
            "unexpected_unit_refs": [item.to_mapping() for item in self.unexpected_unit_refs],
        }


@dataclass(frozen=True, slots=True, init=False)
class CoverageLedger(_CanonicalModel):
    ledger_id: str
    rows: tuple[CoverageRow, ...]
    conservation: CoverageConservation

    @classmethod
    def from_inputs(
        cls,
        ledger_id: str,
        *,
        input_unit_refs: Sequence[DomainRef],
        rows: Sequence[CoverageRow],
    ) -> CoverageLedger:
        _identifier(ledger_id, "ledger_id")
        expected = _unique_domain_refs(input_unit_refs, "input_unit_refs", nonempty=True)
        row_values = cast(
            tuple[CoverageRow, ...], _typed_tuple(rows, CoverageRow, "rows", nonempty=True)
        )
        if len({item.coverage_id for item in row_values}) != len(row_values):
            raise ProductionModelError("coverage rows contain duplicate coverage IDs")
        row_keys = tuple(canonical_json_hash(item.unit_ref.to_mapping()) for item in row_values)
        duplicate_keys = {key for key in row_keys if row_keys.count(key) > 1}
        if duplicate_keys:
            raise ProductionModelError("coverage rows contain duplicate unit refs")
        expected_by_key = {canonical_json_hash(item.to_mapping()): item for item in expected}
        actual_by_key = {
            canonical_json_hash(item.unit_ref.to_mapping()): item.unit_ref for item in row_values
        }
        missing = tuple(
            expected_by_key[key] for key in sorted(set(expected_by_key) - set(actual_by_key))
        )
        unexpected = tuple(
            actual_by_key[key] for key in sorted(set(actual_by_key) - set(expected_by_key))
        )
        conservation = CoverageConservation(len(expected), len(row_values), (), missing, unexpected)
        if (
            conservation.input_unit_count != conservation.ledger_unit_count
            or conservation.missing_unit_refs
            or conservation.unexpected_unit_refs
        ):
            raise ProductionModelError("coverage conservation failed")
        instance = object.__new__(cls)
        object.__setattr__(instance, "ledger_id", ledger_id)
        object.__setattr__(instance, "rows", row_values)
        object.__setattr__(instance, "conservation", conservation)
        return instance

    def to_mapping(self) -> dict[str, object]:
        return {
            "conservation": self.conservation.to_mapping(),
            "ledger_id": self.ledger_id,
            "rows": [item.to_mapping() for item in self.rows],
        }


@dataclass(frozen=True, slots=True)
class CoverageAdmission(_CanonicalModel):
    admission_id: str
    ledger_ref: ArtifactRef
    coverage_mode: str
    next_action: str
    taint_seed_ids: tuple[str, ...]
    dependency_closure_hash: str

    def __post_init__(self) -> None:
        _identifier(self.admission_id, "admission_id")
        if type(self.ledger_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("ledger_ref must be an ArtifactRef")
        if self.coverage_mode not in {"strict_global", "dependency_scoped"}:
            raise ProductionModelError("coverage_mode is unknown")
        if self.next_action not in {"continue", "quarantine", "stop"}:
            raise ProductionModelError("next_action is unknown")
        seeds = _unique_ids(self.taint_seed_ids, "taint_seed_ids")
        if self.coverage_mode == "strict_global" and seeds and self.next_action == "continue":
            raise ProductionModelError("strict_global cannot continue with taint seeds")
        if self.next_action == "continue" and self.coverage_mode == "dependency_scoped":
            pass
        _sha256(self.dependency_closure_hash, "dependency_closure_hash")
        object.__setattr__(self, "taint_seed_ids", seeds)

    @classmethod
    def from_ledger(
        cls,
        *,
        admission_id: str,
        ledger_ref: ArtifactRef,
        ledger: CoverageLedger,
        coverage_mode: str,
        dependency_closure_hash: str,
    ) -> CoverageAdmission:
        if type(ledger) is not CoverageLedger:  # noqa: E721
            raise ProductionModelError("ledger must be a CoverageLedger")
        if ledger_ref.content_hash != ledger.canonical_hash:
            raise ProductionModelError("ledger_ref does not bind the exact ledger")
        seeds = tuple(seed for row in ledger.rows for seed in row.taint_seed_ids)
        next_action = "continue"
        if seeds and coverage_mode == "strict_global":
            next_action = "quarantine"
        return cls(
            admission_id,
            ledger_ref,
            coverage_mode,
            next_action,
            seeds,
            dependency_closure_hash,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "admission_id": self.admission_id,
            "coverage_mode": self.coverage_mode,
            "dependency_closure_hash": self.dependency_closure_hash,
            "kind": "coverage",
            "ledger_ref": self.ledger_ref.to_mapping(),
            "next_action": self.next_action,
            "taint_seed_ids": list(self.taint_seed_ids),
        }


@dataclass(frozen=True, slots=True)
class PhysicalRequirement(_CanonicalModel):
    requirement_kind: str
    mode: str

    def __post_init__(self) -> None:
        if _PHYSICAL_REQUIREMENTS.get(self.requirement_kind) != self.mode:
            raise ProductionModelError("physical requirement kind/mode is unknown")

    def to_mapping(self) -> dict[str, object]:
        return {"mode": self.mode, "requirement_kind": self.requirement_kind}


def _physical_tuple(
    values: Sequence[PhysicalRequirement], label: str
) -> tuple[PhysicalRequirement, ...]:
    result = cast(
        tuple[PhysicalRequirement, ...],
        _typed_tuple(values, PhysicalRequirement, label, nonempty=True),
    )
    kinds = tuple(item.requirement_kind for item in result)
    if len(kinds) != len(set(kinds)):
        raise ProductionModelError(f"{label} must not contain duplicates")
    if kinds != tuple(sorted(kinds, key=lambda item: item.encode("utf-8"))):
        raise ProductionModelError(f"{label} must use canonical requirement_kind order")
    return result


@dataclass(frozen=True, slots=True, init=False)
class Candidate(_CanonicalModel):
    candidate_id: str
    event_refs: tuple[DomainRef, ...]
    source_ref: DomainRef
    window_ref: DomainRef
    vlm_observation_ref: DomainRef
    vlm_observation_sha256: str
    capability_policy_ref: ArtifactRef
    editing_modes: tuple[EditingMode, ...]
    supported_narrative_functions: tuple[str, ...]
    usable_duration_seconds: int
    authorization_ref: DomainRef

    @classmethod
    def from_vlm_capability(
        cls,
        *,
        candidate_id: str,
        event_refs: Sequence[DomainRef],
        observation: OwnerBoundVlmObservationRef,
        supported_narrative_functions: Sequence[str],
        usable_duration_seconds: int,
        authorization_ref: DomainRef,
    ) -> Candidate:
        _identifier(candidate_id, "candidate_id")
        events = _unique_domain_refs(event_refs, "event_refs", nonempty=True)
        if any(item.object_type != "event" for item in events):
            raise ProductionModelError("event_refs must point to EventCard objects")
        if type(observation) is not OwnerBoundVlmObservationRef:  # noqa: E721
            raise ProductionModelError("observation must be an owner-bound VLM capability")
        functions = tuple(
            _text(item, "supported_narrative_functions") for item in supported_narrative_functions
        )
        if not functions or any(not _NARRATIVE_FUNCTION.fullmatch(item) for item in functions):
            raise ProductionModelError("supported_narrative_functions is invalid")
        if len(functions) != len(set(functions)) or functions != tuple(sorted(functions)):
            raise ProductionModelError("supported_narrative_functions must be unique and canonical")
        seconds = _integer(usable_duration_seconds, "usable_duration_seconds", minimum=1)
        if (
            type(authorization_ref) is not DomainRef
            or authorization_ref.object_type != "source_authorization"
        ):  # noqa: E721
            raise ProductionModelError("authorization_ref must be a source_authorization")
        instance = object.__new__(cls)
        object.__setattr__(instance, "candidate_id", candidate_id)
        object.__setattr__(instance, "event_refs", events)
        object.__setattr__(instance, "source_ref", observation.source_ref)
        object.__setattr__(instance, "window_ref", observation.window_ref)
        object.__setattr__(instance, "vlm_observation_ref", observation.observation_ref)
        object.__setattr__(instance, "vlm_observation_sha256", observation.vlm_observation_sha256)
        object.__setattr__(instance, "capability_policy_ref", observation.capability_policy_ref)
        object.__setattr__(instance, "editing_modes", observation.editing_modes)
        object.__setattr__(instance, "supported_narrative_functions", functions)
        object.__setattr__(instance, "usable_duration_seconds", seconds)
        object.__setattr__(instance, "authorization_ref", authorization_ref)
        return instance

    @classmethod
    def from_mapping(cls, value: object, *, observation: OwnerBoundVlmObservationRef) -> Candidate:
        item = _mapping(
            value,
            {
                "authorization_ref",
                "candidate_id",
                "capability_policy_ref",
                "editing_modes",
                "event_refs",
                "source_ref",
                "supported_narrative_functions",
                "usable_duration_seconds",
                "vlm_observation_ref",
                "vlm_observation_sha256",
                "window_ref",
            },
            "candidate",
        )
        if (
            item["editing_modes"] != [mode.value for mode in observation.editing_modes]
            or item["vlm_observation_sha256"] != observation.vlm_observation_sha256
            or item["vlm_observation_ref"] != observation.observation_ref.to_mapping()
            or item["source_ref"] != observation.source_ref.to_mapping()
            or item["window_ref"] != observation.window_ref.to_mapping()
            or item["capability_policy_ref"] != observation.capability_policy_ref.to_mapping()
        ):
            raise ProductionModelError("candidate VLM capability fields are a caller override")
        functions = tuple(
            _text(value, "supported_narrative_functions")
            for value in _object_list(
                item["supported_narrative_functions"], "supported_narrative_functions"
            )
        )
        candidate = cls.from_vlm_capability(
            candidate_id=_identifier(item["candidate_id"], "candidate_id"),
            event_refs=tuple(
                _domain_ref(member, "event_refs")
                for member in _object_list(item["event_refs"], "event_refs")
            ),
            observation=observation,
            supported_narrative_functions=functions,
            usable_duration_seconds=_integer(
                item["usable_duration_seconds"], "usable_duration_seconds", minimum=1
            ),
            authorization_ref=_domain_ref(item["authorization_ref"], "authorization_ref"),
        )
        if candidate.to_mapping() != value:
            raise ProductionModelError("candidate mapping is not canonical")
        return candidate

    def to_mapping(self) -> dict[str, object]:
        return {
            "authorization_ref": self.authorization_ref.to_mapping(),
            "candidate_id": self.candidate_id,
            "capability_policy_ref": self.capability_policy_ref.to_mapping(),
            "editing_modes": [mode.value for mode in self.editing_modes],
            "event_refs": [item.to_mapping() for item in self.event_refs],
            "source_ref": self.source_ref.to_mapping(),
            "supported_narrative_functions": list(self.supported_narrative_functions),
            "usable_duration_seconds": self.usable_duration_seconds,
            "vlm_observation_ref": self.vlm_observation_ref.to_mapping(),
            "vlm_observation_sha256": self.vlm_observation_sha256,
            "window_ref": self.window_ref.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class CandidateCatalog(_CanonicalModel):
    candidate_catalog_id: str
    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        _identifier(self.candidate_catalog_id, "candidate_catalog_id")
        candidates = cast(
            tuple[Candidate, ...],
            _typed_tuple(self.candidates, Candidate, "candidates", nonempty=True),
        )
        if len({item.candidate_id for item in candidates}) != len(candidates):
            raise ProductionModelError("candidates must have unique IDs")
        if tuple(sorted(candidates, key=lambda item: item.candidate_id)) != candidates:
            raise ProductionModelError("candidates must use canonical candidate_id order")
        object.__setattr__(self, "candidates", candidates)

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_catalog_id": self.candidate_catalog_id,
            "candidates": [item.to_mapping() for item in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class MaterialRequirement(_CanonicalModel):
    requirement_id: str
    obligation_ref: DomainRef
    minimum_usable_seconds: int
    physical_requirements: tuple[PhysicalRequirement, ...]
    allowed_source_refs: tuple[DomainRef, ...]
    forbidden_source_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        _identifier(self.requirement_id, "requirement_id")
        if (
            type(self.obligation_ref) is not DomainRef
            or self.obligation_ref.object_type != "obligation"
        ):  # noqa: E721
            raise ProductionModelError("obligation_ref must point to an obligation")
        _integer(self.minimum_usable_seconds, "minimum_usable_seconds", minimum=1)
        object.__setattr__(
            self,
            "physical_requirements",
            _physical_tuple(self.physical_requirements, "physical_requirements"),
        )
        allowed = _unique_domain_refs(self.allowed_source_refs, "allowed_source_refs")
        forbidden = _unique_domain_refs(self.forbidden_source_refs, "forbidden_source_refs")
        if any(item.object_type != "source" for item in (*allowed, *forbidden)):
            raise ProductionModelError("source constraints must point to Source objects")
        if set(map(_ref_key, allowed)) & set(map(_ref_key, forbidden)):
            raise ProductionModelError("allowed and forbidden sources must not overlap")
        object.__setattr__(self, "allowed_source_refs", allowed)
        object.__setattr__(self, "forbidden_source_refs", forbidden)

    @property
    def physical_requirements_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.physical_requirements])

    def to_mapping(self) -> dict[str, object]:
        return {
            "allowed_source_refs": [item.to_mapping() for item in self.allowed_source_refs],
            "forbidden_source_refs": [item.to_mapping() for item in self.forbidden_source_refs],
            "minimum_usable_seconds": self.minimum_usable_seconds,
            "obligation_ref": self.obligation_ref.to_mapping(),
            "physical_requirements": [item.to_mapping() for item in self.physical_requirements],
            "physical_requirements_hash": self.physical_requirements_hash,
            "requirement_id": self.requirement_id,
        }


@dataclass(frozen=True, slots=True)
class Proposal(_CanonicalModel):
    proposal_id: str
    story_id: str
    title: str
    narrative_claim: str
    thread_refs: tuple[DomainRef, ...]
    required_obligation_refs: tuple[DomainRef, ...]
    required_fact_refs: tuple[DomainRef, ...]
    key_character_refs: tuple[DomainRef, ...]
    genre_tags: tuple[str, ...]
    editing_profile: str
    target_duration_seconds: DurationRangeSeconds
    teaser_strategy: str
    material_requirements: tuple[MaterialRequirement, ...]
    candidate_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        _identifier(self.proposal_id, "proposal_id")
        _identifier(self.story_id, "story_id")
        _text(self.title, "title")
        _text(self.narrative_claim, "narrative_claim")
        ref_fields = (
            ("thread_refs", self.thread_refs, "story_thread", True),
            ("required_obligation_refs", self.required_obligation_refs, "obligation", True),
            ("required_fact_refs", self.required_fact_refs, "fact", True),
            ("key_character_refs", self.key_character_refs, "character", False),
            ("candidate_refs", self.candidate_refs, "candidate", True),
        )
        for name, values, object_type, nonempty in ref_fields:
            refs = _unique_domain_refs(values, name, nonempty=nonempty)
            if any(item.object_type != object_type for item in refs):
                raise ProductionModelError(f"{name} has the wrong object type")
            object.__setattr__(self, name, refs)
        tags = tuple(_identifier(item, "genre_tags") for item in self.genre_tags)
        if not tags or len(tags) != len(set(tags)) or tags != tuple(sorted(tags)):
            raise ProductionModelError("genre_tags must be non-empty, unique, and canonical")
        object.__setattr__(self, "genre_tags", tags)
        _identifier(self.editing_profile, "editing_profile")
        _identifier(self.teaser_strategy, "teaser_strategy")
        if type(self.target_duration_seconds) is not DurationRangeSeconds:  # noqa: E721
            raise ProductionModelError("target_duration_seconds must be a DurationRangeSeconds")
        requirements = cast(
            tuple[MaterialRequirement, ...],
            _typed_tuple(
                self.material_requirements,
                MaterialRequirement,
                "material_requirements",
                nonempty=True,
            ),
        )
        if len({item.requirement_id for item in requirements}) != len(requirements):
            raise ProductionModelError("material requirements must have unique IDs")
        object.__setattr__(self, "material_requirements", requirements)

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_refs": [item.to_mapping() for item in self.candidate_refs],
            "editing_profile": self.editing_profile,
            "genre_tags": list(self.genre_tags),
            "key_character_refs": [item.to_mapping() for item in self.key_character_refs],
            "material_requirements": [item.to_mapping() for item in self.material_requirements],
            "narrative_claim": self.narrative_claim,
            "proposal_id": self.proposal_id,
            "required_fact_refs": [item.to_mapping() for item in self.required_fact_refs],
            "required_obligation_refs": [
                item.to_mapping() for item in self.required_obligation_refs
            ],
            "story_id": self.story_id,
            "target_duration_seconds": self.target_duration_seconds.to_mapping(),
            "teaser_strategy": self.teaser_strategy,
            "thread_refs": [item.to_mapping() for item in self.thread_refs],
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class ProposalSet(_CanonicalModel):
    proposal_set_id: str
    proposals: tuple[Proposal, ...]

    def __post_init__(self) -> None:
        _identifier(self.proposal_set_id, "proposal_set_id")
        proposals = cast(
            tuple[Proposal, ...],
            _typed_tuple(self.proposals, Proposal, "proposals", nonempty=True),
        )
        if len({item.proposal_id for item in proposals}) != len(proposals):
            raise ProductionModelError("proposals must have unique IDs")
        if len({item.story_id for item in proposals}) != len(proposals):
            raise ProductionModelError("proposals must have unique story IDs")
        object.__setattr__(self, "proposals", proposals)

    def to_mapping(self) -> dict[str, object]:
        return {
            "proposal_set_id": self.proposal_set_id,
            "proposals": [item.to_mapping() for item in self.proposals],
        }


@dataclass(frozen=True, slots=True)
class PortfolioSelectionRecord(_CanonicalModel):
    story_id: str
    proposal_id: str
    proposal_index: int

    def __post_init__(self) -> None:
        _identifier(self.story_id, "story_id")
        _identifier(self.proposal_id, "proposal_id")
        _integer(self.proposal_index, "proposal_index")

    def to_mapping(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "proposal_index": self.proposal_index,
            "selected": True,
            "story_id": self.story_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class Portfolio(_CanonicalModel):
    portfolio_id: str
    proposal_set_ref: ArtifactRef
    completion_policy: str
    target_story_ids: tuple[str, ...]
    target_story_ids_hash: str
    selection_records: tuple[PortfolioSelectionRecord, ...]

    @classmethod
    def from_selected_proposals(
        cls,
        *,
        portfolio_id: str,
        proposal_set_ref: ArtifactRef,
        selected_proposals: Sequence[tuple[int, Proposal]],
        completion_policy: str,
    ) -> Portfolio:
        _identifier(portfolio_id, "portfolio_id")
        if type(proposal_set_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("proposal_set_ref must be an ArtifactRef")
        if completion_policy not in {"independent_outputs", "all_or_nothing"}:
            raise ProductionModelError("completion_policy is unknown")
        selected = tuple(selected_proposals)
        if not selected:
            raise ProductionModelError("selected_proposals must not be empty")
        indices = tuple(index for index, _ in selected)
        if any(type(index) is not int or index < 0 for index in indices):  # noqa: E721
            raise ProductionModelError("proposal indices must be non-negative integers")
        if tuple(sorted(indices)) != indices or len(indices) != len(set(indices)):
            raise ProductionModelError("selected proposal indices must be strictly increasing")
        if any(type(proposal) is not Proposal for _, proposal in selected):  # noqa: E721
            raise ProductionModelError("selected proposals must be Proposal values")
        records = tuple(
            PortfolioSelectionRecord(proposal.story_id, proposal.proposal_id, index)
            for index, proposal in selected
        )
        targets = tuple(item.story_id for item in records)
        if len(targets) != len(set(targets)):
            raise ProductionModelError("target story IDs must be unique")
        instance = object.__new__(cls)
        object.__setattr__(instance, "portfolio_id", portfolio_id)
        object.__setattr__(instance, "proposal_set_ref", proposal_set_ref)
        object.__setattr__(instance, "completion_policy", completion_policy)
        object.__setattr__(instance, "target_story_ids", targets)
        object.__setattr__(instance, "target_story_ids_hash", canonical_json_hash(list(targets)))
        object.__setattr__(instance, "selection_records", records)
        return instance

    def to_mapping(self) -> dict[str, object]:
        return {
            "completion_policy": self.completion_policy,
            "portfolio_id": self.portfolio_id,
            "proposal_set_ref": self.proposal_set_ref.to_mapping(),
            "selection_records": [item.to_mapping() for item in self.selection_records],
            "target_story_ids": list(self.target_story_ids),
            "target_story_ids_hash": self.target_story_ids_hash,
        }


@dataclass(frozen=True, slots=True, init=False)
class PortfolioAdmission(_CanonicalModel):
    admission_id: str
    portfolio_ref: ArtifactRef
    target_story_ids: tuple[str, ...]
    target_story_ids_hash: str
    source_usage_ledger_ref: ArtifactRef
    next_action: str

    @classmethod
    def from_portfolio(
        cls,
        *,
        admission_id: str,
        portfolio_ref: ArtifactRef,
        portfolio: Portfolio,
        source_usage_ledger_ref: ArtifactRef,
    ) -> PortfolioAdmission:
        _identifier(admission_id, "admission_id")
        if type(portfolio) is not Portfolio:  # noqa: E721
            raise ProductionModelError("portfolio must be a Portfolio")
        if (
            type(portfolio_ref) is not ArtifactRef
            or portfolio_ref.content_hash != portfolio.canonical_hash
        ):  # noqa: E721
            raise ProductionModelError("portfolio_ref must bind the exact Portfolio")
        if type(source_usage_ledger_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("source_usage_ledger_ref must be an ArtifactRef")
        instance = object.__new__(cls)
        object.__setattr__(instance, "admission_id", admission_id)
        object.__setattr__(instance, "portfolio_ref", portfolio_ref)
        object.__setattr__(instance, "target_story_ids", portfolio.target_story_ids)
        object.__setattr__(instance, "target_story_ids_hash", portfolio.target_story_ids_hash)
        object.__setattr__(instance, "source_usage_ledger_ref", source_usage_ledger_ref)
        object.__setattr__(instance, "next_action", "continue")
        return instance

    def to_mapping(self) -> dict[str, object]:
        return {
            "admission_id": self.admission_id,
            "kind": "portfolio",
            "next_action": self.next_action,
            "portfolio_ref": self.portfolio_ref.to_mapping(),
            "source_usage_ledger_ref": self.source_usage_ledger_ref.to_mapping(),
            "target_story_ids": list(self.target_story_ids),
            "target_story_ids_hash": self.target_story_ids_hash,
        }


@dataclass(frozen=True, slots=True)
class CandidateAlternative(_CanonicalModel):
    alternative_id: str
    event_refs: tuple[DomainRef, ...]
    candidate_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        _identifier(self.alternative_id, "alternative_id")
        events = _unique_domain_refs(self.event_refs, "event_refs", nonempty=True)
        candidates = _unique_domain_refs(self.candidate_refs, "candidate_refs", nonempty=True)
        if any(item.object_type != "event" for item in events):
            raise ProductionModelError("alternative event_refs must point to Events")
        if any(item.object_type != "candidate" for item in candidates):
            raise ProductionModelError("alternative candidate_refs must point to Candidates")
        object.__setattr__(self, "event_refs", events)
        object.__setattr__(self, "candidate_refs", candidates)

    def to_mapping(self) -> dict[str, object]:
        return {
            "alternative_id": self.alternative_id,
            "candidate_refs": [item.to_mapping() for item in self.candidate_refs],
            "event_refs": [item.to_mapping() for item in self.event_refs],
        }


@dataclass(frozen=True, slots=True)
class EvidenceRequirement(_CanonicalModel):
    requirement_id: str
    source_material_requirement_id: str
    satisfaction: str
    alternative_sets: tuple[CandidateAlternative, ...]
    physical_requirements: tuple[PhysicalRequirement, ...]
    required_candidate_refs: tuple[DomainRef, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.requirement_id, "requirement_id")
        _identifier(self.source_material_requirement_id, "source_material_requirement_id")
        if self.satisfaction not in {"one_of", "all_of"}:
            raise ProductionModelError("satisfaction is unknown")
        alternatives = cast(
            tuple[CandidateAlternative, ...],
            _typed_tuple(
                self.alternative_sets,
                CandidateAlternative,
                "alternative_sets",
                nonempty=True,
            ),
        )
        if len({item.alternative_id for item in alternatives}) != len(alternatives):
            raise ProductionModelError("alternative sets must have unique IDs")
        required = _unique_domain_refs(self.required_candidate_refs, "required_candidate_refs")
        if any(item.object_type != "candidate" for item in required):
            raise ProductionModelError("required_candidate_refs must point to Candidates")
        object.__setattr__(self, "alternative_sets", alternatives)
        object.__setattr__(self, "required_candidate_refs", required)
        object.__setattr__(
            self,
            "physical_requirements",
            _physical_tuple(self.physical_requirements, "physical_requirements"),
        )

    @property
    def physical_requirements_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.physical_requirements])

    def to_mapping(self) -> dict[str, object]:
        return {
            "alternative_sets": [item.to_mapping() for item in self.alternative_sets],
            "physical_requirements": [item.to_mapping() for item in self.physical_requirements],
            "physical_requirements_hash": self.physical_requirements_hash,
            "required_candidate_refs": [item.to_mapping() for item in self.required_candidate_refs],
            "requirement_id": self.requirement_id,
            "satisfaction": self.satisfaction,
            "source_material_requirement_id": self.source_material_requirement_id,
        }


@dataclass(frozen=True, slots=True)
class SpanPolicy(_CanonicalModel):
    preferred: str
    allowed: tuple[str, ...]
    fallback_order: tuple[str, ...]

    def __post_init__(self) -> None:
        allowed_values = {"tight", "scene", "context"}
        allowed = tuple(self.allowed)
        fallback = tuple(self.fallback_order)
        if not allowed or any(item not in allowed_values for item in allowed):
            raise ProductionModelError("span_policy.allowed contains an unknown value")
        if len(allowed) != len(set(allowed)):
            raise ProductionModelError("span_policy.allowed must be unique")
        if self.preferred not in allowed:
            raise ProductionModelError("span_policy.preferred must be allowed")
        if len(fallback) != len(set(fallback)) or set(fallback) != set(allowed):
            raise ProductionModelError("span_policy.fallback_order must permute allowed")
        object.__setattr__(self, "allowed", allowed)
        object.__setattr__(self, "fallback_order", fallback)

    @classmethod
    def from_mapping(cls, value: object) -> SpanPolicy:
        item = _mapping(value, {"preferred", "allowed", "fallback_order"}, "span_policy")
        return cls(
            _text(item["preferred"], "span_policy.preferred"),
            tuple(
                _text(member, "span_policy.allowed")
                for member in _object_list(item["allowed"], "span_policy.allowed")
            ),
            tuple(
                _text(member, "span_policy.fallback_order")
                for member in _object_list(item["fallback_order"], "span_policy.fallback_order")
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "allowed": list(self.allowed),
            "fallback_order": list(self.fallback_order),
            "preferred": self.preferred,
        }


@dataclass(frozen=True, slots=True)
class BlueprintBeat(_CanonicalModel):
    blueprint_beat_id: str
    narrative_role: str
    narrative_function: str
    summary: str
    required_obligation_refs: tuple[DomainRef, ...]
    required_fact_refs: tuple[DomainRef, ...]
    evidence_requirements: tuple[EvidenceRequirement, ...]
    candidate_preferences: tuple[DomainRef, ...]
    span_policy: SpanPolicy
    duration_seconds: DurationRangeSeconds

    def __post_init__(self) -> None:
        _identifier(self.blueprint_beat_id, "blueprint_beat_id")
        if self.narrative_role not in {
            "setup",
            "escalation",
            "turn",
            "reveal",
            "payoff",
            "consequence",
            "coda",
        }:
            raise ProductionModelError("narrative_role is unknown")
        if not _NARRATIVE_FUNCTION.fullmatch(self.narrative_function):
            raise ProductionModelError("narrative_function is invalid")
        _text(self.summary, "summary")
        obligations = _unique_domain_refs(
            self.required_obligation_refs, "required_obligation_refs", nonempty=True
        )
        facts = _unique_domain_refs(self.required_fact_refs, "required_fact_refs", nonempty=True)
        if any(item.object_type != "obligation" for item in obligations):
            raise ProductionModelError("required_obligation_refs must point to obligations")
        if any(item.object_type != "fact" for item in facts):
            raise ProductionModelError("required_fact_refs must point to facts")
        requirements = cast(
            tuple[EvidenceRequirement, ...],
            _typed_tuple(
                self.evidence_requirements,
                EvidenceRequirement,
                "evidence_requirements",
                nonempty=True,
            ),
        )
        if len({item.requirement_id for item in requirements}) != len(requirements):
            raise ProductionModelError("evidence requirements must have unique IDs")
        preferences = _unique_domain_refs(self.candidate_preferences, "candidate_preferences")
        alternative_keys = {
            canonical_json_hash(ref.to_mapping())
            for requirement in requirements
            for alternative in requirement.alternative_sets
            for ref in alternative.candidate_refs
        }
        if any(
            canonical_json_hash(item.to_mapping()) not in alternative_keys for item in preferences
        ):
            raise ProductionModelError("candidate preferences must be legal alternatives")
        if type(self.span_policy) is not SpanPolicy:  # noqa: E721
            raise ProductionModelError("span_policy must be a SpanPolicy")
        if type(self.duration_seconds) is not DurationRangeSeconds:  # noqa: E721
            raise ProductionModelError("duration_seconds must be a DurationRangeSeconds")
        object.__setattr__(self, "required_obligation_refs", obligations)
        object.__setattr__(self, "required_fact_refs", facts)
        object.__setattr__(self, "evidence_requirements", requirements)
        object.__setattr__(self, "candidate_preferences", preferences)

    def to_mapping(self) -> dict[str, object]:
        return {
            "blueprint_beat_id": self.blueprint_beat_id,
            "candidate_preferences": [item.to_mapping() for item in self.candidate_preferences],
            "duration_seconds": self.duration_seconds.to_mapping(),
            "evidence_requirements": [item.to_mapping() for item in self.evidence_requirements],
            "narrative_function": self.narrative_function,
            "narrative_role": self.narrative_role,
            "required_fact_refs": [item.to_mapping() for item in self.required_fact_refs],
            "required_obligation_refs": [
                item.to_mapping() for item in self.required_obligation_refs
            ],
            "span_policy": self.span_policy.to_mapping(),
            "summary": self.summary,
        }


def _physical_from_mapping(value: object, label: str) -> PhysicalRequirement:
    item = _mapping(value, {"requirement_kind", "mode"}, label)
    return PhysicalRequirement(
        _text(item["requirement_kind"], f"{label}.requirement_kind"),
        _text(item["mode"], f"{label}.mode"),
    )


def _alternative_from_mapping(value: object) -> CandidateAlternative:
    item = _mapping(value, {"alternative_id", "event_refs", "candidate_refs"}, "alternative")
    return CandidateAlternative(
        _identifier(item["alternative_id"], "alternative_id"),
        tuple(
            _domain_ref(member, "event_refs")
            for member in _object_list(item["event_refs"], "event_refs")
        ),
        tuple(
            _domain_ref(member, "candidate_refs")
            for member in _object_list(item["candidate_refs"], "candidate_refs")
        ),
    )


def _requirement_from_mapping(value: object) -> EvidenceRequirement:
    item = _mapping(
        value,
        {
            "alternative_sets",
            "physical_requirements",
            "physical_requirements_hash",
            "required_candidate_refs",
            "requirement_id",
            "satisfaction",
            "source_material_requirement_id",
        },
        "evidence_requirement",
    )
    requirement = EvidenceRequirement(
        requirement_id=_identifier(item["requirement_id"], "requirement_id"),
        source_material_requirement_id=_identifier(
            item["source_material_requirement_id"], "source_material_requirement_id"
        ),
        satisfaction=_text(item["satisfaction"], "satisfaction"),
        alternative_sets=tuple(
            _alternative_from_mapping(member)
            for member in _object_list(item["alternative_sets"], "alternative_sets")
        ),
        physical_requirements=tuple(
            _physical_from_mapping(member, "physical_requirements")
            for member in _object_list(item["physical_requirements"], "physical_requirements")
        ),
        required_candidate_refs=tuple(
            _domain_ref(member, "required_candidate_refs")
            for member in _object_list(item["required_candidate_refs"], "required_candidate_refs")
        ),
    )
    if item["physical_requirements_hash"] != requirement.physical_requirements_hash:
        raise ProductionModelError("physical_requirements_hash does not match")
    return requirement


def _beat_from_mapping(value: object) -> BlueprintBeat:
    item = _mapping(
        value,
        {
            "blueprint_beat_id",
            "candidate_preferences",
            "duration_seconds",
            "evidence_requirements",
            "narrative_function",
            "narrative_role",
            "required_fact_refs",
            "required_obligation_refs",
            "span_policy",
            "summary",
        },
        "blueprint_beat",
    )
    return BlueprintBeat(
        blueprint_beat_id=_identifier(item["blueprint_beat_id"], "blueprint_beat_id"),
        narrative_role=_text(item["narrative_role"], "narrative_role"),
        narrative_function=_text(item["narrative_function"], "narrative_function"),
        summary=_text(item["summary"], "summary"),
        required_obligation_refs=tuple(
            _domain_ref(member, "required_obligation_refs")
            for member in _object_list(item["required_obligation_refs"], "required_obligation_refs")
        ),
        required_fact_refs=tuple(
            _domain_ref(member, "required_fact_refs")
            for member in _object_list(item["required_fact_refs"], "required_fact_refs")
        ),
        evidence_requirements=tuple(
            _requirement_from_mapping(member)
            for member in _object_list(item["evidence_requirements"], "evidence_requirements")
        ),
        candidate_preferences=tuple(
            _domain_ref(member, "candidate_preferences")
            for member in _object_list(item["candidate_preferences"], "candidate_preferences")
        ),
        span_policy=SpanPolicy.from_mapping(item["span_policy"]),
        duration_seconds=DurationRangeSeconds.from_mapping(item["duration_seconds"]),
    )


@dataclass(frozen=True, slots=True)
class EditorialBlueprint(_CanonicalModel):
    blueprint_id: str
    story_id: str
    proposal_ref: DomainRef
    beats: tuple[BlueprintBeat, ...]
    story_duration_seconds: DurationRangeSeconds
    pacing: str
    continuity_priority: str
    teaser_strategy: str
    teaser_duration_seconds: DurationRangeSeconds

    def __post_init__(self) -> None:
        _identifier(self.blueprint_id, "blueprint_id")
        _identifier(self.story_id, "story_id")
        if type(self.proposal_ref) is not DomainRef or self.proposal_ref.object_type != "proposal":  # noqa: E721
            raise ProductionModelError("proposal_ref must point to a Proposal")
        beats = cast(
            tuple[BlueprintBeat, ...],
            _typed_tuple(self.beats, BlueprintBeat, "beats", nonempty=True),
        )
        if len({item.blueprint_beat_id for item in beats}) != len(beats):
            raise ProductionModelError("beats must have unique IDs")
        if type(self.story_duration_seconds) is not DurationRangeSeconds:  # noqa: E721
            raise ProductionModelError("story_duration_seconds is invalid")
        if self.pacing not in {"slow", "balanced", "fast"}:
            raise ProductionModelError("pacing is unknown")
        if self.continuity_priority not in {"low", "medium", "high"}:
            raise ProductionModelError("continuity_priority is unknown")
        _identifier(self.teaser_strategy, "teaser_strategy")
        if type(self.teaser_duration_seconds) is not DurationRangeSeconds:  # noqa: E721
            raise ProductionModelError("teaser_duration_seconds is invalid")
        object.__setattr__(self, "beats", beats)

    @property
    def required_obligation_refs(self) -> tuple[DomainRef, ...]:
        by_hash = {
            canonical_json_hash(reference.to_mapping()): reference
            for beat in self.beats
            for reference in beat.required_obligation_refs
        }
        return tuple(by_hash[key] for key in sorted(by_hash))

    @classmethod
    def from_mapping(cls, value: object) -> EditorialBlueprint:
        item = _mapping(
            value,
            {
                "beats",
                "blueprint_id",
                "editing_intent",
                "ordering_constraints",
                "proposal_ref",
                "story_duration_seconds",
                "story_id",
                "teaser_intent",
            },
            "editorial_blueprint",
        )
        ordering = _object_list(item["ordering_constraints"], "ordering_constraints")
        if ordering:
            raise ProductionModelError(
                "ordering constraints require the Stage 3 compiler-owned ordering model"
            )
        editing = _mapping(
            item["editing_intent"], {"pacing", "continuity_priority"}, "editing_intent"
        )
        teaser = _mapping(item["teaser_intent"], {"strategy", "duration_seconds"}, "teaser_intent")
        blueprint = cls(
            blueprint_id=_identifier(item["blueprint_id"], "blueprint_id"),
            story_id=_identifier(item["story_id"], "story_id"),
            proposal_ref=_domain_ref(item["proposal_ref"], "proposal_ref"),
            beats=tuple(
                _beat_from_mapping(member) for member in _object_list(item["beats"], "beats")
            ),
            story_duration_seconds=DurationRangeSeconds.from_mapping(
                item["story_duration_seconds"]
            ),
            pacing=_text(editing["pacing"], "editing_intent.pacing"),
            continuity_priority=_text(
                editing["continuity_priority"], "editing_intent.continuity_priority"
            ),
            teaser_strategy=_text(teaser["strategy"], "teaser_intent.strategy"),
            teaser_duration_seconds=DurationRangeSeconds.from_mapping(teaser["duration_seconds"]),
        )
        if blueprint.to_mapping() != value:
            raise ProductionModelError("editorial_blueprint mapping is not canonical")
        return blueprint

    def to_mapping(self) -> dict[str, object]:
        return {
            "beats": [item.to_mapping() for item in self.beats],
            "blueprint_id": self.blueprint_id,
            "editing_intent": {
                "continuity_priority": self.continuity_priority,
                "pacing": self.pacing,
            },
            "ordering_constraints": [],
            "proposal_ref": self.proposal_ref.to_mapping(),
            "story_duration_seconds": self.story_duration_seconds.to_mapping(),
            "story_id": self.story_id,
            "teaser_intent": {
                "duration_seconds": self.teaser_duration_seconds.to_mapping(),
                "strategy": self.teaser_strategy,
            },
        }


@dataclass(frozen=True, slots=True)
class EvidenceClosureMember(_CanonicalModel):
    kind: str
    source_ref: DomainRef
    object_content_hash: str

    def __post_init__(self) -> None:
        allowed = {
            "narrative_node",
            "fact",
            "event",
            "vlm_observation",
            "character_state",
            "candidate_metadata",
        }
        if self.kind not in allowed:
            raise ProductionModelError("evidence closure member kind is unknown")
        if type(self.source_ref) is not DomainRef:  # noqa: E721
            raise ProductionModelError("source_ref must be a DomainRef")
        if (
            self.kind in {"fact", "event", "vlm_observation"}
            and self.source_ref.object_type != self.kind
        ):
            raise ProductionModelError("closure member kind does not match source_ref")
        _sha256(self.object_content_hash, "object_content_hash")

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "object_content_hash": self.object_content_hash,
            "source_ref": self.source_ref.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class EvidenceClosure(_CanonicalModel):
    closure_id: str
    requirement_id: str
    members: tuple[EvidenceClosureMember, ...]
    dependency_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        _identifier(self.closure_id, "closure_id")
        _identifier(self.requirement_id, "requirement_id")
        members = cast(
            tuple[EvidenceClosureMember, ...],
            _typed_tuple(self.members, EvidenceClosureMember, "members", nonempty=True),
        )
        member_keys = tuple(canonical_json_hash(item.to_mapping()) for item in members)
        if len(member_keys) != len(set(member_keys)):
            raise ProductionModelError("closure members must not contain duplicates")
        object.__setattr__(self, "members", members)
        object.__setattr__(
            self,
            "dependency_refs",
            _unique_domain_refs(self.dependency_refs, "dependency_refs"),
        )

    @property
    def closure_hash(self) -> str:
        return canonical_json_hash(
            {
                "dependency_refs": [item.to_mapping() for item in self.dependency_refs],
                "members": [item.to_mapping() for item in self.members],
                "requirement_id": self.requirement_id,
            }
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "closure_hash": self.closure_hash,
            "closure_id": self.closure_id,
            "dependency_refs": [item.to_mapping() for item in self.dependency_refs],
            "members": [item.to_mapping() for item in self.members],
            "requirement_id": self.requirement_id,
        }


@dataclass(frozen=True, slots=True)
class EvidenceClosureSet(_CanonicalModel):
    evidence_closure_set_id: str
    story_id: str
    closures: tuple[EvidenceClosure, ...]

    def __post_init__(self) -> None:
        _identifier(self.evidence_closure_set_id, "evidence_closure_set_id")
        _identifier(self.story_id, "story_id")
        closures = cast(
            tuple[EvidenceClosure, ...],
            _typed_tuple(self.closures, EvidenceClosure, "closures", nonempty=True),
        )
        if len({item.closure_id for item in closures}) != len(closures):
            raise ProductionModelError("closures must have unique IDs")
        if len({item.requirement_id for item in closures}) != len(closures):
            raise ProductionModelError("each requirement must have exactly one closure")
        if tuple(sorted(closures, key=lambda item: item.closure_id)) != closures:
            raise ProductionModelError("closures must use canonical closure_id order")
        object.__setattr__(self, "closures", closures)

    @property
    def closure_set_hash(self) -> str:
        return canonical_json_hash(
            [
                {"closure_hash": item.closure_hash, "closure_id": item.closure_id}
                for item in self.closures
            ]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "closure_set_hash": self.closure_set_hash,
            "closures": [item.to_mapping() for item in self.closures],
            "evidence_closure_set_id": self.evidence_closure_set_id,
            "story_id": self.story_id,
        }


@dataclass(frozen=True, slots=True)
class RequiredClosure(_CanonicalModel):
    closure_id: str
    closure_hash: str

    def __post_init__(self) -> None:
        _identifier(self.closure_id, "closure_id")
        _sha256(self.closure_hash, "closure_hash")

    def to_mapping(self) -> dict[str, object]:
        return {"closure_hash": self.closure_hash, "closure_id": self.closure_id}


@dataclass(frozen=True, slots=True)
class ContextBudget(_CanonicalModel):
    unit: str
    limit: int
    used: int
    tokenizer_id: str
    tokenizer_version: str

    def __post_init__(self) -> None:
        if self.unit != "tokens":
            raise ProductionModelError("context budget unit must be tokens")
        limit = _integer(self.limit, "budget.limit", minimum=1)
        used = _integer(self.used, "budget.used")
        if used > limit:
            raise ProductionModelError("context budget used must not exceed limit")
        _identifier(self.tokenizer_id, "tokenizer_id")
        _safe_token(self.tokenizer_version, "tokenizer_version")

    def to_mapping(self) -> dict[str, object]:
        return {
            "limit": self.limit,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_version": self.tokenizer_version,
            "unit": self.unit,
            "used": self.used,
        }


@dataclass(frozen=True, slots=True)
class ContextOmission(_CanonicalModel):
    ref: DomainRef
    reason: str
    semantic_impact: str

    def __post_init__(self) -> None:
        if type(self.ref) is not DomainRef:  # noqa: E721
            raise ProductionModelError("omission.ref must be a DomainRef")
        if self.reason != "optional_priority_cut":
            raise ProductionModelError("omission reason is unknown")
        if self.semantic_impact != "none":
            raise ProductionModelError("required semantic context cannot be omitted")

    def to_mapping(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "ref": self.ref.to_mapping(),
            "semantic_impact": self.semantic_impact,
        }


@dataclass(frozen=True, slots=True)
class ContextManifest(_CanonicalModel):
    context_manifest_id: str
    story_id: str
    input_refs: tuple[ArtifactRef, ...]
    evidence_closure_set_ref: ArtifactRef
    required_closures: tuple[RequiredClosure, ...]
    optional_context_refs: tuple[DomainRef, ...]
    omissions: tuple[ContextOmission, ...]
    budget: ContextBudget
    builder_version: str

    def __post_init__(self) -> None:
        _identifier(self.context_manifest_id, "context_manifest_id")
        _identifier(self.story_id, "story_id")
        inputs = cast(
            tuple[ArtifactRef, ...],
            _typed_tuple(self.input_refs, ArtifactRef, "input_refs", nonempty=True),
        )
        if len({canonical_json_hash(item.to_mapping()) for item in inputs}) != len(inputs):
            raise ProductionModelError("input_refs must not contain duplicates")
        if type(self.evidence_closure_set_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("evidence_closure_set_ref must be an ArtifactRef")
        required = cast(
            tuple[RequiredClosure, ...],
            _typed_tuple(
                self.required_closures, RequiredClosure, "required_closures", nonempty=True
            ),
        )
        if len({item.closure_id for item in required}) != len(required):
            raise ProductionModelError("required_closures must not contain duplicates")
        omissions = cast(
            tuple[ContextOmission, ...],
            _typed_tuple(self.omissions, ContextOmission, "omissions"),
        )
        if type(self.budget) is not ContextBudget:  # noqa: E721
            raise ProductionModelError("budget must be a ContextBudget")
        _safe_token(self.builder_version, "builder_version")
        object.__setattr__(self, "input_refs", inputs)
        object.__setattr__(self, "required_closures", required)
        object.__setattr__(
            self,
            "optional_context_refs",
            _unique_domain_refs(self.optional_context_refs, "optional_context_refs"),
        )
        object.__setattr__(self, "omissions", omissions)

    def to_mapping(self) -> dict[str, object]:
        return {
            "budget": self.budget.to_mapping(),
            "builder_version": self.builder_version,
            "context_manifest_id": self.context_manifest_id,
            "evidence_closure_set_ref": self.evidence_closure_set_ref.to_mapping(),
            "input_refs": [item.to_mapping() for item in self.input_refs],
            "omissions": [item.to_mapping() for item in self.omissions],
            "optional_context_refs": [item.to_mapping() for item in self.optional_context_refs],
            "required_closures": [item.to_mapping() for item in self.required_closures],
            "story_id": self.story_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class SemanticFeasibilityAdmission(_CanonicalModel):
    admission_id: str
    story_id: str
    blueprint_ref: ArtifactRef
    evidence_closure_set_ref: ArtifactRef
    context_manifest_ref: ArtifactRef
    required_obligations_hash: str
    next_action: str

    @classmethod
    def from_artifacts(
        cls,
        *,
        admission_id: str,
        story_id: str,
        blueprint_ref: ArtifactRef,
        evidence_closure_set_ref: ArtifactRef,
        context_manifest_ref: ArtifactRef,
        required_obligation_refs: Sequence[DomainRef],
    ) -> SemanticFeasibilityAdmission:
        _identifier(admission_id, "admission_id")
        _identifier(story_id, "story_id")
        for label, reference in (
            ("blueprint_ref", blueprint_ref),
            ("evidence_closure_set_ref", evidence_closure_set_ref),
            ("context_manifest_ref", context_manifest_ref),
        ):
            if type(reference) is not ArtifactRef:  # noqa: E721
                raise ProductionModelError(f"{label} must be an ArtifactRef")
        obligations = _unique_domain_refs(
            required_obligation_refs, "required_obligation_refs", nonempty=True
        )
        if any(item.object_type != "obligation" for item in obligations):
            raise ProductionModelError("required obligations must point to obligation objects")
        instance = object.__new__(cls)
        object.__setattr__(instance, "admission_id", admission_id)
        object.__setattr__(instance, "story_id", story_id)
        object.__setattr__(instance, "blueprint_ref", blueprint_ref)
        object.__setattr__(instance, "evidence_closure_set_ref", evidence_closure_set_ref)
        object.__setattr__(instance, "context_manifest_ref", context_manifest_ref)
        object.__setattr__(
            instance,
            "required_obligations_hash",
            canonical_json_hash([item.to_mapping() for item in obligations]),
        )
        object.__setattr__(instance, "next_action", "continue")
        return instance

    def to_mapping(self) -> dict[str, object]:
        return {
            "admission_id": self.admission_id,
            "blueprint_ref": self.blueprint_ref.to_mapping(),
            "context_manifest_ref": self.context_manifest_ref.to_mapping(),
            "evidence_closure_set_ref": self.evidence_closure_set_ref.to_mapping(),
            "kind": "semantic_feasibility",
            "next_action": self.next_action,
            "required_obligations_hash": self.required_obligations_hash,
            "story_id": self.story_id,
        }


__all__ = [
    "BlueprintBeat",
    "Candidate",
    "CandidateAlternative",
    "CandidateCatalog",
    "ContextBudget",
    "ContextManifest",
    "ContextOmission",
    "CoverageAdmission",
    "CoverageConservation",
    "CoverageDisposition",
    "CoverageLedger",
    "CoverageResolution",
    "CoverageRow",
    "CoverageUnitType",
    "DurationRangeSeconds",
    "EditingMode",
    "EditorialBlueprint",
    "EpisodeDigest",
    "EpisodeDigestSet",
    "EventCard",
    "EventCardSet",
    "EvidenceClosure",
    "EvidenceClosureMember",
    "EvidenceClosureSet",
    "EvidenceRequirement",
    "MaterialRequirement",
    "NarrativeEdge",
    "NarrativeGraph",
    "NarrativeNode",
    "NarrativeNodeType",
    "OwnerBoundVlmObservationRef",
    "PhysicalRequirement",
    "Portfolio",
    "PortfolioAdmission",
    "PortfolioSelectionRecord",
    "ProductionModelError",
    "Proposal",
    "ProposalSet",
    "RequiredClosure",
    "SemanticFeasibilityAdmission",
    "SpanPolicy",
]
