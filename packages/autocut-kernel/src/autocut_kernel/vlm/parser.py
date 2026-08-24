"""Strict unknown-JSON parser for coarse VLM observations."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import NoReturn, cast

from ..media.types import TickRange, canonical_sha256, require_pts
from .models import (
    VlmContractError,
    VlmObservation,
    VlmObservationKind,
    VlmObservationSet,
    VlmParsePolicy,
    VlmRequestIdentity,
    VlmValidationError,
)
from .window import WindowManifest, WindowManifestSet, select_core_owner


class VlmResponseRejected(VlmContractError):  # noqa: N818 - closed outcome vocabulary is intentional.
    """The provider response violated the closed response contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class VlmResponseIndeterminate(VlmContractError):  # noqa: N818 - closed outcome vocabulary is intentional.
    """The response was valid enough to inspect but cannot become evidence."""

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


def _closed_object(value: object, expected: frozenset[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        _reject("INVALID_RESPONSE_SCHEMA", f"{field_name} must be an object")
    result = cast(dict[str, object], value)
    keys = frozenset(result)
    extra = keys - expected
    missing = expected - keys
    if extra:
        _reject("UNKNOWN_RESPONSE_FIELD", f"{field_name} contains unknown fields: {sorted(extra)}")
    if missing:
        _reject("MISSING_RESPONSE_FIELD", f"{field_name} is missing fields: {sorted(missing)}")
    return result


def _decimal(value: object, field_name: str) -> Decimal:
    if type(value) is bool or value is None:  # noqa: E721
        _reject("INVALID_CONFIDENCE", f"{field_name} must be an exact decimal")
    try:
        if type(value) is Decimal:  # noqa: E721
            result = value
        elif type(value) is int:  # noqa: E721
            result = Decimal(value)
        elif type(value) is str:  # noqa: E721
            result = Decimal(value)
        else:
            _reject("INVALID_CONFIDENCE", f"{field_name} must be an exact decimal")
    except InvalidOperation:
        _reject("INVALID_CONFIDENCE", f"{field_name} must be an exact decimal")
    if not result.is_finite() or not Decimal("0") <= result <= Decimal("1"):
        _reject("INVALID_CONFIDENCE", f"{field_name} must be between 0 and 1")
    return result


def _tick(value: object, field_name: str) -> int:
    try:
        return require_pts(value, field_name)
    except ValueError as error:
        raise VlmResponseRejected("INVALID_TICK", str(error)) from error


def _summary(value: object, policy: VlmParsePolicy, position: int) -> str:
    if type(value) is not str or not value or value.isspace():  # noqa: E721
        _reject("INVALID_SUMMARY", f"observations[{position}].summary must be non-empty text")
    if len(value) > policy.max_summary_characters:
        raise VlmResponseIndeterminate("SUMMARY_BUDGET_EXCEEDED", "an observation exceeds the frozen summary budget")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _reject("INVALID_SUMMARY", f"observations[{position}].summary contains control characters")
    return value


def parse_vlm_response(
    raw_response: bytes,
    *,
    manifest: WindowManifest,
    manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity,
    policy: VlmParsePolicy,
) -> VlmObservationSet:
    """Parse exact provider bytes into a complete provenance-bound observation set.

    Contract violations are rejected.  Low confidence and resource-budget
    exhaustion are explicit indeterminate outcomes.  Neither path returns an
    empty set that could be confused with a successful "no content" result.
    """

    if type(raw_response) is not bytes:  # noqa: E721
        _reject("INVALID_RAW_RESPONSE", "raw_response must be exact bytes")
    if type(manifest) is not WindowManifest:  # noqa: E721
        raise VlmValidationError("manifest must be a WindowManifest")
    if type(manifest_set) is not WindowManifestSet:  # noqa: E721
        raise VlmValidationError("manifest_set must be a WindowManifestSet")
    if type(request_identity) is not VlmRequestIdentity:  # noqa: E721
        raise VlmValidationError("request_identity must be a VlmRequestIdentity")
    if type(policy) is not VlmParsePolicy:  # noqa: E721
        raise VlmValidationError("policy must be a VlmParsePolicy")
    request_identity.assert_manifest_binding(manifest, manifest_set)
    if request_identity.parse_policy_sha256 != policy.canonical_hash:
        raise VlmValidationError("request identity does not bind the supplied parse policy")
    if len(raw_response) > policy.max_response_bytes:
        raise VlmResponseIndeterminate("RESPONSE_BUDGET_EXCEEDED", "raw response exceeds the frozen byte budget")
    try:
        decoded = raw_response.decode("utf-8", "strict")
        payload = cast(
            object,
            json.loads(
                decoded,
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

    root = _closed_object(payload, frozenset({"schema_version", "observations"}), "response")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:  # noqa: E721
        _reject("UNSUPPORTED_SCHEMA_VERSION", "schema_version must be integer 1")
    response_observations_value = root["observations"]
    if type(response_observations_value) is not list:  # noqa: E721
        _reject("INVALID_RESPONSE_SCHEMA", "observations must be an array")
    response_observations = cast(list[object], response_observations_value)
    if not response_observations:
        raise VlmResponseIndeterminate("EMPTY_OBSERVATIONS", "an empty response cannot prove no semantic content")
    if len(response_observations) > policy.max_observations:
        raise VlmResponseIndeterminate("OBSERVATION_BUDGET_EXCEEDED", "response exceeds the frozen observation budget")

    frame_index = manifest.frame_by_id
    parsed: list[VlmObservation] = []
    total_summary_characters = 0
    for position, raw_observation in enumerate(response_observations):
        item = _closed_object(
            raw_observation,
            frozenset({"confidence", "kind", "proxy_interval", "summary", "supporting_frame_ids"}),
            f"observations[{position}]",
        )
        kind_value = item["kind"]
        if type(kind_value) is not str:  # noqa: E721
            _reject("UNKNOWN_OBSERVATION_KIND", "observation kind is not registered")
        try:
            kind = VlmObservationKind(kind_value)
        except (TypeError, ValueError) as error:
            raise VlmResponseRejected("UNKNOWN_OBSERVATION_KIND", "observation kind is not registered") from error
        summary = _summary(item["summary"], policy, position)
        total_summary_characters += len(summary)
        if total_summary_characters > policy.max_total_summary_characters:
            raise VlmResponseIndeterminate("SUMMARY_BUDGET_EXCEEDED", "response exceeds the frozen total summary budget")
        confidence = _decimal(item["confidence"], f"observations[{position}].confidence")
        if confidence < policy.minimum_confidence:
            raise VlmResponseIndeterminate("LOW_CONFIDENCE", "response contains an observation below the frozen threshold")

        interval = _closed_object(
            item["proxy_interval"],
            frozenset({"end_pts", "start_pts", "uncertainty_pts"}),
            f"observations[{position}].proxy_interval",
        )
        start_pts = _tick(interval["start_pts"], f"observations[{position}].proxy_interval.start_pts")
        end_pts = _tick(interval["end_pts"], f"observations[{position}].proxy_interval.end_pts")
        uncertainty_pts = _tick(interval["uncertainty_pts"], f"observations[{position}].proxy_interval.uncertainty_pts")
        if uncertainty_pts < 0:
            _reject("INVALID_TICK", "proxy interval uncertainty must be non-negative")
        try:
            proxy_interval = TickRange(start_pts, end_pts)
            mapped = manifest.timeline_map.map_interval(
                proxy_interval,
                provider_uncertainty_proxy_pts=uncertainty_pts,
            )
        except ValueError as error:
            raise VlmResponseRejected("OUT_OF_BOUNDS_INTERVAL", str(error)) from error
        if not manifest.source_range.contains(mapped.coarse_range):
            _reject("OUT_OF_BOUNDS_INTERVAL", "mapped observation is outside the Kernel window")

        raw_frame_ids_value = item["supporting_frame_ids"]
        if type(raw_frame_ids_value) is not list or not raw_frame_ids_value:  # noqa: E721
            _reject("INVALID_FRAME_REFS", "supporting_frame_ids must be a non-empty array")
        raw_frame_ids = cast(list[object], raw_frame_ids_value)
        if any(type(frame_id) is not str for frame_id in raw_frame_ids):  # noqa: E721
            _reject("INVALID_FRAME_REFS", "supporting frame ids must be strings")
        frame_ids = tuple(cast(str, frame_id) for frame_id in raw_frame_ids)
        if len(frame_ids) != len(set(frame_ids)):
            _reject("INVALID_FRAME_REFS", "supporting frame ids must be unique")
        unknown = sorted(set(frame_ids) - set(frame_index))
        if unknown:
            _reject("UNKNOWN_FRAME_ID", f"response references unknown frames: {unknown}")
        if not any(
            mapped.coarse_range.start_pts <= frame_index[frame_id].source_pts < mapped.coarse_range.end_pts
            for frame_id in frame_ids
        ):
            _reject("FRAME_INTERVAL_MISMATCH", "no supporting frame lies within the mapped coarse interval")

        identity_payload = {
            "confidence": format(confidence, "f"),
            "kind": kind.value,
            "request_identity_sha256": request_identity.canonical_hash,
            "source_interval": mapped.to_mapping(),
            "summary": summary,
            "supporting_frame_ids": sorted(frame_ids),
        }
        parsed.append(
            VlmObservation(
                observation_id=canonical_sha256(identity_payload),
                kind=kind,
                summary=summary,
                confidence=confidence,
                supporting_frame_ids=tuple(sorted(frame_ids)),
                source_interval=mapped,
                request_identity_sha256=request_identity.canonical_hash,
                window_manifest_sha256=manifest.canonical_hash,
                core_owned=(
                    select_core_owner(manifest_set, mapped.coarse_range).canonical_hash
                    == manifest.canonical_hash
                ),
            )
        )

    ids = tuple(item.observation_id for item in parsed)
    if len(ids) != len(set(ids)):
        _reject("DUPLICATE_OBSERVATION", "response contains duplicate semantic observations")
    raw_response_sha256 = f"sha256:{hashlib.sha256(raw_response).hexdigest()}"
    return VlmObservationSet(
        request_identity_sha256=request_identity.canonical_hash,
        window_manifest_sha256=manifest.canonical_hash,
        raw_response_sha256=raw_response_sha256,
        observations=tuple(parsed),
    )
