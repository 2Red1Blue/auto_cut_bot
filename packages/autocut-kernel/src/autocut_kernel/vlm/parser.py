"""Strict parser for provider-local VLM Semantic Pack v3 responses."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import NoReturn, TypeVar, cast

from ..media.types import TickRange, require_pts
from .models import (
    VlmCandidateHypothesis,
    VlmCandidateKind,
    VlmCandidateTag,
    VlmContinuity,
    VlmContractError,
    VlmEditingMode,
    VlmEntity,
    VlmEntityKind,
    VlmEvent,
    VlmEventKind,
    VlmFact,
    VlmFactKind,
    VlmMeasurementKind,
    VlmNarrativeFunction,
    VlmParsePolicy,
    VlmProxyInterval,
    VlmRequestIdentity,
    VlmSemanticMeasurement,
    VlmSemanticPack,
    VlmSemanticSupport,
    VlmTemporalMode,
    VlmTemporalSegment,
    VlmValidationError,
    VlmWindowSummary,
    derive_vlm_global_id,
)
from .window import WindowManifest, WindowManifestSet, select_core_owner


class VlmResponseRejected(VlmContractError):  # noqa: N818
    """The provider response violated the closed v3 contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class VlmResponseIndeterminate(VlmContractError):  # noqa: N818
    """A structurally valid response exceeded a frozen resource budget."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _reject(code: str, message: str) -> NoReturn:
    raise VlmResponseRejected(code, message)


def _pairs_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("DUPLICATE_JSON_KEY", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> NoReturn:
    _reject("INVALID_JSON_NUMBER", f"non-finite JSON number is forbidden: {value}")


def _closed(value: object, fields: frozenset[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        _reject("INVALID_RESPONSE_SCHEMA", f"{field_name} must be an object")
    result = cast(dict[str, object], value)
    keys = frozenset(result)
    if keys - fields:
        _reject(
            "UNKNOWN_RESPONSE_FIELD",
            f"{field_name} contains unknown fields: {sorted(keys - fields)}",
        )
    if fields - keys:
        _reject(
            "MISSING_RESPONSE_FIELD",
            f"{field_name} is missing fields: {sorted(fields - keys)}",
        )
    return result


def _array(value: object, field_name: str) -> list[object]:
    if type(value) is not list:  # noqa: E721
        _reject("INVALID_RESPONSE_SCHEMA", f"{field_name} must be an array")
    return cast(list[object], value)


@dataclass(slots=True)
class _TextBudget:
    policy: VlmParsePolicy
    total: int = 0

    def text(self, value: object, field_name: str, *, nullable: bool = False) -> str | None:
        if value is None and nullable:
            return None
        if type(value) is not str or not value or value.isspace():  # noqa: E721
            _reject("INVALID_TEXT", f"{field_name} must be non-empty text")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            _reject("INVALID_TEXT", f"{field_name} contains control characters")
        if len(value) > self.policy.max_text_characters:
            raise VlmResponseIndeterminate(
                "TEXT_BUDGET_EXCEEDED", f"{field_name} exceeds the frozen text budget"
            )
        self.total += len(value)
        if self.total > self.policy.max_total_text_characters:
            raise VlmResponseIndeterminate(
                "TEXT_BUDGET_EXCEEDED", "response exceeds the frozen total text budget"
            )
        return value


_DECIMAL = re.compile(r"(?:0(?:\.\d+)?|1(?:\.0+)?)\Z")


def _decimal(value: object, field_name: str) -> Decimal:
    if type(value) is not str or _DECIMAL.fullmatch(value) is None:  # noqa: E721
        _reject("INVALID_DECIMAL", f"{field_name} must be a canonical decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise VlmResponseRejected(
            "INVALID_DECIMAL", f"{field_name} must be a canonical decimal string"
        ) from error
    if not result.is_finite() or not Decimal("0") <= result <= Decimal("1"):
        _reject("INVALID_DECIMAL", f"{field_name} must be between zero and one")
    return result


def _tick(value: object, field_name: str) -> int:
    try:
        return require_pts(value, field_name)
    except ValueError as error:
        raise VlmResponseRejected("INVALID_PTS", str(error)) from error


EnumT = TypeVar("EnumT")


def _enum(value: object, enum_type: type[EnumT], field_name: str) -> EnumT:
    if type(value) is not str:  # noqa: E721
        _reject("UNKNOWN_ENUM_VALUE", f"{field_name} is not registered")
    try:
        return enum_type(value)  # type: ignore[call-arg,return-value]
    except (TypeError, ValueError) as error:
        raise VlmResponseRejected(
            "UNKNOWN_ENUM_VALUE", f"{field_name} is not registered"
        ) from error


def _bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:  # noqa: E721
        _reject("INVALID_RESPONSE_SCHEMA", f"{field_name} must be boolean")
    return value


def _local_id(value: object, field_name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None:  # noqa: E721
        _reject("INVALID_LOCAL_ID", f"{field_name} must be a canonical provider-local ID")
    return value


def _local_ids(items: list[object], field_name: str, id_field: str) -> tuple[str, ...]:
    identifiers: list[str] = []
    for position, value in enumerate(items):
        if type(value) is not dict:  # noqa: E721
            _reject("INVALID_RESPONSE_SCHEMA", f"{field_name}[{position}] must be an object")
        mapping = cast(dict[str, object], value)
        if id_field not in mapping:
            _reject("MISSING_RESPONSE_FIELD", f"{field_name}[{position}] is missing {id_field}")
        identifiers.append(_local_id(mapping[id_field], f"{field_name}[{position}].{id_field}"))
    if len(identifiers) != len(set(identifiers)):
        _reject("DUPLICATE_LOCAL_ID", f"{field_name} contains duplicate local IDs")
    return tuple(identifiers)


def _refs(
    value: object,
    field_name: str,
    identities: dict[str, str],
    *,
    nonempty: bool = False,
) -> tuple[str, ...]:
    values = _array(value, field_name)
    if nonempty and not values:
        _reject("INVALID_REFERENCE", f"{field_name} must be non-empty")
    if any(type(item) is not str for item in values):  # noqa: E721
        _reject("INVALID_REFERENCE", f"{field_name} must contain local ID strings")
    local_refs = tuple(cast(str, item) for item in values)
    if len(local_refs) != len(set(local_refs)):
        _reject("DUPLICATE_REFERENCE", f"{field_name} must be unique")
    unknown = tuple(item for item in local_refs if item not in identities)
    if unknown:
        _reject("UNKNOWN_REFERENCE", f"{field_name} contains unknown local IDs: {unknown}")
    return tuple(sorted(identities[item] for item in local_refs))


def _canonical_enums(value: object, field_name: str, enum_type: type[EnumT]) -> tuple[EnumT, ...]:
    raw_values = _array(value, field_name)
    values = tuple(_enum(item, enum_type, f"{field_name}[]") for item in raw_values)
    if not values or len(values) != len(set(values)):
        _reject("NONCANONICAL_ENUM_SET", f"{field_name} must be non-empty and unique")
    # LLM output may not respect the enum declaration order.  Coerce to
    # canonical order rather than rejecting — enum *sets* are unordered by
    # nature, and the downstream consumers already receive a sorted tuple.
    return tuple(item for item in enum_type if item in values)  # type: ignore[union-attr]


def _support_fields(
    *,
    proxy_interval_value: object,
    frame_ids_value: object,
    confidence_value: object,
    field_name: str,
    manifest: WindowManifest,
    manifest_set: WindowManifestSet,
) -> VlmSemanticSupport:
    interval = _closed(
        proxy_interval_value,
        frozenset({"start_pts", "end_pts", "uncertainty_pts"}),
        f"{field_name}.proxy_interval",
    )
    start = _tick(interval["start_pts"], f"{field_name}.proxy_interval.start_pts")
    end = _tick(interval["end_pts"], f"{field_name}.proxy_interval.end_pts")
    uncertainty = _tick(interval["uncertainty_pts"], f"{field_name}.proxy_interval.uncertainty_pts")
    if uncertainty < 0:
        _reject("INVALID_PTS", f"{field_name}.proxy_interval.uncertainty_pts is negative")
    try:
        proxy_range = TickRange(start, end)
        mapped = manifest.timeline_map.map_interval(
            proxy_range, provider_uncertainty_proxy_pts=uncertainty
        )
    except ValueError as error:
        raise VlmResponseRejected("OUT_OF_BOUNDS_INTERVAL", str(error)) from error
    if not manifest.source_range.contains(mapped.coarse_range):
        _reject("OUT_OF_BOUNDS_INTERVAL", f"{field_name} maps outside the Kernel window")
    raw_frames = _array(frame_ids_value, f"{field_name}.supporting_frame_ids")
    if not raw_frames or any(type(item) is not str for item in raw_frames):  # noqa: E721
        _reject(
            "INVALID_FRAME_REFS",
            f"{field_name}.supporting_frame_ids must contain frame ID strings",
        )
    frame_ids = tuple(cast(str, item) for item in raw_frames)
    if len(frame_ids) != len(set(frame_ids)):
        _reject("INVALID_FRAME_REFS", f"{field_name}.supporting_frame_ids must be unique")
    frame_index = manifest.frame_by_id
    if any(frame_id not in frame_index for frame_id in frame_ids):
        _reject("UNKNOWN_FRAME_ID", f"{field_name} references a non-allowlisted frame ID")
    if not any(
        mapped.coarse_range.start_pts
        <= frame_index[frame_id].source_pts
        < mapped.coarse_range.end_pts
        for frame_id in frame_ids
    ):
        _reject("FRAME_INTERVAL_MISMATCH", f"{field_name} has no frame in its interval")
    try:
        owner = select_core_owner(manifest_set, mapped.coarse_range)
    except ValueError as error:
        raise VlmResponseRejected("INVALID_CORE_OWNER", str(error)) from error
    return VlmSemanticSupport(
        proxy_interval=VlmProxyInterval(proxy_range, uncertainty),
        supporting_frame_ids=tuple(sorted(frame_ids)),
        confidence=_decimal(confidence_value, f"{field_name}.confidence"),
        source_interval=mapped,
        core_owner_window_manifest_sha256=owner.canonical_hash,
    )


def _support(
    value: object,
    field_name: str,
    manifest: WindowManifest,
    manifest_set: WindowManifestSet,
) -> VlmSemanticSupport:
    mapping = _closed(
        value,
        frozenset({"proxy_interval", "supporting_frame_ids", "confidence"}),
        field_name,
    )
    return _support_fields(
        proxy_interval_value=mapping["proxy_interval"],
        frame_ids_value=mapping["supporting_frame_ids"],
        confidence_value=mapping["confidence"],
        field_name=field_name,
        manifest=manifest,
        manifest_set=manifest_set,
    )


def _check_count(values: list[object], maximum: int, field_name: str) -> None:
    if len(values) > maximum:
        raise VlmResponseIndeterminate(
            "STRUCTURE_BUDGET_EXCEEDED", f"{field_name} exceeds its frozen item budget"
        )


def parse_vlm_response(
    raw_response: bytes,
    *,
    manifest: WindowManifest,
    manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity,
    policy: VlmParsePolicy,
) -> VlmSemanticPack:
    """Parse exact provider bytes and derive the complete persisted v3 authority."""

    if type(raw_response) is not bytes:  # noqa: E721
        _reject("INVALID_RAW_RESPONSE", "raw_response must be exact bytes")
    if type(manifest) is not WindowManifest or type(manifest_set) is not WindowManifestSet:  # noqa: E721
        raise VlmValidationError("parser requires exact WindowManifest values")
    if type(request_identity) is not VlmRequestIdentity:  # noqa: E721
        raise VlmValidationError("request_identity must be a VlmRequestIdentity")
    if type(policy) is not VlmParsePolicy:  # noqa: E721
        raise VlmValidationError("policy must be a VlmParsePolicy")
    request_identity.assert_manifest_binding(manifest, manifest_set)
    if request_identity.parse_policy_sha256 != policy.canonical_hash:
        raise VlmValidationError("request identity does not bind the supplied parse policy")
    try:
        return _parse_provider_response(
            raw_response,
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=request_identity,
            policy=policy,
        )
    except VlmValidationError as error:
        raise VlmResponseRejected(
            "SEMANTIC_PACK_INVARIANT_VIOLATION",
            str(error),
        ) from error


def _parse_provider_response(
    raw_response: bytes,
    *,
    manifest: WindowManifest,
    manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity,
    policy: VlmParsePolicy,
) -> VlmSemanticPack:
    """Decode all provider-controlled values behind one rejection boundary."""

    if len(raw_response) > policy.max_response_bytes:
        raise VlmResponseIndeterminate(
            "RESPONSE_BUDGET_EXCEEDED", "raw response exceeds the frozen byte budget"
        )
    try:
        payload = cast(
            object,
            json.loads(
                raw_response.decode("utf-8", "strict"),
                object_pairs_hook=_pairs_object,
                parse_float=Decimal,
                parse_int=int,
                parse_constant=_constant,
            ),
        )
    except UnicodeDecodeError as error:
        raise VlmResponseRejected("INVALID_JSON", "response is not valid UTF-8") from error
    except json.JSONDecodeError as error:
        raise VlmResponseRejected("INVALID_JSON", "response is not strict JSON") from error
    root = _closed(
        payload,
        frozenset(
            {
                "schema_version",
                "window_summary",
                "continuity",
                "entities",
                "facts",
                "events",
                "candidate_hypotheses",
            }
        ),
        "response",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 3:  # noqa: E721
        _reject("UNSUPPORTED_SCHEMA_VERSION", "schema_version must be integer 3")
    budget = _TextBudget(policy)
    request_hash = request_identity.canonical_hash

    raw_entities = _array(root["entities"], "entities")
    raw_facts = _array(root["facts"], "facts")
    raw_events = _array(root["events"], "events")
    raw_candidates = _array(root["candidate_hypotheses"], "candidate_hypotheses")
    _check_count(raw_entities, policy.max_entities, "entities")
    _check_count(raw_facts, policy.max_facts, "facts")
    _check_count(raw_events, policy.max_events, "events")
    _check_count(raw_candidates, policy.max_candidate_hypotheses, "candidate_hypotheses")
    if not raw_facts:
        _reject("EMPTY_FACTS", "facts must contain at least one visible fact")

    entity_locals = _local_ids(raw_entities, "entities", "local_entity_id")
    fact_locals = _local_ids(raw_facts, "facts", "local_fact_id")
    event_locals = _local_ids(raw_events, "events", "local_event_id")
    _local_ids(raw_candidates, "candidate_hypotheses", "local_candidate_id")
    entity_ids = {
        local_id: derive_vlm_global_id("entity", local_id, request_hash)
        for local_id in entity_locals
    }
    fact_ids = {
        local_id: derive_vlm_global_id("fact", local_id, request_hash) for local_id in fact_locals
    }
    event_ids = {
        local_id: derive_vlm_global_id("event", local_id, request_hash) for local_id in event_locals
    }

    entities: list[VlmEntity] = []
    for position, value in enumerate(raw_entities):
        field = f"entities[{position}]"
        item = _closed(
            value,
            frozenset(
                {
                    "local_entity_id",
                    "entity_kind",
                    "display_label",
                    "visual_description",
                    "support",
                }
            ),
            field,
        )
        local_id = _local_id(item["local_entity_id"], f"{field}.local_entity_id")
        entities.append(
            VlmEntity(
                entity_id=entity_ids[local_id],
                local_entity_id=local_id,
                entity_kind=_enum(item["entity_kind"], VlmEntityKind, f"{field}.entity_kind"),
                display_label=cast(
                    str, budget.text(item["display_label"], f"{field}.display_label")
                ),
                visual_description=cast(
                    str,
                    budget.text(item["visual_description"], f"{field}.visual_description"),
                ),
                support=_support(item["support"], f"{field}.support", manifest, manifest_set),
            )
        )

    facts: list[VlmFact] = []
    for position, value in enumerate(raw_facts):
        field = f"facts[{position}]"
        item = _closed(
            value,
            frozenset(
                {
                    "local_fact_id",
                    "fact_kind",
                    "subject_ref",
                    "object_ref",
                    "summary",
                    "support",
                }
            ),
            field,
        )
        local_id = _local_id(item["local_fact_id"], f"{field}.local_fact_id")
        subject_local = _local_id(item["subject_ref"], f"{field}.subject_ref")
        if subject_local not in entity_ids:
            _reject("UNKNOWN_REFERENCE", f"{field}.subject_ref is not closed")
        object_value = item["object_ref"]
        object_ref: str | None = None
        if object_value is not None:
            object_local = _local_id(object_value, f"{field}.object_ref")
            if object_local not in entity_ids:
                _reject("UNKNOWN_REFERENCE", f"{field}.object_ref is not closed")
            object_ref = entity_ids[object_local]
        facts.append(
            VlmFact(
                fact_id=fact_ids[local_id],
                local_fact_id=local_id,
                fact_kind=_enum(item["fact_kind"], VlmFactKind, f"{field}.fact_kind"),
                subject_ref=entity_ids[subject_local],
                object_ref=object_ref,
                summary=cast(str, budget.text(item["summary"], f"{field}.summary")),
                support=_support(item["support"], f"{field}.support", manifest, manifest_set),
            )
        )

    events: list[VlmEvent] = []
    for position, value in enumerate(raw_events):
        field = f"events[{position}]"
        item = _closed(
            value,
            frozenset(
                {
                    "local_event_id",
                    "event_kind",
                    "summary",
                    "participant_refs",
                    "fact_refs",
                    "cause_event_refs",
                    "effect_event_refs",
                    "open_question",
                    "temporal_mode",
                    "support",
                }
            ),
            field,
        )
        local_id = _local_id(item["local_event_id"], f"{field}.local_event_id")
        events.append(
            VlmEvent(
                event_id=event_ids[local_id],
                local_event_id=local_id,
                event_kind=_enum(item["event_kind"], VlmEventKind, f"{field}.event_kind"),
                summary=cast(str, budget.text(item["summary"], f"{field}.summary")),
                participant_refs=_refs(
                    item["participant_refs"], f"{field}.participant_refs", entity_ids
                ),
                fact_refs=_refs(item["fact_refs"], f"{field}.fact_refs", fact_ids, nonempty=True),
                cause_event_refs=_refs(
                    item["cause_event_refs"], f"{field}.cause_event_refs", event_ids
                ),
                effect_event_refs=_refs(
                    item["effect_event_refs"], f"{field}.effect_event_refs", event_ids
                ),
                open_question=budget.text(
                    item["open_question"], f"{field}.open_question", nullable=True
                ),
                temporal_mode=_enum(
                    item["temporal_mode"], VlmTemporalMode, f"{field}.temporal_mode"
                ),
                support=_support(item["support"], f"{field}.support", manifest, manifest_set),
            )
        )

    summary_value = _closed(
        root["window_summary"],
        frozenset({"summary", "dominant_temporal_mode", "fact_refs", "event_refs", "confidence"}),
        "window_summary",
    )
    window_summary = VlmWindowSummary(
        summary=cast(str, budget.text(summary_value["summary"], "window_summary.summary")),
        dominant_temporal_mode=_enum(
            summary_value["dominant_temporal_mode"],
            VlmTemporalMode,
            "window_summary.dominant_temporal_mode",
        ),
        fact_refs=_refs(summary_value["fact_refs"], "window_summary.fact_refs", fact_ids),
        event_refs=_refs(summary_value["event_refs"], "window_summary.event_refs", event_ids),
        confidence=_decimal(summary_value["confidence"], "window_summary.confidence"),
    )

    continuity_value = _closed(
        root["continuity"],
        frozenset(
            {
                "starts_mid_event",
                "ends_mid_event",
                "continues_from_previous",
                "continues_into_next",
                "entry_state_fact_refs",
                "exit_state_fact_refs",
                "temporal_segments",
            }
        ),
        "continuity",
    )
    raw_segments = _array(continuity_value["temporal_segments"], "continuity.temporal_segments")
    _check_count(raw_segments, policy.max_temporal_segments, "continuity.temporal_segments")
    segments: list[VlmTemporalSegment] = []
    for position, value in enumerate(raw_segments):
        field = f"continuity.temporal_segments[{position}]"
        item = _closed(
            value,
            frozenset({"proxy_interval", "mode", "summary", "supporting_frame_ids", "confidence"}),
            field,
        )
        segments.append(
            VlmTemporalSegment(
                mode=_enum(item["mode"], VlmTemporalMode, f"{field}.mode"),
                summary=cast(str, budget.text(item["summary"], f"{field}.summary")),
                support=_support_fields(
                    proxy_interval_value=item["proxy_interval"],
                    frame_ids_value=item["supporting_frame_ids"],
                    confidence_value=item["confidence"],
                    field_name=field,
                    manifest=manifest,
                    manifest_set=manifest_set,
                ),
            )
        )
    continuity = VlmContinuity(
        starts_mid_event=_bool(continuity_value["starts_mid_event"], "continuity.starts_mid_event"),
        ends_mid_event=_bool(continuity_value["ends_mid_event"], "continuity.ends_mid_event"),
        continues_from_previous=_bool(
            continuity_value["continues_from_previous"], "continuity.continues_from_previous"
        ),
        continues_into_next=_bool(
            continuity_value["continues_into_next"], "continuity.continues_into_next"
        ),
        entry_state_fact_refs=_refs(
            continuity_value["entry_state_fact_refs"],
            "continuity.entry_state_fact_refs",
            fact_ids,
        ),
        exit_state_fact_refs=_refs(
            continuity_value["exit_state_fact_refs"],
            "continuity.exit_state_fact_refs",
            fact_ids,
        ),
        temporal_segments=tuple(segments),
    )

    candidates: list[VlmCandidateHypothesis] = []
    total_measurements = 0
    for position, value in enumerate(raw_candidates):
        field = f"candidate_hypotheses[{position}]"
        item = _closed(
            value,
            frozenset(
                {
                    "local_candidate_id",
                    "candidate_kind",
                    "anchor_event_ref",
                    "supporting_event_refs",
                    "context_event_refs",
                    "payoff_event_refs",
                    "open_question",
                    "reason",
                    "anchor_summary",
                    "payoff_or_open_question",
                    "dialogue_excerpt",
                    "editing_modes",
                    "narrative_functions",
                    "tags",
                    "measurements",
                    "support",
                }
            ),
            field,
        )
        local_id = _local_id(item["local_candidate_id"], f"{field}.local_candidate_id")
        anchor_local = _local_id(item["anchor_event_ref"], f"{field}.anchor_event_ref")
        if anchor_local not in event_ids:
            _reject("UNKNOWN_REFERENCE", f"{field}.anchor_event_ref is not closed")
        candidate_kind = _enum(item["candidate_kind"], VlmCandidateKind, f"{field}.candidate_kind")
        payoff_event_refs = _refs(
            item["payoff_event_refs"], f"{field}.payoff_event_refs", event_ids
        )
        open_question = budget.text(item["open_question"], f"{field}.open_question", nullable=True)
        if candidate_kind is VlmCandidateKind.HOOK:
            if open_question is None or payoff_event_refs:
                _reject(
                    "INVALID_CANDIDATE_KIND_RULE",
                    f"{field} hook requires open_question and empty payoff_event_refs",
                )
        elif not payoff_event_refs:
            _reject(
                "INVALID_CANDIDATE_KIND_RULE",
                f"{field} highlight requires non-empty payoff_event_refs",
            )
        raw_measurements = _array(item["measurements"], f"{field}.measurements")
        if not raw_measurements:
            _reject("EMPTY_MEASUREMENTS", f"{field}.measurements must be non-empty")
        total_measurements += len(raw_measurements)
        if total_measurements > policy.max_measurements:
            raise VlmResponseIndeterminate(
                "STRUCTURE_BUDGET_EXCEEDED", "measurements exceed the frozen item budget"
            )
        measurements: list[VlmSemanticMeasurement] = []
        for measurement_position, measurement_value in enumerate(raw_measurements):
            measurement_field = f"{field}.measurements[{measurement_position}]"
            measurement = _closed(
                measurement_value,
                frozenset({"measurement_kind", "value", "confidence", "fact_refs", "event_refs"}),
                measurement_field,
            )
            fact_refs = _refs(measurement["fact_refs"], f"{measurement_field}.fact_refs", fact_ids)
            measurement_event_refs = _refs(
                measurement["event_refs"], f"{measurement_field}.event_refs", event_ids
            )
            if not fact_refs and not measurement_event_refs:
                _reject(
                    "EMPTY_MEASUREMENT_SUPPORT",
                    f"{measurement_field} refs must be non-empty collectively",
                )
            measurements.append(
                VlmSemanticMeasurement(
                    measurement_kind=_enum(
                        measurement["measurement_kind"],
                        VlmMeasurementKind,
                        f"{measurement_field}.measurement_kind",
                    ),
                    value=_decimal(measurement["value"], f"{measurement_field}.value"),
                    confidence=_decimal(
                        measurement["confidence"], f"{measurement_field}.confidence"
                    ),
                    fact_refs=fact_refs,
                    event_refs=measurement_event_refs,
                )
            )
        candidates.append(
            VlmCandidateHypothesis(
                candidate_id=derive_vlm_global_id("candidate", local_id, request_hash),
                local_candidate_id=local_id,
                candidate_kind=candidate_kind,
                anchor_event_ref=event_ids[anchor_local],
                supporting_event_refs=_refs(
                    item["supporting_event_refs"],
                    f"{field}.supporting_event_refs",
                    event_ids,
                ),
                context_event_refs=_refs(
                    item["context_event_refs"], f"{field}.context_event_refs", event_ids
                ),
                payoff_event_refs=payoff_event_refs,
                open_question=open_question,
                reason=cast(str, budget.text(item["reason"], f"{field}.reason")),
                anchor_summary=cast(
                    str, budget.text(item["anchor_summary"], f"{field}.anchor_summary")
                ),
                payoff_or_open_question=cast(
                    str,
                    budget.text(
                        item["payoff_or_open_question"],
                        f"{field}.payoff_or_open_question",
                    ),
                ),
                dialogue_excerpt=budget.text(
                    item["dialogue_excerpt"], f"{field}.dialogue_excerpt", nullable=True
                ),
                editing_modes=_canonical_enums(
                    item["editing_modes"], f"{field}.editing_modes", VlmEditingMode
                ),
                narrative_functions=_canonical_enums(
                    item["narrative_functions"],
                    f"{field}.narrative_functions",
                    VlmNarrativeFunction,
                ),
                tags=_canonical_enums(item["tags"], f"{field}.tags", VlmCandidateTag),
                measurements=tuple(measurements),
                support=_support(item["support"], f"{field}.support", manifest, manifest_set),
            )
        )

    raw_hash = "sha256:" + hashlib.sha256(raw_response).hexdigest()
    return VlmSemanticPack(
        request_identity_sha256=request_hash,
        window_manifest_sha256=manifest.canonical_hash,
        raw_response_sha256=raw_hash,
        window_summary=window_summary,
        continuity=continuity,
        entities=tuple(sorted(entities, key=lambda item: item.local_entity_id)),
        facts=tuple(sorted(facts, key=lambda item: item.local_fact_id)),
        events=tuple(sorted(events, key=lambda item: item.local_event_id)),
        candidate_hypotheses=tuple(
            sorted(candidates, key=lambda item: item.local_candidate_id)
        ),
    )
