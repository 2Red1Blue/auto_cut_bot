"""Closed Stage 2 CandidateCatalog values, without portfolio admission.

Candidate observations are copied from exact committed VLM packs.  These
values preserve provenance and semantic capability; they do not assert that a
candidate is physically editable or authorize any later Stage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, TypeVar, cast

from ..contracts.compiler.canonical import canonical_json_hash
from ..media.types import TickRange, TimeBase
from ..vlm.models import (
    MappedSourceInterval,
    VlmCandidateTag,
    VlmEditingMode,
    VlmMeasurementKind,
    VlmNarrativeFunction,
    VlmProxyInterval,
    VlmSemanticSupport,
)
from ..vlm.semantic_support_v4 import (
    FrameAnchoredObservationSupportV4,
    VideoObservationSupportV4,
    frame_aliases,
)
from .candidate_duration import ConservativeDuration
from .member_refs import SemanticMemberIdentity, SemanticObjectRef

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DECIMAL = re.compile(r"(?:0|1|0\.[0-9]*[1-9])\Z")
_EDITING_MODES = tuple(item.value for item in VlmEditingMode)
_NARRATIVE_FUNCTIONS = tuple(item.value for item in VlmNarrativeFunction)
_TAGS = tuple(item.value for item in VlmCandidateTag)
_MEASUREMENT_KINDS = tuple(item.value for item in VlmMeasurementKind)
_T = TypeVar("_T")
_SAFE = 2**53 - 1


class CandidateCatalogError(ValueError):
    """A CandidateCatalog value has malformed or non-closed provenance."""


def _closed(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise CandidateCatalogError("candidate value must be a closed mapping")
    mapping = cast(dict[str, object], value)
    if set(mapping) != set(keys):
        raise CandidateCatalogError("candidate value must be a closed mapping")
    return mapping


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise CandidateCatalogError(f"{label} must be non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CandidateCatalogError(f"{label} must be valid UTF-8") from error
    return value


def _hash(value: object, label: str) -> str:
    result = _text(value, label)
    if _SHA256.fullmatch(result) is None:
        raise CandidateCatalogError(f"{label} must be lowercase sha256")
    return result


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _SAFE:  # noqa: E721
        raise CandidateCatalogError(f"{label} must be an exact JSON-safe integer")
    return value


def _decimal(value: object, label: str) -> str:
    raw = _text(value, label)
    if len(raw) > 80 or _DECIMAL.fullmatch(raw) is None:
        raise CandidateCatalogError(f"{label} must be canonical decimal text")
    return raw


def candidate_confidence_text(value: object, label: str) -> str:
    """Bound and canonically render a VLM confidence without exponent expansion."""
    if (type(value) is not Decimal or not value.is_finite()  # noqa: E721
            or not 0 <= value <= 1 or len(value.as_tuple().digits) > 80
            or value.adjusted() < -78):
        raise CandidateCatalogError(f"{label} must be a bounded finite Decimal")
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    if raw == "-0":
        raw = "0"
    return _decimal(raw, label)


def _array(value: object, convert: Callable[[object], _T], label: str) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise CandidateCatalogError(f"{label} must be an array")
    return tuple(convert(item) for item in cast(list[object], value))


def _enum_tuple(value: object, allowed: tuple[str, ...], label: str) -> tuple[str, ...]:
    if type(value) is not tuple:  # noqa: E721
        raise CandidateCatalogError(f"{label} must be an actual tuple")
    items = cast(tuple[object, ...], value)
    items = tuple(_text(item, label) for item in items)
    if not items or len(set(items)) != len(items) or any(item not in allowed for item in items):
        raise CandidateCatalogError(f"{label} must be a non-empty unique closed enum collection")
    expected = tuple(item for item in allowed if item in items)
    if items != expected:
        raise CandidateCatalogError(f"{label} must use canonical enum order")
    return items


def _identity(value: object, label: str) -> SemanticMemberIdentity:
    try:
        return SemanticMemberIdentity.from_mapping(value)
    except ValueError as error:
        raise CandidateCatalogError(f"{label} is invalid") from error


def _ref(value: object, label: str) -> SemanticObjectRef:
    try:
        return SemanticObjectRef.from_mapping(value)
    except ValueError as error:
        raise CandidateCatalogError(f"{label} is invalid") from error


def _owner(ref: SemanticObjectRef, artifact_type: str, object_type: str, label: str) -> None:
    if type(ref) is not SemanticObjectRef or (  # noqa: E721
        ref.member_ref.artifact_type != artifact_type or ref.object_type != object_type
    ):
        raise CandidateCatalogError(f"{label} has an invalid owner")


@dataclass(frozen=True, slots=True)
class CandidateCatalogPolicy:
    strategy_version: str
    minimum_confidence: str
    required_measurement_kinds: tuple[str, ...]

    def __post_init__(self) -> None:
        if _text(self.strategy_version, "strategy version") != "candidate-catalog-v1":
            raise CandidateCatalogError("candidate catalog strategy is unsupported")
        object.__setattr__(self, "minimum_confidence", _decimal(self.minimum_confidence, "minimum_confidence"))
        values = self.required_measurement_kinds
        if type(values) is not tuple:  # noqa: E721
            raise CandidateCatalogError("required_measurement_kinds must be a tuple")
        canonical = _enum_tuple(values, _MEASUREMENT_KINDS, "required_measurement_kinds") if values else ()
        object.__setattr__(self, "required_measurement_kinds", canonical)

    def to_mapping(self) -> dict[str, object]:
        return {
            "strategy_version": self.strategy_version,
            "minimum_confidence": self.minimum_confidence,
            "required_measurement_kinds": list(self.required_measurement_kinds),
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateCatalogPolicy:
        item = _closed(value, ("strategy_version", "minimum_confidence", "required_measurement_kinds"))
        return cls(
            _text(item["strategy_version"], "strategy_version"),
            _decimal(item["minimum_confidence"], "minimum_confidence"),
            _array(item["required_measurement_kinds"], lambda entry: _text(entry, "required_measurement_kinds"), "required_measurement_kinds"),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class CandidateSupport:
    proxy_interval: VlmProxyInterval
    source_interval: MappedSourceInterval
    supporting_frame_ids: tuple[str, ...]
    confidence: str
    core_owner_window_manifest_sha256: str
    conservative_duration: ConservativeDuration

    def __post_init__(self) -> None:
        if type(self.proxy_interval) is not VlmProxyInterval or type(self.source_interval) is not MappedSourceInterval:  # noqa: E721
            raise CandidateCatalogError("candidate support timing must be exact VLM values")
        for interval in (self.proxy_interval.proxy_range, self.source_interval.coarse_range):
            _integer(interval.start_pts, "support start", minimum=-_SAFE)
            _integer(interval.end_pts, "support end", minimum=-_SAFE)
        for uncertainty in (self.proxy_interval.uncertainty_pts,
                            self.source_interval.provider_uncertainty_proxy_pts,
                            self.source_interval.mapping_error_bound_source_pts):
            _integer(uncertainty, "support uncertainty")
        if self.proxy_interval.uncertainty_pts != self.source_interval.provider_uncertainty_proxy_pts:
            raise CandidateCatalogError("support provider uncertainty differs between clocks")
        for base in (self.source_interval.source_time_base, self.source_interval.proxy_time_base):
            _integer(base.numerator, "support time base numerator", minimum=1)
            _integer(base.denominator, "support time base denominator", minimum=1)
        if type(self.supporting_frame_ids) is not tuple or not self.supporting_frame_ids:  # noqa: E721
            raise CandidateCatalogError("candidate support requires frame IDs")
        if len(set(self.supporting_frame_ids)) != len(self.supporting_frame_ids):
            raise CandidateCatalogError("candidate support frame IDs must be unique")
        for frame in self.supporting_frame_ids:
            _hash(frame, "candidate support frame ID")
        if self.supporting_frame_ids != tuple(sorted(self.supporting_frame_ids)):
            raise CandidateCatalogError("candidate support frame IDs must be canonical")
        object.__setattr__(self, "confidence", _decimal(self.confidence, "candidate support confidence"))
        object.__setattr__(self, "core_owner_window_manifest_sha256", _hash(
            self.core_owner_window_manifest_sha256, "candidate support owner window"
        ))
        if type(self.conservative_duration) is not ConservativeDuration:  # noqa: E721
            raise CandidateCatalogError("candidate support duration must be exact")

    @classmethod
    def from_vlm_support(cls, value: VlmSemanticSupport, duration: ConservativeDuration) -> CandidateSupport:
        if type(value) is VlmSemanticSupport:  # noqa: E721
            frame_ids = value.supporting_frame_ids
        elif isinstance(value, (VideoObservationSupportV4, FrameAnchoredObservationSupportV4)):
            if isinstance(value, FrameAnchoredObservationSupportV4):
                frame_ids = tuple(sorted(set(anchor.frame_sha256 for anchor in value.frame_anchors)))
            else:
                table = frame_aliases(value.manifest)
                frame_ids = tuple(sorted(entry.frame_sha256 for entry in table.entries))
        else:
            raise CandidateCatalogError("candidate support must be an exact VLM support")
        return cls(
            value.proxy_interval, value.source_interval, frame_ids,
            candidate_confidence_text(value.confidence, "candidate support confidence"), value.core_owner_window_manifest_sha256, duration,
        )

    def to_mapping(self) -> dict[str, object]:
        proxy = self.proxy_interval.proxy_range
        interval = self.source_interval
        return {
            "proxy_interval": {"start_pts": proxy.start_pts, "end_pts": proxy.end_pts, "uncertainty_pts": self.proxy_interval.uncertainty_pts},
            "source_interval": interval.to_mapping(),
            "supporting_frame_ids": list(self.supporting_frame_ids),
            "confidence": self.confidence,
            "core_owner_window_manifest_sha256": self.core_owner_window_manifest_sha256,
            "conservative_duration": self.conservative_duration.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateSupport:
        item = _closed(value, (
            "proxy_interval", "source_interval", "supporting_frame_ids", "confidence",
            "core_owner_window_manifest_sha256", "conservative_duration",
        ))
        proxy_raw = _closed(item["proxy_interval"], ("start_pts", "end_pts", "uncertainty_pts"))
        source = _mapped_source_interval(item["source_interval"])
        proxy = VlmProxyInterval(
            TickRange(_integer(proxy_raw["start_pts"], "proxy start", minimum=-_SAFE), _integer(proxy_raw["end_pts"], "proxy end", minimum=-_SAFE)),
            _integer(proxy_raw["uncertainty_pts"], "proxy uncertainty"),
        )
        return cls(
            proxy, source,
            _array(item["supporting_frame_ids"], lambda entry: _hash(entry, "candidate frame"), "supporting_frame_ids"),
            _decimal(item["confidence"], "candidate support confidence"),
            _hash(item["core_owner_window_manifest_sha256"], "candidate support owner window"),
            ConservativeDuration.from_mapping(item["conservative_duration"]),
        )


def _time_base(value: object, label: str) -> TimeBase:
    item = _closed(value, ("numerator", "denominator"))
    try:
        return TimeBase(_integer(item["numerator"], label, minimum=1), _integer(item["denominator"], label, minimum=1))
    except ValueError as error:
        raise CandidateCatalogError(f"{label} is invalid") from error


def _mapped_source_interval(value: object) -> MappedSourceInterval:
    item = _closed(value, ("coarse_range", "mapping_error_bound", "provider_uncertainty", "semantic_precision"))
    if item["semantic_precision"] != "coarse_only":
        raise CandidateCatalogError("candidate source interval must remain coarse_only")
    coarse = _closed(item["coarse_range"], ("start_pts", "end_pts", "time_base"))
    error = _closed(item["mapping_error_bound"], ("clock", "tick", "time_base"))
    uncertainty = _closed(item["provider_uncertainty"], ("clock", "tick", "time_base"))
    if error["clock"] != "source" or uncertainty["clock"] != "proxy":
        raise CandidateCatalogError("candidate source interval clocks are invalid")
    source_base = _time_base(coarse["time_base"], "source time base")
    if _time_base(error["time_base"], "mapping error time base") != source_base:
        raise CandidateCatalogError("candidate mapping error base differs from source base")
    try:
        return MappedSourceInterval(
            TickRange(_integer(coarse["start_pts"], "coarse start", minimum=-_SAFE), _integer(coarse["end_pts"], "coarse end", minimum=-_SAFE)),
            _integer(error["tick"], "mapping error"), source_base,
            _integer(uncertainty["tick"], "provider uncertainty"), _time_base(uncertainty["time_base"], "proxy time base"),
        )
    except ValueError as error:
        raise CandidateCatalogError("candidate source interval is invalid") from error


@dataclass(frozen=True, slots=True)
class CandidateMeasurement:
    measurement_kind: str
    value: str
    confidence: str
    fact_refs: tuple[SemanticObjectRef, ...]
    event_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        if _text(self.measurement_kind, "measurement kind") not in _MEASUREMENT_KINDS:
            raise CandidateCatalogError("candidate measurement kind is unsupported")
        object.__setattr__(self, "value", _decimal(self.value, "candidate measurement value"))
        object.__setattr__(self, "confidence", _decimal(self.confidence, "candidate measurement confidence"))
        object.__setattr__(self, "fact_refs", _measurement_refs(self.fact_refs, "vlm_fact", "fact_refs"))
        object.__setattr__(self, "event_refs", _measurement_refs(self.event_refs, "vlm_event", "event_refs"))
        if not self.fact_refs and not self.event_refs:
            raise CandidateCatalogError("candidate measurement needs a fact or event")

    def to_mapping(self) -> dict[str, object]:
        return {
            "measurement_kind": self.measurement_kind, "value": self.value, "confidence": self.confidence,
            "fact_refs": [ref.to_mapping() for ref in self.fact_refs],
            "event_refs": [ref.to_mapping() for ref in self.event_refs],
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateMeasurement:
        item = _closed(value, ("measurement_kind", "value", "confidence", "fact_refs", "event_refs"))
        return cls(
            _text(item["measurement_kind"], "measurement kind"), _decimal(item["value"], "measurement value"),
            _decimal(item["confidence"], "measurement confidence"),
            _array(item["fact_refs"], lambda entry: _ref(entry, "measurement fact ref"), "fact_refs"),
            _array(item["event_refs"], lambda entry: _ref(entry, "measurement event ref"), "event_refs"),
        )


def _measurement_refs(
    refs: tuple[SemanticObjectRef, ...], kind: str, label: str,
) -> tuple[SemanticObjectRef, ...]:
    if type(refs) is not tuple or any(type(ref) is not SemanticObjectRef for ref in refs):  # noqa: E721
        raise CandidateCatalogError(f"candidate measurement {label} is invalid")
    if len(set(refs)) != len(refs):
        raise CandidateCatalogError(f"candidate measurement {label} has duplicates")
    if any(ref.member_ref.artifact_type != "vlm_semantic_pack" or ref.object_type != kind for ref in refs):
        raise CandidateCatalogError(f"candidate measurement {label} has a wrong owner")
    for ref in refs:
        _hash(ref.object_id, "VLM measurement evidence ID")
    return tuple(sorted(refs, key=lambda ref: ref.canonical_hash))


@dataclass(frozen=True, slots=True)
class CandidateEventBinding:
    vlm_event_ref: SemanticObjectRef
    graph_event_ref: SemanticObjectRef
    event_card_ref: SemanticObjectRef

    def __post_init__(self) -> None:
        _owner(self.vlm_event_ref, "vlm_semantic_pack", "vlm_event", "candidate VLM event")
        _owner(self.graph_event_ref, "narrative_graph", "event", "candidate Graph event")
        _owner(self.event_card_ref, "event_card_set", "event", "candidate EventCard")
        if len({self.vlm_event_ref.object_id, self.graph_event_ref.object_id, self.event_card_ref.object_id}) != 1:
            raise CandidateCatalogError("candidate event binding IDs differ")
        _hash(self.vlm_event_ref.object_id, "VLM event ID")
        if len({ref.member_ref.scope for ref in (self.vlm_event_ref, self.graph_event_ref, self.event_card_ref)}) != 1:
            raise CandidateCatalogError("candidate event binding scopes differ")

    def to_mapping(self) -> dict[str, object]:
        return {
            "vlm_event_ref": self.vlm_event_ref.to_mapping(),
            "graph_event_ref": self.graph_event_ref.to_mapping(),
            "event_card_ref": self.event_card_ref.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateEventBinding:
        item = _closed(value, ("vlm_event_ref", "graph_event_ref", "event_card_ref"))
        return cls(_ref(item["vlm_event_ref"], "candidate VLM event"), _ref(item["graph_event_ref"], "candidate Graph event"), _ref(item["event_card_ref"], "candidate EventCard"))


def _event_bindings(value: object, label: str) -> tuple[CandidateEventBinding, ...]:
    entries = _array(value, CandidateEventBinding.from_mapping, label)
    if len(set(entries)) != len(entries):
        raise CandidateCatalogError(f"{label} contains duplicate bindings")
    return tuple(sorted(entries, key=lambda entry: entry.vlm_event_ref.canonical_hash))


def _bindings(
    values: tuple[CandidateEventBinding, ...], label: str,
) -> tuple[CandidateEventBinding, ...]:
    if type(values) is not tuple or any(type(item) is not CandidateEventBinding for item in values):  # noqa: E721
        raise CandidateCatalogError(f"candidate {label} is invalid")
    if len(set(values)) != len(values):
        raise CandidateCatalogError(f"candidate {label} contains duplicates")
    return tuple(sorted(values, key=lambda item: item.vlm_event_ref.canonical_hash))


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_ref: SemanticObjectRef
    source_ref: SemanticObjectRef
    source_window_ref: SemanticObjectRef
    coverage_window_id: str
    candidate_kind: str
    local_candidate_id: str
    reason: str
    anchor_summary: str
    payoff_or_open_question: str
    open_question: str | None
    dialogue_excerpt: str | None
    anchor_event: CandidateEventBinding
    supporting_events: tuple[CandidateEventBinding, ...]
    context_events: tuple[CandidateEventBinding, ...]
    payoff_events: tuple[CandidateEventBinding, ...]
    editing_modes: tuple[str, ...]
    narrative_functions: tuple[str, ...]
    tags: tuple[str, ...]
    measurements: tuple[CandidateMeasurement, ...]
    support: CandidateSupport

    def __post_init__(self) -> None:
        _owner(self.candidate_ref, "vlm_semantic_pack", "vlm_candidate", "candidate")
        _hash(self.candidate_ref.object_id, "VLM candidate ID")
        _owner(self.source_ref, "whole_series_source_manifest", "source", "candidate source")
        _owner(self.source_window_ref, "whole_series_source_manifest", "source_window", "candidate source window")
        if self.source_ref.member_ref != self.source_window_ref.member_ref:
            raise CandidateCatalogError("candidate source and window owners differ")
        if self.candidate_ref.member_ref.scope != self.source_ref.member_ref.scope:
            raise CandidateCatalogError("candidate VLM and Source scopes differ")
        if _text(self.candidate_kind, "candidate kind") not in ("highlight", "hook"):
            raise CandidateCatalogError("candidate kind is unsupported")
        for label, value in (("local candidate ID", self.local_candidate_id), ("reason", self.reason), ("anchor summary", self.anchor_summary), ("payoff/open question", self.payoff_or_open_question), ("coverage window ID", self.coverage_window_id)):
            _text(value, label)
        for label, value in (("open question", self.open_question), ("dialogue excerpt", self.dialogue_excerpt)):
            if value is not None:
                _text(value, label)
        if type(self.anchor_event) is not CandidateEventBinding:  # noqa: E721
            raise CandidateCatalogError("candidate anchor event is invalid")
        object.__setattr__(self, "supporting_events", _bindings(self.supporting_events, "supporting_events"))
        object.__setattr__(self, "context_events", _bindings(self.context_events, "context_events"))
        object.__setattr__(self, "payoff_events", _bindings(self.payoff_events, "payoff_events"))
        all_events = (self.anchor_event, *self.supporting_events, *self.context_events, *self.payoff_events)
        if any(item.vlm_event_ref.member_ref != self.candidate_ref.member_ref for item in all_events):
            raise CandidateCatalogError("candidate event belongs to a different VLM pack")
        object.__setattr__(self, "editing_modes", _enum_tuple(self.editing_modes, _EDITING_MODES, "editing_modes"))
        object.__setattr__(self, "narrative_functions", _enum_tuple(self.narrative_functions, _NARRATIVE_FUNCTIONS, "narrative_functions"))
        object.__setattr__(self, "tags", _enum_tuple(self.tags, _TAGS, "candidate tags"))
        if type(self.measurements) is not tuple or not self.measurements or any(type(item) is not CandidateMeasurement for item in self.measurements):  # noqa: E721
            raise CandidateCatalogError("candidate measurements are invalid")
        if any(ref.member_ref != self.candidate_ref.member_ref for item in self.measurements for ref in (*item.fact_refs, *item.event_refs)):
            raise CandidateCatalogError("candidate measurement belongs to a different VLM pack")
        if type(self.support) is not CandidateSupport:  # noqa: E721
            raise CandidateCatalogError("candidate support is invalid")
        if self.support.core_owner_window_manifest_sha256 != self.source_window_ref.object_id:
            raise CandidateCatalogError("candidate support window differs from candidate window")
        if self.candidate_kind == "hook":
            if self.open_question is None or self.payoff_events:
                raise CandidateCatalogError("hook candidate has invalid payoff/open-question closure")
        elif not self.payoff_events:
            raise CandidateCatalogError("highlight candidate needs payoff evidence")
    @property
    def candidate_id(self) -> str:
        return self.candidate_ref.object_id

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_ref": self.candidate_ref.to_mapping(), "source_ref": self.source_ref.to_mapping(),
            "source_window_ref": self.source_window_ref.to_mapping(), "coverage_window_id": self.coverage_window_id,
            "candidate_kind": self.candidate_kind, "local_candidate_id": self.local_candidate_id,
            "reason": self.reason, "anchor_summary": self.anchor_summary,
            "payoff_or_open_question": self.payoff_or_open_question, "open_question": self.open_question,
            "dialogue_excerpt": self.dialogue_excerpt, "anchor_event": self.anchor_event.to_mapping(),
            "supporting_events": [item.to_mapping() for item in self.supporting_events],
            "context_events": [item.to_mapping() for item in self.context_events],
            "payoff_events": [item.to_mapping() for item in self.payoff_events],
            "editing_modes": list(self.editing_modes), "narrative_functions": list(self.narrative_functions),
            "tags": list(self.tags), "measurements": [item.to_mapping() for item in self.measurements],
            "support": self.support.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Candidate:
        item = _closed(value, (
            "candidate_ref", "source_ref", "source_window_ref", "coverage_window_id", "candidate_kind", "local_candidate_id",
            "reason", "anchor_summary", "payoff_or_open_question", "open_question", "dialogue_excerpt", "anchor_event",
            "supporting_events", "context_events", "payoff_events", "editing_modes", "narrative_functions", "tags", "measurements", "support",
        ))
        return cls(
            _ref(item["candidate_ref"], "candidate ref"), _ref(item["source_ref"], "source ref"),
            _ref(item["source_window_ref"], "source window ref"), _text(item["coverage_window_id"], "coverage window ID"),
            _text(item["candidate_kind"], "candidate kind"), _text(item["local_candidate_id"], "local candidate ID"),
            _text(item["reason"], "reason"), _text(item["anchor_summary"], "anchor summary"),
            _text(item["payoff_or_open_question"], "payoff/open question"),
            None if item["open_question"] is None else _text(item["open_question"], "open question"),
            None if item["dialogue_excerpt"] is None else _text(item["dialogue_excerpt"], "dialogue excerpt"),
            CandidateEventBinding.from_mapping(item["anchor_event"]), _event_bindings(item["supporting_events"], "supporting events"),
            _event_bindings(item["context_events"], "context events"), _event_bindings(item["payoff_events"], "payoff events"),
            _array(item["editing_modes"], lambda entry: _text(entry, "editing mode"), "editing_modes"),
            _array(item["narrative_functions"], lambda entry: _text(entry, "narrative function"), "narrative_functions"),
            _array(item["tags"], lambda entry: _text(entry, "candidate tag"), "tags"),
            _array(item["measurements"], CandidateMeasurement.from_mapping, "measurements"),
            CandidateSupport.from_mapping(item["support"]),
        )


@dataclass(frozen=True, slots=True)
class CandidateCatalog:
    catalog_id: str
    input_binding_sha256: str
    source_grant_sha256: str
    event_card_member_ref: SemanticMemberIdentity
    narrative_graph_member_ref: SemanticMemberIdentity
    coverage_ledger_member_ref: SemanticMemberIdentity
    policy_sha256: str
    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        _hash(self.catalog_id, "catalog ID")
        for label in ("input binding", "source grant", "policy"):
            object.__setattr__(self, {"input binding": "input_binding_sha256", "source grant": "source_grant_sha256", "policy": "policy_sha256"}[label], _hash(getattr(self, {"input binding": "input_binding_sha256", "source grant": "source_grant_sha256", "policy": "policy_sha256"}[label]), label))
        expected = ((self.event_card_member_ref, "event_card_set"), (self.narrative_graph_member_ref, "narrative_graph"), (self.coverage_ledger_member_ref, "coverage_ledger"))
        if any(type(ref) is not SemanticMemberIdentity or ref.artifact_type != kind for ref, kind in expected):  # noqa: E721
            raise CandidateCatalogError("catalog Stage 1 member identity is invalid")
        if len({(ref.scope, ref.revision) for ref, _ in expected}) != 1:
            raise CandidateCatalogError("catalog Stage 1 members have different scopes/revisions")
        if type(self.candidates) is not tuple or any(type(item) is not Candidate for item in self.candidates):  # noqa: E721
            raise CandidateCatalogError("catalog candidates must be exact tuple values")
        ids = tuple(item.candidate_id for item in self.candidates)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise CandidateCatalogError("catalog candidates must be uniquely canonical by ID")
        if len({item.source_ref.member_ref for item in self.candidates}) > 1:
            raise CandidateCatalogError("catalog candidates have different SourceManifest owners")
        for candidate in self.candidates:
            if candidate.source_ref.member_ref.scope != self.narrative_graph_member_ref.scope:
                raise CandidateCatalogError("catalog candidate belongs to a different scope")
            events = (candidate.anchor_event, *candidate.supporting_events,
                      *candidate.context_events, *candidate.payoff_events)
            if any(event.graph_event_ref.member_ref != self.narrative_graph_member_ref
                   or event.event_card_ref.member_ref != self.event_card_member_ref for event in events):
                raise CandidateCatalogError("candidate event owners differ from catalog Graph/Card")

    def to_mapping(self) -> dict[str, object]:
        return {
            "catalog_id": self.catalog_id, "input_binding_sha256": self.input_binding_sha256,
            "source_grant_sha256": self.source_grant_sha256,
            "event_card_member_ref": self.event_card_member_ref.to_mapping(),
            "narrative_graph_member_ref": self.narrative_graph_member_ref.to_mapping(),
            "coverage_ledger_member_ref": self.coverage_ledger_member_ref.to_mapping(),
            "policy_sha256": self.policy_sha256, "candidates": [item.to_mapping() for item in self.candidates],
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateCatalog:
        item = _closed(value, (
            "catalog_id", "input_binding_sha256", "source_grant_sha256", "event_card_member_ref",
            "narrative_graph_member_ref", "coverage_ledger_member_ref", "policy_sha256", "candidates",
        ))
        return cls(
            _hash(item["catalog_id"], "catalog ID"), _hash(item["input_binding_sha256"], "input binding"),
            _hash(item["source_grant_sha256"], "source grant"), _identity(item["event_card_member_ref"], "EventCard identity"),
            _identity(item["narrative_graph_member_ref"], "Graph identity"), _identity(item["coverage_ledger_member_ref"], "Ledger identity"),
            _hash(item["policy_sha256"], "policy"),
            _array(item["candidates"], Candidate.from_mapping, "candidates"),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
