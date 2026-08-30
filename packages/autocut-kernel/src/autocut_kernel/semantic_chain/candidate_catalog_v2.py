"""Closed CandidateCatalog V2 values for text-enriched V4 observations.

The catalog preserves semantic coarse support and exact provenance.  It does
not carry frame identities, speech analysis, physical edit endpoints,
admission results, or publication decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, TypeVar, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..media.types import TickRange, TimeBase
from ..vlm.models import MappedSourceInterval
from .member_refs import SemanticMemberIdentity, SemanticObjectRef

CANDIDATE_CATALOG_V2_SCHEMA_VERSION = "candidate-catalog-v2"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DECIMAL = re.compile(r"(?:0|1|0\.[0-9]*[1-9])\Z")
_LOCAL_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MEASUREMENT_KINDS = (
    "hook_strength",
    "reveal_strength",
    "emotional_payoff_strength",
    "dialogue_salience",
    "action_salience",
    "visual_salience",
)
_CAPABILITY_REGISTRY: tuple[tuple[str, str, str], ...] = (
    ("dialogue_salience_v1", "dialogue", "dialogue_salience"),
    ("action_salience_v1", "action", "action_salience"),
)
_CAPABILITY_OUTCOMES = (
    "available",
    "measurement_missing",
    "value_below_threshold",
    "confidence_below_threshold",
)
_SAFE = 2**53 - 1
_T = TypeVar("_T")


class CandidateCatalogV2Error(ValueError):
    """A CandidateCatalog V2 value violates its closed pure contract."""


def _closed(value: object, keys: tuple[str, ...], label: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise CandidateCatalogV2Error(f"{label} must be a closed mapping")
    mapping = cast(dict[object, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != set(keys):  # noqa: E721
        raise CandidateCatalogV2Error(f"{label} has missing or unknown fields")
    return cast(dict[str, object], mapping)


def _array(value: object, parser: Callable[[object], _T], label: str) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise CandidateCatalogV2Error(f"{label} must be an array")
    return tuple(parser(item) for item in cast(list[object], value))


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise CandidateCatalogV2Error(f"{label} must be non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CandidateCatalogV2Error(f"{label} must be valid UTF-8") from error
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:  # noqa: E721
        raise CandidateCatalogV2Error(f"{label} must be lowercase sha256")
    return value


def _decimal(value: object, label: str) -> str:
    if type(value) is not str or _DECIMAL.fullmatch(value) is None:  # noqa: E721
        raise CandidateCatalogV2Error(f"{label} must be canonical decimal text in [0,1]")
    return value


def canonical_decimal(value: Decimal, label: str) -> str:
    """Render one exact bounded Decimal without context rounding."""

    if (
        type(value) is not Decimal  # noqa: E721
        or not value.is_finite()
        or not Decimal(0) <= value <= Decimal(1)
        or len(value.as_tuple().digits) > 80
        or value.adjusted() < -78
    ):
        raise CandidateCatalogV2Error(f"{label} must be a bounded finite Decimal")
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return _decimal("0" if result == "-0" else result, label)


def _integer(value: object, label: str, *, minimum: int = -_SAFE) -> int:
    if type(value) is not int or not minimum <= value <= _SAFE:  # noqa: E721
        raise CandidateCatalogV2Error(f"{label} must be an exact JSON-safe integer")
    return value


def _identity(value: object, label: str) -> SemanticMemberIdentity:
    try:
        return SemanticMemberIdentity.from_mapping(value)
    except ValueError as error:
        raise CandidateCatalogV2Error(f"{label} is invalid") from error


def _ref(value: object, label: str) -> SemanticObjectRef:
    try:
        return SemanticObjectRef.from_mapping(value)
    except ValueError as error:
        raise CandidateCatalogV2Error(f"{label} is invalid") from error


def _canonical_refs(
    values: tuple[SemanticObjectRef, ...],
    label: str,
    *,
    allowed: tuple[tuple[str, str], ...],
    nonempty: bool = True,
) -> tuple[SemanticObjectRef, ...]:
    if type(values) is not tuple or any(type(item) is not SemanticObjectRef for item in values):  # noqa: E721
        raise CandidateCatalogV2Error(f"{label} must contain exact semantic references")
    if nonempty and not values:
        raise CandidateCatalogV2Error(f"{label} must be non-empty")
    if any((item.member_ref.artifact_type, item.object_type) not in allowed for item in values):
        raise CandidateCatalogV2Error(f"{label} has a wrong owner")
    keys = tuple(canonical_json_bytes(item.to_mapping()) for item in values)
    if len(keys) != len(set(keys)) or keys != tuple(sorted(keys)):
        raise CandidateCatalogV2Error(f"{label} must be canonical and unique")
    return values


@dataclass(frozen=True, slots=True)
class CandidateCatalogV2Policy:
    strategy_version: str
    candidate_id_strategy_version: str
    coarse_support_strategy_version: str

    def __post_init__(self) -> None:
        if self.strategy_version != "candidate-catalog-v2":
            raise CandidateCatalogV2Error("candidate catalog V2 strategy is unsupported")
        if self.candidate_id_strategy_version != "candidate-content-id-v1":
            raise CandidateCatalogV2Error("candidate ID strategy is unsupported")
        if self.coarse_support_strategy_version != "anchor-source-envelope-v1":
            raise CandidateCatalogV2Error("candidate coarse-support strategy is unsupported")

    def to_mapping(self) -> dict[str, object]:
        return {
            "strategy_version": self.strategy_version,
            "candidate_id_strategy_version": self.candidate_id_strategy_version,
            "coarse_support_strategy_version": self.coarse_support_strategy_version,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class CandidateCapabilityRule:
    rule_id: str
    minimum_value: str
    minimum_confidence: str

    def __post_init__(self) -> None:
        if self.rule_id not in {item[0] for item in _CAPABILITY_REGISTRY}:
            raise CandidateCatalogV2Error("candidate capability rule is not registered")
        object.__setattr__(self, "minimum_value", _decimal(self.minimum_value, "minimum value"))
        object.__setattr__(
            self,
            "minimum_confidence",
            _decimal(self.minimum_confidence, "minimum confidence"),
        )

    @property
    def capability(self) -> str:
        return next(item[1] for item in _CAPABILITY_REGISTRY if item[0] == self.rule_id)

    @property
    def measurement_kind(self) -> str:
        return next(item[2] for item in _CAPABILITY_REGISTRY if item[0] == self.rule_id)

    def to_mapping(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "minimum_value": self.minimum_value,
            "minimum_confidence": self.minimum_confidence,
        }


@dataclass(frozen=True, slots=True)
class CandidateCapabilityPolicy:
    strategy_version: str
    rules: tuple[CandidateCapabilityRule, ...]

    def __post_init__(self) -> None:
        if self.strategy_version != "registered-semantic-capabilities-v1":
            raise CandidateCatalogV2Error("candidate capability strategy is unsupported")
        if type(self.rules) is not tuple or not self.rules or any(  # noqa: E721
            type(item) is not CandidateCapabilityRule for item in self.rules
        ):
            raise CandidateCatalogV2Error("candidate capability policy needs exact rules")
        ids = tuple(item.rule_id for item in self.rules)
        expected = tuple(item[0] for item in _CAPABILITY_REGISTRY if item[0] in ids)
        if ids != expected or len(ids) != len(set(ids)):
            raise CandidateCatalogV2Error("candidate capability rules must be canonical and unique")

    def to_mapping(self) -> dict[str, object]:
        return {
            "strategy_version": self.strategy_version,
            "rules": [item.to_mapping() for item in self.rules],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class CandidateAnchorRefV2:
    vlm_event_ref: SemanticObjectRef
    narrative_event_ref: SemanticObjectRef
    event_card_ref: SemanticObjectRef

    def __post_init__(self) -> None:
        expected = (
            (self.vlm_event_ref, "vlm_semantic_pack", "vlm_event"),
            (self.narrative_event_ref, "narrative_graph", "event"),
            (self.event_card_ref, "event_card_set", "event"),
        )
        if any(
            type(ref) is not SemanticObjectRef
            or ref.member_ref.artifact_type != artifact_type
            or ref.object_type != object_type
            for ref, artifact_type, object_type in expected
        ):
            raise CandidateCatalogV2Error("candidate anchor has a wrong exact owner")
        if len({ref.object_id for ref, _, _ in expected}) != 1:
            raise CandidateCatalogV2Error("candidate anchor expanded object IDs differ")
        if len({ref.member_ref.scope for ref, _, _ in expected}) != 1:
            raise CandidateCatalogV2Error("candidate anchor expanded scopes differ")

    @property
    def object_id(self) -> str:
        return self.vlm_event_ref.object_id

    def to_mapping(self) -> dict[str, object]:
        return {
            "vlm_event_ref": self.vlm_event_ref.to_mapping(),
            "narrative_event_ref": self.narrative_event_ref.to_mapping(),
            "event_card_ref": self.event_card_ref.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateAnchorRefV2:
        item = _closed(
            value,
            ("vlm_event_ref", "narrative_event_ref", "event_card_ref"),
            "candidate anchor",
        )
        return cls(
            _ref(item["vlm_event_ref"], "VLM event"),
            _ref(item["narrative_event_ref"], "narrative event"),
            _ref(item["event_card_ref"], "event card"),
        )


@dataclass(frozen=True, slots=True)
class CandidateSemanticMeasurementV2:
    measurement_id: str
    measurement_kind: str
    value: str
    confidence: str
    evidence_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        _sha256(self.measurement_id, "candidate measurement ID")
        if self.measurement_kind not in _MEASUREMENT_KINDS:
            raise CandidateCatalogV2Error("candidate measurement kind is unsupported")
        object.__setattr__(self, "value", _decimal(self.value, "candidate measurement value"))
        object.__setattr__(
            self,
            "confidence",
            _decimal(self.confidence, "candidate measurement confidence"),
        )
        _canonical_refs(
            self.evidence_refs,
            "candidate measurement evidence",
            allowed=(("vlm_semantic_pack", "vlm_fact"), ("vlm_semantic_pack", "vlm_event")),
        )
        if len({item.member_ref for item in self.evidence_refs}) != 1:
            raise CandidateCatalogV2Error("candidate measurement evidence crosses owners")

    def to_mapping(self) -> dict[str, object]:
        return {
            "measurement_id": self.measurement_id,
            "measurement_kind": self.measurement_kind,
            "value": self.value,
            "confidence": self.confidence,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateSemanticMeasurementV2:
        item = _closed(
            value,
            ("measurement_id", "measurement_kind", "value", "confidence", "evidence_refs"),
            "candidate measurement",
        )
        return cls(
            _sha256(item["measurement_id"], "candidate measurement ID"),
            _text(item["measurement_kind"], "candidate measurement kind"),
            _decimal(item["value"], "candidate measurement value"),
            _decimal(item["confidence"], "candidate measurement confidence"),
            _array(item["evidence_refs"], lambda entry: _ref(entry, "measurement evidence"), "measurement evidence"),
        )


def _time_base(value: object, label: str) -> TimeBase:
    item = _closed(value, ("numerator", "denominator"), label)
    try:
        return TimeBase(
            _integer(item["numerator"], label, minimum=1),
            _integer(item["denominator"], label, minimum=1),
        )
    except ValueError as error:
        raise CandidateCatalogV2Error(f"{label} is invalid") from error


def _mapped_interval(value: object) -> MappedSourceInterval:
    item = _closed(
        value,
        ("coarse_range", "mapping_error_bound", "provider_uncertainty", "semantic_precision"),
        "candidate coarse interval",
    )
    if item["semantic_precision"] != "coarse_only":
        raise CandidateCatalogV2Error("candidate support must remain coarse_only")
    coarse = _closed(item["coarse_range"], ("start_pts", "end_pts", "time_base"), "coarse range")
    error = _closed(item["mapping_error_bound"], ("clock", "tick", "time_base"), "mapping error")
    uncertainty = _closed(item["provider_uncertainty"], ("clock", "tick", "time_base"), "provider uncertainty")
    source_base = _time_base(coarse["time_base"], "source time base")
    proxy_base = _time_base(uncertainty["time_base"], "proxy time base")
    if error["clock"] != "source" or uncertainty["clock"] != "proxy":
        raise CandidateCatalogV2Error("candidate support clocks are invalid")
    if _time_base(error["time_base"], "mapping error time base") != source_base:
        raise CandidateCatalogV2Error("candidate mapping error uses another source time base")
    try:
        return MappedSourceInterval(
            TickRange(
                _integer(coarse["start_pts"], "coarse start"),
                _integer(coarse["end_pts"], "coarse end"),
            ),
            _integer(error["tick"], "mapping error", minimum=0),
            source_base,
            _integer(uncertainty["tick"], "provider uncertainty", minimum=0),
            proxy_base,
        )
    except ValueError as exc:
        raise CandidateCatalogV2Error("candidate coarse support is invalid") from exc


@dataclass(frozen=True, slots=True)
class CandidateCoarseSupportV2:
    derivation_strategy_version: str
    source_interval: MappedSourceInterval
    confidence: str
    core_owner_window_manifest_sha256: str

    def __post_init__(self) -> None:
        if self.derivation_strategy_version != "anchor-source-envelope-v1":
            raise CandidateCatalogV2Error("candidate support derivation is unsupported")
        if type(self.source_interval) is not MappedSourceInterval:  # noqa: E721
            raise CandidateCatalogV2Error("candidate support interval must be exact")
        interval = self.source_interval
        _integer(interval.coarse_range.start_pts, "coarse start")
        _integer(interval.coarse_range.end_pts, "coarse end")
        _integer(interval.mapping_error_bound_source_pts, "mapping error", minimum=0)
        _integer(interval.provider_uncertainty_proxy_pts, "provider uncertainty", minimum=0)
        for base in (interval.source_time_base, interval.proxy_time_base):
            _integer(base.numerator, "time base numerator", minimum=1)
            _integer(base.denominator, "time base denominator", minimum=1)
        object.__setattr__(self, "confidence", _decimal(self.confidence, "support confidence"))
        _sha256(self.core_owner_window_manifest_sha256, "support owner window")

    def to_mapping(self) -> dict[str, object]:
        return {
            "derivation_strategy_version": self.derivation_strategy_version,
            "source_interval": self.source_interval.to_mapping(),
            "confidence": self.confidence,
            "core_owner_window_manifest_sha256": self.core_owner_window_manifest_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateCoarseSupportV2:
        item = _closed(
            value,
            (
                "derivation_strategy_version",
                "source_interval",
                "confidence",
                "core_owner_window_manifest_sha256",
            ),
            "candidate coarse support",
        )
        return cls(
            _text(item["derivation_strategy_version"], "support strategy"),
            _mapped_interval(item["source_interval"]),
            _decimal(item["confidence"], "support confidence"),
            _sha256(item["core_owner_window_manifest_sha256"], "support owner window"),
        )


@dataclass(frozen=True, slots=True)
class CandidateCapabilityAssessmentV2:
    rule_id: str
    capability: str
    outcome: str
    basis_measurement_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        registered = {item[0]: item[1] for item in _CAPABILITY_REGISTRY}
        if self.rule_id not in registered or self.capability != registered[self.rule_id]:
            raise CandidateCatalogV2Error("capability assessment rule/capability is unregistered")
        if self.outcome not in _CAPABILITY_OUTCOMES:
            raise CandidateCatalogV2Error("capability assessment outcome is unsupported")
        if type(self.basis_measurement_ids) is not tuple or any(  # noqa: E721
            type(item) is not str for item in self.basis_measurement_ids
        ):
            raise CandidateCatalogV2Error("capability basis IDs must be an exact tuple")
        for item in self.basis_measurement_ids:
            _sha256(item, "capability basis measurement ID")
        if (
            len(self.basis_measurement_ids) != len(set(self.basis_measurement_ids))
            or self.basis_measurement_ids != tuple(sorted(self.basis_measurement_ids))
        ):
            raise CandidateCatalogV2Error("capability basis IDs must be canonical and unique")
        if (self.outcome == "measurement_missing") != (not self.basis_measurement_ids):
            raise CandidateCatalogV2Error("capability outcome and measurement basis disagree")

    def to_mapping(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "capability": self.capability,
            "outcome": self.outcome,
            "basis_measurement_ids": list(self.basis_measurement_ids),
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateCapabilityAssessmentV2:
        item = _closed(
            value,
            ("rule_id", "capability", "outcome", "basis_measurement_ids"),
            "candidate capability assessment",
        )
        return cls(
            _text(item["rule_id"], "capability rule"),
            _text(item["capability"], "capability"),
            _text(item["outcome"], "capability outcome"),
            _array(item["basis_measurement_ids"], lambda entry: _sha256(entry, "measurement ID"), "measurement IDs"),
        )


@dataclass(frozen=True, slots=True)
class CandidateV2:
    candidate_id: str
    local_candidate_id: str
    summary: str
    anchor_refs: tuple[CandidateAnchorRefV2, ...]
    semantic_measurements: tuple[CandidateSemanticMeasurementV2, ...]
    source_ref: SemanticObjectRef
    source_window_ref: SemanticObjectRef
    coarse_support: CandidateCoarseSupportV2
    capability_assessment: tuple[CandidateCapabilityAssessmentV2, ...]

    def __post_init__(self) -> None:
        _sha256(self.candidate_id, "candidate ID")
        if type(self.local_candidate_id) is not str or _LOCAL_ID.fullmatch(self.local_candidate_id) is None:  # noqa: E721
            raise CandidateCatalogV2Error("candidate local ID is invalid")
        _text(self.summary, "candidate summary")
        if type(self.anchor_refs) is not tuple or not self.anchor_refs or any(  # noqa: E721
            type(item) is not CandidateAnchorRefV2 for item in self.anchor_refs
        ):
            raise CandidateCatalogV2Error("candidate anchors must be exact non-empty values")
        anchor_keys = tuple(item.object_id for item in self.anchor_refs)
        if anchor_keys != tuple(sorted(anchor_keys)) or len(anchor_keys) != len(set(anchor_keys)):
            raise CandidateCatalogV2Error("candidate anchors must be canonical and unique")
        if len({item.vlm_event_ref.member_ref for item in self.anchor_refs}) != 1:
            raise CandidateCatalogV2Error("candidate anchors cross VLM owners")
        if type(self.semantic_measurements) is not tuple or not self.semantic_measurements or any(  # noqa: E721
            type(item) is not CandidateSemanticMeasurementV2 for item in self.semantic_measurements
        ):
            raise CandidateCatalogV2Error("candidate measurements must be exact non-empty values")
        measurement_kinds = tuple(item.measurement_kind for item in self.semantic_measurements)
        expected_kinds = tuple(kind for kind in _MEASUREMENT_KINDS if kind in measurement_kinds)
        if measurement_kinds != expected_kinds or len(measurement_kinds) != len(set(measurement_kinds)):
            raise CandidateCatalogV2Error("candidate measurements must be canonical by unique kind")
        owner = self.anchor_refs[0].vlm_event_ref.member_ref
        if any(
            ref.member_ref != owner
            for measurement in self.semantic_measurements
            for ref in measurement.evidence_refs
        ):
            raise CandidateCatalogV2Error("candidate measurements cross the anchor owner")
        if (
            type(self.source_ref) is not SemanticObjectRef  # noqa: E721
            or self.source_ref.member_ref.artifact_type != "whole_series_source_manifest"
            or self.source_ref.object_type != "source"
            or type(self.source_window_ref) is not SemanticObjectRef  # noqa: E721
            or self.source_window_ref.member_ref.artifact_type != "whole_series_source_manifest"
            or self.source_window_ref.object_type != "source_window"
            or self.source_ref.member_ref != self.source_window_ref.member_ref
        ):
            raise CandidateCatalogV2Error("candidate source/window provenance is invalid")
        if type(self.coarse_support) is not CandidateCoarseSupportV2:  # noqa: E721
            raise CandidateCatalogV2Error("candidate coarse support is invalid")
        if self.coarse_support.core_owner_window_manifest_sha256 != self.source_window_ref.object_id:
            raise CandidateCatalogV2Error("candidate support owner differs from source window")
        if type(self.capability_assessment) is not tuple or any(  # noqa: E721
            type(item) is not CandidateCapabilityAssessmentV2 for item in self.capability_assessment
        ):
            raise CandidateCatalogV2Error("candidate capability assessment is invalid")
        rules = tuple(item.rule_id for item in self.capability_assessment)
        expected_rules = tuple(item[0] for item in _CAPABILITY_REGISTRY if item[0] in rules)
        if rules != expected_rules or len(rules) != len(set(rules)):
            raise CandidateCatalogV2Error("candidate capability assessment must be canonical")
        measurement_ids = {item.measurement_id for item in self.semantic_measurements}
        if any(
            not set(item.basis_measurement_ids) <= measurement_ids
            for item in self.capability_assessment
        ):
            raise CandidateCatalogV2Error("capability assessment cites an unknown measurement")

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "local_candidate_id": self.local_candidate_id,
            "summary": self.summary,
            "anchor_refs": [item.to_mapping() for item in self.anchor_refs],
            "semantic_measurements": [item.to_mapping() for item in self.semantic_measurements],
            "source_ref": self.source_ref.to_mapping(),
            "source_window_ref": self.source_window_ref.to_mapping(),
            "coarse_support": self.coarse_support.to_mapping(),
            "capability_assessment": [item.to_mapping() for item in self.capability_assessment],
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateV2:
        item = _closed(
            value,
            (
                "candidate_id",
                "local_candidate_id",
                "summary",
                "anchor_refs",
                "semantic_measurements",
                "source_ref",
                "source_window_ref",
                "coarse_support",
                "capability_assessment",
            ),
            "candidate V2",
        )
        return cls(
            _sha256(item["candidate_id"], "candidate ID"),
            _text(item["local_candidate_id"], "candidate local ID"),
            _text(item["summary"], "candidate summary"),
            _array(item["anchor_refs"], CandidateAnchorRefV2.from_mapping, "candidate anchors"),
            _array(item["semantic_measurements"], CandidateSemanticMeasurementV2.from_mapping, "candidate measurements"),
            _ref(item["source_ref"], "candidate source"),
            _ref(item["source_window_ref"], "candidate source window"),
            CandidateCoarseSupportV2.from_mapping(item["coarse_support"]),
            _array(item["capability_assessment"], CandidateCapabilityAssessmentV2.from_mapping, "capability assessment"),
        )


@dataclass(frozen=True, slots=True)
class CandidateCatalogV2:
    catalog_id: str
    input_binding_sha256: str
    canonical_draft_sha256: str
    draft_policy_sha256: str
    catalog_policy_sha256: str
    capability_policy_sha256: str
    source_manifest_ref: SemanticMemberIdentity
    event_card_member_ref: SemanticMemberIdentity
    narrative_graph_member_ref: SemanticMemberIdentity
    coverage_ledger_member_ref: SemanticMemberIdentity
    candidates: tuple[CandidateV2, ...]

    def __post_init__(self) -> None:
        for field in (
            "catalog_id",
            "input_binding_sha256",
            "canonical_draft_sha256",
            "draft_policy_sha256",
            "catalog_policy_sha256",
            "capability_policy_sha256",
        ):
            _sha256(getattr(self, field), field)
        expected = (
            (self.source_manifest_ref, "whole_series_source_manifest"),
            (self.event_card_member_ref, "event_card_set"),
            (self.narrative_graph_member_ref, "narrative_graph"),
            (self.coverage_ledger_member_ref, "coverage_ledger"),
        )
        if any(
            type(item) is not SemanticMemberIdentity or item.artifact_type != artifact_type
            for item, artifact_type in expected
        ):
            raise CandidateCatalogV2Error("catalog member provenance is invalid")
        if len({item.scope for item, _ in expected}) != 1:
            raise CandidateCatalogV2Error("catalog member scopes differ")
        if type(self.candidates) is not tuple or not self.candidates or any(  # noqa: E721
            type(item) is not CandidateV2 for item in self.candidates
        ):
            raise CandidateCatalogV2Error("catalog candidates must be exact non-empty values")
        ids = tuple(item.candidate_id for item in self.candidates)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise CandidateCatalogV2Error("catalog candidates must be canonical by unique ID")
        for candidate in self.candidates:
            if candidate.source_ref.member_ref != self.source_manifest_ref:
                raise CandidateCatalogV2Error("candidate source differs from catalog source")
            if any(
                anchor.narrative_event_ref.member_ref != self.narrative_graph_member_ref
                or anchor.event_card_ref.member_ref != self.event_card_member_ref
                for anchor in candidate.anchor_refs
            ):
                raise CandidateCatalogV2Error("candidate expanded refs differ from catalog owners")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": CANDIDATE_CATALOG_V2_SCHEMA_VERSION,
            "catalog_id": self.catalog_id,
            "input_binding_sha256": self.input_binding_sha256,
            "canonical_draft_sha256": self.canonical_draft_sha256,
            "draft_policy_sha256": self.draft_policy_sha256,
            "catalog_policy_sha256": self.catalog_policy_sha256,
            "capability_policy_sha256": self.capability_policy_sha256,
            "source_manifest_ref": self.source_manifest_ref.to_mapping(),
            "event_card_member_ref": self.event_card_member_ref.to_mapping(),
            "narrative_graph_member_ref": self.narrative_graph_member_ref.to_mapping(),
            "coverage_ledger_member_ref": self.coverage_ledger_member_ref.to_mapping(),
            "candidates": [item.to_mapping() for item in self.candidates],
        }

    @classmethod
    def from_mapping(cls, value: object) -> CandidateCatalogV2:
        item = _closed(
            value,
            (
                "schema_version",
                "catalog_id",
                "input_binding_sha256",
                "canonical_draft_sha256",
                "draft_policy_sha256",
                "catalog_policy_sha256",
                "capability_policy_sha256",
                "source_manifest_ref",
                "event_card_member_ref",
                "narrative_graph_member_ref",
                "coverage_ledger_member_ref",
                "candidates",
            ),
            "CandidateCatalog V2",
        )
        if item["schema_version"] != CANDIDATE_CATALOG_V2_SCHEMA_VERSION:
            raise CandidateCatalogV2Error("CandidateCatalog V2 schema is unsupported")
        return cls(
            _sha256(item["catalog_id"], "catalog ID"),
            _sha256(item["input_binding_sha256"], "input binding"),
            _sha256(item["canonical_draft_sha256"], "canonical draft"),
            _sha256(item["draft_policy_sha256"], "draft policy"),
            _sha256(item["catalog_policy_sha256"], "catalog policy"),
            _sha256(item["capability_policy_sha256"], "capability policy"),
            _identity(item["source_manifest_ref"], "source manifest"),
            _identity(item["event_card_member_ref"], "EventCard member"),
            _identity(item["narrative_graph_member_ref"], "NarrativeGraph member"),
            _identity(item["coverage_ledger_member_ref"], "CoverageLedger member"),
            _array(item["candidates"], CandidateV2.from_mapping, "catalog candidates"),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def evaluate_candidate_capabilities(
    measurements: tuple[CandidateSemanticMeasurementV2, ...],
    policy: CandidateCapabilityPolicy,
) -> tuple[CandidateCapabilityAssessmentV2, ...]:
    """Evaluate the immutable local registry; provider fields cannot override it."""

    if type(policy) is not CandidateCapabilityPolicy:  # noqa: E721
        raise CandidateCatalogV2Error("capability evaluation requires an exact policy")
    if type(measurements) is not tuple or any(  # noqa: E721
        type(item) is not CandidateSemanticMeasurementV2 for item in measurements
    ):
        raise CandidateCatalogV2Error("capability evaluation requires exact measurements")
    by_kind = {item.measurement_kind: item for item in measurements}
    result: list[CandidateCapabilityAssessmentV2] = []
    for rule in policy.rules:
        measurement = by_kind.get(rule.measurement_kind)
        if measurement is None:
            result.append(
                CandidateCapabilityAssessmentV2(
                    rule.rule_id,
                    rule.capability,
                    "measurement_missing",
                    (),
                )
            )
            continue
        outcome = "available"
        if Decimal(measurement.value) < Decimal(rule.minimum_value):
            outcome = "value_below_threshold"
        elif Decimal(measurement.confidence) < Decimal(rule.minimum_confidence):
            outcome = "confidence_below_threshold"
        result.append(
            CandidateCapabilityAssessmentV2(
                rule.rule_id,
                rule.capability,
                outcome,
                (measurement.measurement_id,),
            )
        )
    return tuple(result)
