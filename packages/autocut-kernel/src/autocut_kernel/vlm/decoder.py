"""Strict decoder for an already committed :class:`VlmObservationSet` payload.

Provider response parsing and persisted Artifact decoding are separate trust
boundaries.  This module never calls a provider and never repairs JSON; it
reconstructs the exact immutable Kernel value and rechecks every derived
observation identity before a downstream Command may consume it.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import cast

from ..media.types import TickRange, TimeBase, canonical_sha256, require_pts
from .models import (
    MappedSourceInterval,
    VlmObservation,
    VlmObservationKind,
    VlmObservationSet,
    VlmValidationError,
)


def _closed(
    value: object,
    fields: frozenset[str],
    field_name: str,
) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721 - subclasses are not accepted at trust boundary
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


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value.isspace():  # noqa: E721
        raise VlmValidationError(f"{field_name} must be non-empty text")
    return value


def _time_base(value: object, field_name: str) -> TimeBase:
    mapping = _closed(
        value,
        frozenset({"denominator", "numerator"}),
        field_name,
    )
    try:
        return TimeBase(
            require_pts(mapping["numerator"], f"{field_name}.numerator"),
            require_pts(mapping["denominator"], f"{field_name}.denominator"),
        )
    except ValueError as error:
        raise VlmValidationError(str(error)) from error


def _non_negative_tick(value: object, field_name: str) -> int:
    try:
        tick = require_pts(value, field_name)
    except ValueError as error:
        raise VlmValidationError(str(error)) from error
    if tick < 0:
        raise VlmValidationError(f"{field_name} must be non-negative")
    return tick


def _source_interval(value: object, field_name: str) -> MappedSourceInterval:
    mapping = _closed(
        value,
        frozenset(
            {
                "coarse_range",
                "mapping_error_bound",
                "provider_uncertainty",
                "semantic_precision",
            }
        ),
        field_name,
    )
    if mapping["semantic_precision"] != "coarse_only":
        raise VlmValidationError(f"{field_name}.semantic_precision must be coarse_only")
    coarse = _closed(
        mapping["coarse_range"],
        frozenset({"end_pts", "start_pts", "time_base"}),
        f"{field_name}.coarse_range",
    )
    mapping_error = _closed(
        mapping["mapping_error_bound"],
        frozenset({"clock", "tick", "time_base"}),
        f"{field_name}.mapping_error_bound",
    )
    provider_uncertainty = _closed(
        mapping["provider_uncertainty"],
        frozenset({"clock", "tick", "time_base"}),
        f"{field_name}.provider_uncertainty",
    )
    if mapping_error["clock"] != "source" or provider_uncertainty["clock"] != "proxy":
        raise VlmValidationError(f"{field_name} clock labels are invalid")
    source_time_base = _time_base(
        coarse["time_base"], f"{field_name}.coarse_range.time_base"
    )
    if _time_base(
        mapping_error["time_base"], f"{field_name}.mapping_error_bound.time_base"
    ) != source_time_base:
        raise VlmValidationError(f"{field_name} source time bases must match")
    proxy_time_base = _time_base(
        provider_uncertainty["time_base"],
        f"{field_name}.provider_uncertainty.time_base",
    )
    try:
        coarse_range = TickRange(
            require_pts(coarse["start_pts"], f"{field_name}.coarse_range.start_pts"),
            require_pts(coarse["end_pts"], f"{field_name}.coarse_range.end_pts"),
        )
        return MappedSourceInterval(
            coarse_range=coarse_range,
            mapping_error_bound_source_pts=_non_negative_tick(
                mapping_error["tick"], f"{field_name}.mapping_error_bound.tick"
            ),
            source_time_base=source_time_base,
            provider_uncertainty_proxy_pts=_non_negative_tick(
                provider_uncertainty["tick"],
                f"{field_name}.provider_uncertainty.tick",
            ),
            proxy_time_base=proxy_time_base,
        )
    except ValueError as error:
        raise VlmValidationError(str(error)) from error


def _confidence(value: object, field_name: str) -> Decimal:
    if type(value) is not str:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise VlmValidationError(f"{field_name} is invalid") from error
    if not result.is_finite() or not Decimal("0") <= result <= Decimal("1"):
        raise VlmValidationError(f"{field_name} must be between zero and one")
    if format(result, "f") != value:
        raise VlmValidationError(f"{field_name} must use canonical fixed-point syntax")
    return result


def _observation(value: object, position: int) -> VlmObservation:
    field_name = f"observations[{position}]"
    mapping = _closed(
        value,
        frozenset(
            {
                "confidence",
                "core_owned",
                "kind",
                "observation_id",
                "provenance",
                "source_interval",
                "summary",
                "supporting_frame_ids",
            }
        ),
        field_name,
    )
    try:
        kind = VlmObservationKind(_text(mapping["kind"], f"{field_name}.kind"))
    except ValueError as error:
        raise VlmValidationError(f"{field_name}.kind is not registered") from error
    provenance = _closed(
        mapping["provenance"],
        frozenset({"request_identity_sha256", "window_manifest_sha256"}),
        f"{field_name}.provenance",
    )
    frame_values = _array(
        mapping["supporting_frame_ids"], f"{field_name}.supporting_frame_ids"
    )
    if any(type(item) is not str for item in frame_values):  # noqa: E721
        raise VlmValidationError(f"{field_name}.supporting_frame_ids must contain strings")
    frame_ids = tuple(item for item in frame_values if type(item) is str)
    if not frame_ids or frame_ids != tuple(sorted(frame_ids)) or len(frame_ids) != len(
        set(frame_ids)
    ):
        raise VlmValidationError(
            f"{field_name}.supporting_frame_ids must be non-empty, sorted, and unique"
        )
    core_owned = mapping["core_owned"]
    if type(core_owned) is not bool:  # noqa: E721
        raise VlmValidationError(f"{field_name}.core_owned must be boolean")
    source_interval = _source_interval(mapping["source_interval"], f"{field_name}.source_interval")
    confidence = _confidence(mapping["confidence"], f"{field_name}.confidence")
    summary = _text(mapping["summary"], f"{field_name}.summary")
    request_identity_sha256 = _text(
        provenance["request_identity_sha256"],
        f"{field_name}.provenance.request_identity_sha256",
    )
    window_manifest_sha256 = _text(
        provenance["window_manifest_sha256"],
        f"{field_name}.provenance.window_manifest_sha256",
    )
    identity_payload = {
        "confidence": format(confidence, "f"),
        "kind": kind.value,
        "request_identity_sha256": request_identity_sha256,
        "source_interval": source_interval.to_mapping(),
        "summary": summary,
        "supporting_frame_ids": list(frame_ids),
    }
    observation_id = _text(mapping["observation_id"], f"{field_name}.observation_id")
    if observation_id != canonical_sha256(identity_payload):
        raise VlmValidationError(f"{field_name}.observation_id is not derivable")
    try:
        return VlmObservation(
            observation_id=observation_id,
            kind=kind,
            summary=summary,
            confidence=confidence,
            supporting_frame_ids=frame_ids,
            source_interval=source_interval,
            request_identity_sha256=request_identity_sha256,
            window_manifest_sha256=window_manifest_sha256,
            core_owned=core_owned,
        )
    except ValueError as error:
        raise VlmValidationError(str(error)) from error


def decode_vlm_observation_set(value: object) -> VlmObservationSet:
    """Rebuild and independently verify one committed observation-set mapping."""

    root = _closed(
        value,
        frozenset({"observations", "provenance", "schema_version"}),
        "observation_set",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:  # noqa: E721
        raise VlmValidationError("observation_set.schema_version must be integer 1")
    provenance = _closed(
        root["provenance"],
        frozenset(
            {"raw_response_sha256", "request_identity_sha256", "window_manifest_sha256"}
        ),
        "observation_set.provenance",
    )
    observations = tuple(
        _observation(item, position)
        for position, item in enumerate(_array(root["observations"], "observations"))
    )
    try:
        result = VlmObservationSet(
            request_identity_sha256=_text(
                provenance["request_identity_sha256"],
                "observation_set.provenance.request_identity_sha256",
            ),
            window_manifest_sha256=_text(
                provenance["window_manifest_sha256"],
                "observation_set.provenance.window_manifest_sha256",
            ),
            raw_response_sha256=_text(
                provenance["raw_response_sha256"],
                "observation_set.provenance.raw_response_sha256",
            ),
            observations=observations,
        )
    except ValueError as error:
        raise VlmValidationError(str(error)) from error
    if result.to_mapping() != root:
        raise VlmValidationError("observation_set is not in canonical persisted form")
    return result


__all__ = ["decode_vlm_observation_set"]
