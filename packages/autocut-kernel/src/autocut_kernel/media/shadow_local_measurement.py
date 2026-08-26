"""Replayable local measurement content, independent of Store and acceptance.

The response identity and byte length describe the supplied original bytes.
They are NOT a BlobRef: Pipeline/Store must separately bind a real blob's UUID,
owner, media type and immutable bytes when staging this content. No Receipt,
accepted timing bound, calibration Record or deployment permission is produced.
Local source ticks may be negative and genuine zero errors remain zero.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import cast

from .calibration import CalibrationAnchorMatch
from .local_speech_window import LocalSpeechWindowRequest
from .local_speech_window_codec import decode_local_speech_window_request
from .root_evidence_codec import decode_media_evidence_json
from .shadow_local_calibration import ShadowLocalCalibrationCase
from .shadow_local_calibration_projection import (
    ShadowLocalCalibrationProjection,
    project_shadow_local_calibration,
)
from .types import canonical_sha256

SHADOW_LOCAL_MEASUREMENT_EVIDENCE_SCHEMA = "shadow-local-measurement-evidence-v1"


class ShadowLocalMeasurementError(ValueError):
    """Measurement content disagrees with exact inputs or raw-byte replay."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ShadowLocalMeasurementError("measurement must contain canonical media JSON values") from error


def _raw_bytes(value: object) -> bytes:
    if type(value) is not bytes or not value:
        raise ShadowLocalMeasurementError("measurement requires nonempty original response bytes")
    return value


def _match_mapping(match: CalibrationAnchorMatch) -> dict[str, object]:
    """Serialize the existing match's derived errors without acceptance logic."""
    expected, observed = match.anchor.expected_range, match.observation.observed_range
    return {
        "anchor_id": match.anchor.anchor_id,
        "observation_id": match.observation.observation_id,
        "expected_range": {"start_pts": expected.start_pts, "end_pts": expected.end_pts},
        "observed_range": {"start_pts": observed.start_pts, "end_pts": observed.end_pts},
        "early_tick": match.early_tick,
        "late_tick": match.late_tick,
        "absolute_tick": match.absolute_tick,
    }


def _projection_mapping(projection: ShadowLocalCalibrationProjection) -> dict[str, object]:
    return {
        "transcript": projection.transcript.to_mapping(),
        "speech_activity": projection.speech_activity.to_mapping(),
        "asr_matches": [_match_mapping(match) for match in projection.asr_matches],
        "vad_matches": [_match_mapping(match) for match in projection.vad_matches],
    }


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementEvidence:
    """Pure measurements always reconstructed from the exact case/request/raw.

    The derived projection cannot be supplied by a producer or restored merely
    because a persisted JSON payload has a matching content hash. Its original
    response bytes must be supplied again and independently replayed.
    """

    case: ShadowLocalCalibrationCase
    request: LocalSpeechWindowRequest
    raw_response: bytes
    projection: ShadowLocalCalibrationProjection = field(init=False)

    def __post_init__(self) -> None:
        if type(self.case) is not ShadowLocalCalibrationCase or type(self.request) is not LocalSpeechWindowRequest:
            raise ShadowLocalMeasurementError("measurement requires exact local case and request")
        raw = _raw_bytes(self.raw_response)
        if len(raw) > self.request.max_response_bytes:
            raise ShadowLocalMeasurementError("measurement response exceeds frozen request byte limit")
        try:
            projection = project_shadow_local_calibration(raw, case=self.case, request=self.request)
        except ValueError as error:
            raise ShadowLocalMeasurementError("measurement failed independent local raw projection") from error
        object.__setattr__(self, "projection", projection)

    @property
    def raw_response_sha256(self) -> str:
        return self.projection.response_sha256

    @property
    def byte_length(self) -> int:
        return len(self.raw_response)

    @property
    def projection_sha256(self) -> str:
        return canonical_sha256(_projection_mapping(self.projection))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SHADOW_LOCAL_MEASUREMENT_EVIDENCE_SCHEMA,
            "case": self.case.to_mapping(),
            "request": self.request.to_mapping(),
            "raw_response_sha256": self.raw_response_sha256,
            "raw_response_byte_length": self.byte_length,
            "projection": _projection_mapping(self.projection),
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_mapping(cls, value: object, *, raw_response: bytes) -> ShadowLocalMeasurementEvidence:
        """Validate persisted content against raw bytes, not their Store origin."""
        fields = {"schema_version", "case", "request", "raw_response_sha256",
                  "raw_response_byte_length", "projection"}
        if type(value) is not dict:
            raise ShadowLocalMeasurementError("measurement must be an exact object")
        raw = cast(dict[object, object], value)
        if any(type(key) is not str for key in raw) or set(raw) != fields:
            raise ShadowLocalMeasurementError("measurement object has missing or unknown fields")
        mapping = cast(dict[str, object], value)
        if (type(mapping["schema_version"]) is not str
                or mapping["schema_version"] != SHADOW_LOCAL_MEASUREMENT_EVIDENCE_SCHEMA):
            raise ShadowLocalMeasurementError("unsupported local measurement schema")
        response = _raw_bytes(raw_response)
        if (type(mapping["raw_response_byte_length"]) is not int
                or mapping["raw_response_byte_length"] != len(response)
                or type(mapping["raw_response_sha256"]) is not str
                or mapping["raw_response_sha256"] != "sha256:" + hashlib.sha256(response).hexdigest()):
            raise ShadowLocalMeasurementError("measurement raw-response identity or length drift")
        try:
            measured = cls(
                ShadowLocalCalibrationCase.from_mapping(mapping["case"]),
                decode_local_speech_window_request(mapping["request"]),
                response,
            )
        except ValueError as error:
            raise ShadowLocalMeasurementError("measurement input or raw projection does not close") from error
        # Canonical byte equality, not Python equality (False == 0 == 0.0).
        # Checking the complete reconstructed payload also rejects extra nested
        # projection fields without a parallel decoder for derived observations.
        if _canonical_bytes(mapping) != measured.to_bytes():
            raise ShadowLocalMeasurementError("measurement content differs from independent recomputation")
        return measured


def decode_shadow_local_measurement_evidence(
    raw: bytes, *, raw_response: bytes, max_bytes: int,
) -> ShadowLocalMeasurementEvidence:
    """Bound metadata before strict JSON parsing; raw has its request's bound."""
    try:
        mapping = decode_media_evidence_json(raw, max_bytes=max_bytes)
        return ShadowLocalMeasurementEvidence.from_mapping(mapping, raw_response=raw_response)
    except ValueError as error:
        raise ShadowLocalMeasurementError("invalid bounded local measurement evidence") from error
