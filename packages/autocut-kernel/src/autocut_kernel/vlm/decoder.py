"""Strict decoder for a committed VLM Semantic Pack v3 mapping."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TypeVar, cast

from ..media.types import TickRange, TimeBase, require_pts
from .models import (
    MappedSourceInterval,
    VlmCandidateHypothesis,
    VlmCandidateKind,
    VlmCandidateTag,
    VlmContinuity,
    VlmEditingMode,
    VlmEntity,
    VlmEntityKind,
    VlmEvent,
    VlmEventKind,
    VlmFact,
    VlmFactKind,
    VlmMeasurementKind,
    VlmNarrativeFunction,
    VlmProxyInterval,
    VlmSemanticMeasurement,
    VlmSemanticPack,
    VlmSemanticSupport,
    VlmTemporalMode,
    VlmTemporalSegment,
    VlmValidationError,
    VlmWindowSummary,
)


def _closed(value: object, fields: frozenset[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be an object")
    mapping = cast(dict[object, object], value)
    if any(type(key) is not str for key in mapping):  # noqa: E721
        raise VlmValidationError(f"{field_name} field names must be strings")
    result = cast(dict[str, object], mapping)
    if frozenset(result) != fields:
        raise VlmValidationError(f"{field_name} does not match its closed schema")
    return result


def _array(value: object, field_name: str) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be an array")
    return cast(list[object], value)


def _text(value: object, field_name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value or value.isspace():  # noqa: E721
        raise VlmValidationError(f"{field_name} must be non-empty text")
    return value


def _bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be boolean")
    return value


EnumT = TypeVar("EnumT", bound=Enum)


def _enum(value: object, enum_type: type[EnumT], field_name: str) -> EnumT:
    text = _text(value, field_name)
    try:
        return enum_type(text)
    except ValueError as error:
        raise VlmValidationError(f"{field_name} is not registered") from error


_DECIMAL = re.compile(r"(?:0(?:\.\d+)?|1(?:\.0+)?)\Z")


def _decimal(value: object, field_name: str) -> Decimal:
    if type(value) is not str or _DECIMAL.fullmatch(value) is None:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be a canonical decimal string")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise VlmValidationError(f"{field_name} is not a valid decimal") from error


def _tick(value: object, field_name: str, *, nonnegative: bool = False) -> int:
    try:
        result = require_pts(value, field_name)
    except ValueError as error:
        raise VlmValidationError(str(error)) from error
    if nonnegative and result < 0:
        raise VlmValidationError(f"{field_name} must be non-negative")
    return result


def _refs(value: object, field_name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    values = _array(value, field_name)
    if any(type(item) is not str for item in values):  # noqa: E721
        raise VlmValidationError(f"{field_name} must contain strings")
    result = tuple(cast(str, item) for item in values)
    if nonempty and not result:
        raise VlmValidationError(f"{field_name} must be non-empty")
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise VlmValidationError(f"{field_name} must be sorted and unique")
    return result


def _time_base(value: object, field_name: str) -> TimeBase:
    mapping = _closed(value, frozenset({"numerator", "denominator"}), field_name)
    try:
        return TimeBase(
            _tick(mapping["numerator"], f"{field_name}.numerator"),
            _tick(mapping["denominator"], f"{field_name}.denominator"),
        )
    except ValueError as error:
        raise VlmValidationError(str(error)) from error


def _source_interval(value: object, field_name: str) -> MappedSourceInterval:
    mapping = _closed(
        value,
        frozenset(
            {"coarse_range", "mapping_error_bound", "provider_uncertainty", "semantic_precision"}
        ),
        field_name,
    )
    if mapping["semantic_precision"] != "coarse_only":
        raise VlmValidationError(f"{field_name}.semantic_precision must be coarse_only")
    coarse = _closed(
        mapping["coarse_range"],
        frozenset({"start_pts", "end_pts", "time_base"}),
        f"{field_name}.coarse_range",
    )
    mapping_error = _closed(
        mapping["mapping_error_bound"],
        frozenset({"clock", "tick", "time_base"}),
        f"{field_name}.mapping_error_bound",
    )
    uncertainty = _closed(
        mapping["provider_uncertainty"],
        frozenset({"clock", "tick", "time_base"}),
        f"{field_name}.provider_uncertainty",
    )
    if mapping_error["clock"] != "source" or uncertainty["clock"] != "proxy":
        raise VlmValidationError(f"{field_name} clock labels are invalid")
    source_base = _time_base(coarse["time_base"], f"{field_name}.coarse_range.time_base")
    if (
        _time_base(mapping_error["time_base"], f"{field_name}.mapping_error_bound.time_base")
        != source_base
    ):
        raise VlmValidationError(f"{field_name} source time bases must match")
    try:
        coarse_range = TickRange(
            _tick(coarse["start_pts"], f"{field_name}.coarse_range.start_pts"),
            _tick(coarse["end_pts"], f"{field_name}.coarse_range.end_pts"),
        )
    except ValueError as error:
        raise VlmValidationError(str(error)) from error
    return MappedSourceInterval(
        coarse_range=coarse_range,
        mapping_error_bound_source_pts=_tick(
            mapping_error["tick"], f"{field_name}.mapping_error_bound.tick", nonnegative=True
        ),
        source_time_base=source_base,
        provider_uncertainty_proxy_pts=_tick(
            uncertainty["tick"], f"{field_name}.provider_uncertainty.tick", nonnegative=True
        ),
        proxy_time_base=_time_base(
            uncertainty["time_base"], f"{field_name}.provider_uncertainty.time_base"
        ),
    )


def _proxy_interval(value: object, field_name: str) -> VlmProxyInterval:
    mapping = _closed(value, frozenset({"start_pts", "end_pts", "uncertainty_pts"}), field_name)
    try:
        proxy_range = TickRange(
            _tick(mapping["start_pts"], f"{field_name}.start_pts"),
            _tick(mapping["end_pts"], f"{field_name}.end_pts"),
        )
    except ValueError as error:
        raise VlmValidationError(str(error)) from error
    return VlmProxyInterval(
        proxy_range,
        _tick(mapping["uncertainty_pts"], f"{field_name}.uncertainty_pts", nonnegative=True),
    )


_SUPPORT_FIELDS = frozenset(
    {
        "proxy_interval",
        "supporting_frame_ids",
        "confidence",
        "source_interval",
        "core_owner_window_manifest_sha256",
    }
)


def _support(value: object, field_name: str) -> VlmSemanticSupport:
    mapping = _closed(value, _SUPPORT_FIELDS, field_name)
    return VlmSemanticSupport(
        proxy_interval=_proxy_interval(mapping["proxy_interval"], f"{field_name}.proxy_interval"),
        supporting_frame_ids=_refs(
            mapping["supporting_frame_ids"],
            f"{field_name}.supporting_frame_ids",
            nonempty=True,
        ),
        confidence=_decimal(mapping["confidence"], f"{field_name}.confidence"),
        source_interval=_source_interval(
            mapping["source_interval"], f"{field_name}.source_interval"
        ),
        core_owner_window_manifest_sha256=cast(
            str,
            _text(
                mapping["core_owner_window_manifest_sha256"],
                f"{field_name}.core_owner_window_manifest_sha256",
            ),
        ),
    )


def _window_summary(value: object) -> VlmWindowSummary:
    field = "window_summary"
    mapping = _closed(
        value,
        frozenset({"summary", "dominant_temporal_mode", "fact_refs", "event_refs", "confidence"}),
        field,
    )
    return VlmWindowSummary(
        summary=cast(str, _text(mapping["summary"], f"{field}.summary")),
        dominant_temporal_mode=_enum(
            mapping["dominant_temporal_mode"],
            VlmTemporalMode,
            f"{field}.dominant_temporal_mode",
        ),
        fact_refs=_refs(mapping["fact_refs"], f"{field}.fact_refs"),
        event_refs=_refs(mapping["event_refs"], f"{field}.event_refs"),
        confidence=_decimal(mapping["confidence"], f"{field}.confidence"),
    )


def _temporal_segment(value: object, position: int) -> VlmTemporalSegment:
    field = f"continuity.temporal_segments[{position}]"
    mapping = _closed(
        value,
        frozenset(
            {
                "mode",
                "summary",
                "proxy_interval",
                "supporting_frame_ids",
                "confidence",
                "source_interval",
                "core_owner_window_manifest_sha256",
            }
        ),
        field,
    )
    support = _support(
        {
            "proxy_interval": mapping["proxy_interval"],
            "supporting_frame_ids": mapping["supporting_frame_ids"],
            "confidence": mapping["confidence"],
            "source_interval": mapping["source_interval"],
            "core_owner_window_manifest_sha256": mapping["core_owner_window_manifest_sha256"],
        },
        f"{field}.support",
    )
    return VlmTemporalSegment(
        mode=_enum(mapping["mode"], VlmTemporalMode, f"{field}.mode"),
        summary=cast(str, _text(mapping["summary"], f"{field}.summary")),
        support=support,
    )


def _continuity(value: object) -> VlmContinuity:
    field = "continuity"
    mapping = _closed(
        value,
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
        field,
    )
    return VlmContinuity(
        starts_mid_event=_bool(mapping["starts_mid_event"], f"{field}.starts_mid_event"),
        ends_mid_event=_bool(mapping["ends_mid_event"], f"{field}.ends_mid_event"),
        continues_from_previous=_bool(
            mapping["continues_from_previous"], f"{field}.continues_from_previous"
        ),
        continues_into_next=_bool(mapping["continues_into_next"], f"{field}.continues_into_next"),
        entry_state_fact_refs=_refs(
            mapping["entry_state_fact_refs"], f"{field}.entry_state_fact_refs"
        ),
        exit_state_fact_refs=_refs(
            mapping["exit_state_fact_refs"], f"{field}.exit_state_fact_refs"
        ),
        temporal_segments=tuple(
            _temporal_segment(item, position)
            for position, item in enumerate(
                _array(mapping["temporal_segments"], f"{field}.temporal_segments")
            )
        ),
    )


def _entity(value: object, position: int) -> VlmEntity:
    field = f"entities[{position}]"
    mapping = _closed(
        value,
        frozenset(
            {
                "entity_id",
                "local_entity_id",
                "entity_kind",
                "display_label",
                "visual_description",
                "support",
            }
        ),
        field,
    )
    return VlmEntity(
        entity_id=cast(str, _text(mapping["entity_id"], f"{field}.entity_id")),
        local_entity_id=cast(str, _text(mapping["local_entity_id"], f"{field}.local_entity_id")),
        entity_kind=_enum(mapping["entity_kind"], VlmEntityKind, f"{field}.entity_kind"),
        display_label=cast(str, _text(mapping["display_label"], f"{field}.display_label")),
        visual_description=cast(
            str, _text(mapping["visual_description"], f"{field}.visual_description")
        ),
        support=_support(mapping["support"], f"{field}.support"),
    )


def _fact(value: object, position: int) -> VlmFact:
    field = f"facts[{position}]"
    mapping = _closed(
        value,
        frozenset(
            {
                "fact_id",
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
    return VlmFact(
        fact_id=cast(str, _text(mapping["fact_id"], f"{field}.fact_id")),
        local_fact_id=cast(str, _text(mapping["local_fact_id"], f"{field}.local_fact_id")),
        fact_kind=_enum(mapping["fact_kind"], VlmFactKind, f"{field}.fact_kind"),
        subject_ref=cast(str, _text(mapping["subject_ref"], f"{field}.subject_ref")),
        object_ref=_text(mapping["object_ref"], f"{field}.object_ref", nullable=True),
        summary=cast(str, _text(mapping["summary"], f"{field}.summary")),
        support=_support(mapping["support"], f"{field}.support"),
    )


def _event(value: object, position: int) -> VlmEvent:
    field = f"events[{position}]"
    mapping = _closed(
        value,
        frozenset(
            {
                "event_id",
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
    return VlmEvent(
        event_id=cast(str, _text(mapping["event_id"], f"{field}.event_id")),
        local_event_id=cast(str, _text(mapping["local_event_id"], f"{field}.local_event_id")),
        event_kind=_enum(mapping["event_kind"], VlmEventKind, f"{field}.event_kind"),
        summary=cast(str, _text(mapping["summary"], f"{field}.summary")),
        participant_refs=_refs(mapping["participant_refs"], f"{field}.participant_refs"),
        fact_refs=_refs(mapping["fact_refs"], f"{field}.fact_refs"),
        cause_event_refs=_refs(mapping["cause_event_refs"], f"{field}.cause_event_refs"),
        effect_event_refs=_refs(mapping["effect_event_refs"], f"{field}.effect_event_refs"),
        open_question=_text(mapping["open_question"], f"{field}.open_question", nullable=True),
        temporal_mode=_enum(mapping["temporal_mode"], VlmTemporalMode, f"{field}.temporal_mode"),
        support=_support(mapping["support"], f"{field}.support"),
    )


def _measurement(value: object, field: str) -> VlmSemanticMeasurement:
    mapping = _closed(
        value,
        frozenset({"measurement_kind", "value", "confidence", "fact_refs", "event_refs"}),
        field,
    )
    return VlmSemanticMeasurement(
        measurement_kind=_enum(
            mapping["measurement_kind"], VlmMeasurementKind, f"{field}.measurement_kind"
        ),
        value=_decimal(mapping["value"], f"{field}.value"),
        confidence=_decimal(mapping["confidence"], f"{field}.confidence"),
        fact_refs=_refs(mapping["fact_refs"], f"{field}.fact_refs"),
        event_refs=_refs(mapping["event_refs"], f"{field}.event_refs"),
    )


def _enum_array(value: object, field_name: str, enum_type: type[EnumT]) -> tuple[EnumT, ...]:
    return tuple(_enum(item, enum_type, f"{field_name}[]") for item in _array(value, field_name))


def _candidate(value: object, position: int) -> VlmCandidateHypothesis:
    field = f"candidate_hypotheses[{position}]"
    mapping = _closed(
        value,
        frozenset(
            {
                "candidate_id",
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
    return VlmCandidateHypothesis(
        candidate_id=cast(str, _text(mapping["candidate_id"], f"{field}.candidate_id")),
        local_candidate_id=cast(
            str, _text(mapping["local_candidate_id"], f"{field}.local_candidate_id")
        ),
        candidate_kind=_enum(
            mapping["candidate_kind"], VlmCandidateKind, f"{field}.candidate_kind"
        ),
        anchor_event_ref=cast(str, _text(mapping["anchor_event_ref"], f"{field}.anchor_event_ref")),
        supporting_event_refs=_refs(
            mapping["supporting_event_refs"], f"{field}.supporting_event_refs"
        ),
        context_event_refs=_refs(mapping["context_event_refs"], f"{field}.context_event_refs"),
        payoff_event_refs=_refs(mapping["payoff_event_refs"], f"{field}.payoff_event_refs"),
        open_question=_text(mapping["open_question"], f"{field}.open_question", nullable=True),
        reason=cast(str, _text(mapping["reason"], f"{field}.reason")),
        anchor_summary=cast(str, _text(mapping["anchor_summary"], f"{field}.anchor_summary")),
        payoff_or_open_question=cast(
            str,
            _text(mapping["payoff_or_open_question"], f"{field}.payoff_or_open_question"),
        ),
        dialogue_excerpt=_text(
            mapping["dialogue_excerpt"], f"{field}.dialogue_excerpt", nullable=True
        ),
        editing_modes=_enum_array(
            mapping["editing_modes"], f"{field}.editing_modes", VlmEditingMode
        ),
        narrative_functions=_enum_array(
            mapping["narrative_functions"],
            f"{field}.narrative_functions",
            VlmNarrativeFunction,
        ),
        tags=_enum_array(mapping["tags"], f"{field}.tags", VlmCandidateTag),
        measurements=tuple(
            _measurement(item, f"{field}.measurements[{measurement_position}]")
            for measurement_position, item in enumerate(
                _array(mapping["measurements"], f"{field}.measurements")
            )
        ),
        support=_support(mapping["support"], f"{field}.support"),
    )


def decode_vlm_semantic_pack(value: object) -> VlmSemanticPack:
    """Rebuild and independently verify one canonical committed v3 mapping."""

    root = _closed(
        value,
        frozenset(
            {
                "schema_version",
                "provenance",
                "window_summary",
                "continuity",
                "entities",
                "facts",
                "events",
                "candidate_hypotheses",
            }
        ),
        "semantic_pack",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 3:  # noqa: E721
        raise VlmValidationError("semantic_pack.schema_version must be integer 3")
    provenance = _closed(
        root["provenance"],
        frozenset({"raw_response_sha256", "request_identity_sha256", "window_manifest_sha256"}),
        "semantic_pack.provenance",
    )
    try:
        result = VlmSemanticPack(
            request_identity_sha256=cast(
                str,
                _text(
                    provenance["request_identity_sha256"],
                    "semantic_pack.provenance.request_identity_sha256",
                ),
            ),
            window_manifest_sha256=cast(
                str,
                _text(
                    provenance["window_manifest_sha256"],
                    "semantic_pack.provenance.window_manifest_sha256",
                ),
            ),
            raw_response_sha256=cast(
                str,
                _text(
                    provenance["raw_response_sha256"],
                    "semantic_pack.provenance.raw_response_sha256",
                ),
            ),
            window_summary=_window_summary(root["window_summary"]),
            continuity=_continuity(root["continuity"]),
            entities=tuple(
                _entity(item, position)
                for position, item in enumerate(_array(root["entities"], "entities"))
            ),
            facts=tuple(
                _fact(item, position)
                for position, item in enumerate(_array(root["facts"], "facts"))
            ),
            events=tuple(
                _event(item, position)
                for position, item in enumerate(_array(root["events"], "events"))
            ),
            candidate_hypotheses=tuple(
                _candidate(item, position)
                for position, item in enumerate(
                    _array(root["candidate_hypotheses"], "candidate_hypotheses")
                )
            ),
        )
    except ValueError as error:
        raise VlmValidationError(str(error)) from error
    if result.to_mapping() != root:
        raise VlmValidationError("semantic_pack is not in canonical persisted form")
    return result


__all__ = ["decode_vlm_semantic_pack"]
