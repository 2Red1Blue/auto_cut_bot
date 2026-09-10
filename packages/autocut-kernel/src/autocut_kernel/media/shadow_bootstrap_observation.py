"""Anchor-free, raw-bound shadow observations.

This is deliberately a reporting boundary.  It projects native ASR/VAD output
onto source ticks, but has neither calibration anchors nor a path to accepted
timing bounds, a calibration record, or authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import cast

from .types import TickRange, TimeBase, canonical_sha256, sha256_prefixed

SHADOW_BOOTSTRAP_OBSERVATION_SCHEMA = "shadow-bootstrap-observation-v1"
SHADOW_BOOTSTRAP_OBSERVATION_REQUEST_SCHEMA = "shadow-bootstrap-observation-request-v1"
SHADOW_BOOTSTRAP_OBSERVATION_RAW_RESPONSE_SCHEMA = (
    "shadow-bootstrap-observation-funasr-raw-response-v1"
)


class ShadowBootstrapObservationError(ValueError):
    """The anchor-free response is malformed or cannot be bound to its request."""


def _invalid(detail: str) -> ShadowBootstrapObservationError:
    return ShadowBootstrapObservationError(f"shadow bootstrap observation invalid: {detail}")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise _invalid(f"{name} must be non-empty text")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise _invalid(f"{name} must be UTF-8 text") from error
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int:  # noqa: E721
        raise _invalid(f"{name} must be an exact integer")
    return value


def _sha(value: object, name: str) -> str:
    try:
        return sha256_prefixed(value, name)
    except ValueError as error:
        raise _invalid(str(error)) from error


def _object(value: object, fields: frozenset[str], name: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[str, object], value)) != fields:  # noqa: E721
        raise _invalid(f"{name} schema is not closed")
    return cast(dict[str, object], value)


def _strict_json_object(raw: bytes) -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _invalid("raw response contains a duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(constant: str) -> object:
        raise ValueError(f"non-finite JSON constant {constant!r}")

    def finite_float(value: str) -> float:
        number = float(value)
        if number == float("inf") or number == float("-inf") or number != number:
            raise ValueError("non-finite JSON number")
        return number

    try:
        value: object = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as error:
        if isinstance(error, ShadowBootstrapObservationError):
            raise
        raise _invalid("raw response must be strict finite UTF-8 JSON") from error
    if type(value) is not dict:  # noqa: E721
        raise _invalid("raw response root must be an object")
    return cast(dict[str, object], value)


def _time_base(value: object, name: str) -> TimeBase:
    raw = _object(value, frozenset({"numerator", "denominator"}), name)
    try:
        return TimeBase(_integer(raw["numerator"], f"{name}.numerator"),
                        _integer(raw["denominator"], f"{name}.denominator"))
    except ValueError as error:
        raise _invalid(str(error)) from error


def _range(value: object, name: str) -> TickRange:
    raw = _object(value, frozenset({"in_tick", "out_tick"}), name)
    try:
        return TickRange(_integer(raw["in_tick"], f"{name}.in_tick"),
                         _integer(raw["out_tick"], f"{name}.out_tick"))
    except ValueError as error:
        raise _invalid(str(error)) from error


def _time_base_mapping(value: TimeBase) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _range_mapping(value: TickRange) -> dict[str, int]:
    return {"in_tick": value.start_pts, "out_tick": value.end_pts}


@dataclass(frozen=True, slots=True)
class ShadowBootstrapObservationSource:
    """Exact source and audio-clock identity, without any calibration identity."""

    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    source_range: TickRange

    def __post_init__(self) -> None:
        _text(self.source_id, "source.source_id")
        _sha(self.source_sha256, "source.source_sha256")
        _text(self.clock_id, "source.clock_id")
        if type(self.time_base) is not TimeBase or type(self.source_range) is not TickRange:  # noqa: E721
            raise _invalid("source clock and range must be exact typed values")

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "audio_clock": {
                "clock_id": self.clock_id,
                "time_base": _time_base_mapping(self.time_base),
                "origin_tick": self.source_range.start_pts,
                "duration_tick": self.source_range.duration_pts,
            },
        }

    def identity_mapping(self) -> dict[str, str]:
        return {"source_id": self.source_id, "source_sha256": self.source_sha256}

    def audio_clock_mapping(self) -> dict[str, object]:
        return self.to_mapping()["audio_clock"]  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class ShadowBootstrapObservationRequest:
    """Closed request identity for an untrusted bootstrap observation."""

    source: ShadowBootstrapObservationSource
    requested_range: TickRange
    kernel_max_source_bytes: int
    service_max_request_bytes: int
    max_response_bytes: int

    def __post_init__(self) -> None:
        if type(self.source) is not ShadowBootstrapObservationSource:  # noqa: E721
            raise _invalid("request source must be exact")
        if type(self.requested_range) is not TickRange or not self.source.source_range.contains(self.requested_range):  # noqa: E721
            raise _invalid("request range must be an exact range inside its source")
        for name in (
            "kernel_max_source_bytes",
            "service_max_request_bytes",
            "max_response_bytes",
        ):
            if _integer(getattr(self, name), f"request.{name}") <= 0:
                raise _invalid(f"request.{name} must be positive")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SHADOW_BOOTSTRAP_OBSERVATION_REQUEST_SCHEMA,
            "source": self.source.identity_mapping(),
            "source_byte_limits": {
                "kernel_max_source_bytes": self.kernel_max_source_bytes,
                "service_max_request_bytes": self.service_max_request_bytes,
                "effective_max_source_bytes": min(
                    self.kernel_max_source_bytes,
                    self.service_max_request_bytes,
                ),
            },
            "container": {"media_type": "video/mp4", "safe_suffix": ".mp4"},
            "audio_clock": self.source.audio_clock_mapping(),
            "requested_range": _range_mapping(self.requested_range),
            "response_limits": {"max_response_bytes": self.max_response_bytes},
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ShadowBootstrapAsrObservation:
    observation_id: str
    text: str
    source: ShadowBootstrapObservationSource
    observed_range: TickRange

    def __post_init__(self) -> None:
        _text(self.observation_id, "ASR observation ID")
        _text(self.text, "ASR observation text")
        if type(self.source) is not ShadowBootstrapObservationSource or type(self.observed_range) is not TickRange:  # noqa: E721
            raise _invalid("ASR observation source and range must be exact")
        if not self.source.source_range.contains(self.observed_range):
            raise _invalid("ASR observation lies outside its source")

    def to_mapping(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "text": self.text,
            "source_id": self.source.source_id,
            "source_sha256": self.source.source_sha256,
            "clock_id": self.source.clock_id,
            "time_base": _time_base_mapping(self.source.time_base),
            "observed_range": _range_mapping(self.observed_range),
        }


@dataclass(frozen=True, slots=True)
class ShadowBootstrapVadObservation:
    observation_id: str
    source: ShadowBootstrapObservationSource
    observed_range: TickRange

    def __post_init__(self) -> None:
        _text(self.observation_id, "VAD observation ID")
        if type(self.source) is not ShadowBootstrapObservationSource or type(self.observed_range) is not TickRange:  # noqa: E721
            raise _invalid("VAD observation source and range must be exact")
        if not self.source.source_range.contains(self.observed_range):
            raise _invalid("VAD observation lies outside its source")

    def to_mapping(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "source_id": self.source.source_id,
            "source_sha256": self.source.source_sha256,
            "clock_id": self.source.clock_id,
            "time_base": _time_base_mapping(self.source.time_base),
            "observed_range": _range_mapping(self.observed_range),
        }


@dataclass(frozen=True, slots=True)
class DecodedShadowBootstrapObservationResponse:
    request: ShadowBootstrapObservationRequest
    raw_response: bytes
    raw_response_sha256: str
    asr_observations: tuple[ShadowBootstrapAsrObservation, ...]
    vad_observations: tuple[ShadowBootstrapVadObservation, ...]

    def __post_init__(self) -> None:
        if type(self.request) is not ShadowBootstrapObservationRequest or type(self.raw_response) is not bytes:  # noqa: E721
            raise _invalid("decoded response requires exact request and raw bytes")
        if not self.raw_response or len(self.raw_response) > self.request.max_response_bytes:
            raise _invalid("decoded response bytes violate the request bound")
        if _sha(self.raw_response_sha256, "decoded raw response SHA-256") != (
            "sha256:" + hashlib.sha256(self.raw_response).hexdigest()
        ):
            raise _invalid("decoded raw response SHA-256 differs from bytes")
        for observations, expected_type, name in (
            (self.asr_observations, ShadowBootstrapAsrObservation, "ASR"),
            (self.vad_observations, ShadowBootstrapVadObservation, "VAD"),
        ):
            if type(observations) is not tuple or any(type(item) is not expected_type for item in observations):  # noqa: E721
                raise _invalid(f"decoded {name} observations must be exact tuples")


@dataclass(frozen=True, slots=True)
class ShadowBootstrapObservationResult:
    """Raw-bound observations that are explicitly ineligible for authority."""

    decoded: DecodedShadowBootstrapObservationResponse
    trust_status: str = "untrusted"
    authority_eligible: bool = False
    independent_anchor_count: int = 0

    def __post_init__(self) -> None:
        if type(self.decoded) is not DecodedShadowBootstrapObservationResponse:  # noqa: E721
            raise _invalid("result decoded response must be exact")
        if self.trust_status != "untrusted":
            raise _invalid("bootstrap observations must remain untrusted")
        if self.authority_eligible is not False:
            raise _invalid("bootstrap observations are never authority eligible")
        if type(self.independent_anchor_count) is not int or self.independent_anchor_count != 0:  # noqa: E721
            raise _invalid("bootstrap observations have exactly zero independent anchors")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SHADOW_BOOTSTRAP_OBSERVATION_SCHEMA,
            "request": self.decoded.request.to_mapping(),
            "raw_response_sha256": self.decoded.raw_response_sha256,
            "raw_response_byte_length": len(self.decoded.raw_response),
            "asr_observations": [item.to_mapping() for item in self.decoded.asr_observations],
            "vad_observations": [item.to_mapping() for item in self.decoded.vad_observations],
            "trust_status": self.trust_status,
            "authority_eligible": self.authority_eligible,
            "independent_anchor_count": self.independent_anchor_count,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _ticks_from_ms(start_ms: int, end_ms: int, request: ShadowBootstrapObservationRequest) -> TickRange:
    if start_ms < 0 or start_ms >= end_ms:
        raise _invalid("native millisecond interval is invalid")
    time_base = request.source.time_base
    scale = 1_000 * time_base.numerator
    start_tick = request.requested_range.start_pts + start_ms * time_base.denominator // scale
    end_tick = request.requested_range.start_pts + (
        end_ms * time_base.denominator + scale - 1
    ) // scale
    try:
        interval = TickRange(start_tick, end_tick)
    except ValueError as error:
        raise _invalid("native millisecond interval has no positive source-tick range") from error
    if not request.requested_range.contains(interval):
        raise _invalid("native millisecond interval escapes the requested source range")
    return interval


def _native_pairs(value: object, name: str, *, reject_overlap: bool) -> tuple[tuple[int, int], ...]:
    if type(value) is not list:  # noqa: E721
        raise _invalid(f"{name} must be a native array")
    pairs: list[tuple[int, int]] = []
    for position, raw_pair in enumerate(cast(list[object], value)):
        if type(raw_pair) is not list or len(cast(list[object], raw_pair)) != 2:  # noqa: E721
            raise _invalid(f"{name}[{position}] must be a two-integer pair")
        pair = cast(list[object], raw_pair)
        start = _integer(pair[0], f"{name}[{position}][0]")
        end = _integer(pair[1], f"{name}[{position}][1]")
        if start < 0 or start >= end or (pairs and pairs[-1][0] > start):
            raise _invalid(f"{name} must be ordered positive-length pairs")
        if reject_overlap and pairs and pairs[-1][1] > start:
            raise _invalid(f"{name} must not overlap")
        pairs.append((start, end))
    return tuple(pairs)


def _decode_source(value: object) -> ShadowBootstrapObservationSource:
    raw = _object(value, frozenset({"source_id", "source_sha256", "audio_clock"}), "response.source")
    clock = _object(raw["audio_clock"], frozenset({"clock_id", "time_base", "origin_tick", "duration_tick"}), "response.source.audio_clock")
    origin = _integer(clock["origin_tick"], "response.source.audio_clock.origin_tick")
    duration = _integer(clock["duration_tick"], "response.source.audio_clock.duration_tick")
    if duration <= 0:
        raise _invalid("response.source.audio_clock.duration_tick must be positive")
    try:
        source_range = TickRange(origin, origin + duration)
    except ValueError as error:
        raise _invalid("response source clock range is invalid") from error
    return ShadowBootstrapObservationSource(
        _text(raw["source_id"], "response.source.source_id"),
        _sha(raw["source_sha256"], "response.source.source_sha256"),
        _text(clock["clock_id"], "response.source.audio_clock.clock_id"),
        _time_base(clock["time_base"], "response.source.audio_clock.time_base"),
        source_range,
    )


def _decode_asr(value: object, request: ShadowBootstrapObservationRequest) -> tuple[ShadowBootstrapAsrObservation, ...]:
    if type(value) is not list or len(cast(list[object], value)) != 1:  # noqa: E721
        raise _invalid("response.asr_native_output must contain exactly one result")
    raw = _object(cast(list[object], value)[0], frozenset({"text", "words", "timestamp"}), "response.asr_native_output[0]")
    text = raw["text"]
    words_value = raw["words"]
    if type(text) is not str or type(words_value) is not list:  # noqa: E721
        raise _invalid("response ASR text and words must use exact native syntax")
    pairs = _native_pairs(raw["timestamp"], "response.asr_native_output[0].timestamp", reject_overlap=True)
    words = cast(list[object], words_value)
    if not text.strip():
        if words or pairs:
            raise _invalid("empty ASR text must have empty words and timestamps")
        return ()
    if not words or len(words) != len(pairs):
        raise _invalid("response ASR words and timestamps must have equal nonzero length")
    observations = tuple(
        ShadowBootstrapAsrObservation(
            f"asr-word-{position:08d}",
            _text(word, f"response ASR word[{position}]"),
            request.source,
            _ticks_from_ms(start, end, request),
        )
        for position, ((start, end), word) in enumerate(zip(pairs, words, strict=True))
    )
    if any(left.observed_range.end_pts > right.observed_range.start_pts for left, right in zip(observations, observations[1:], strict=False)):
        raise _invalid("ASR observations overlap after source-tick conversion")
    return observations


def _decode_vad(value: object, request: ShadowBootstrapObservationRequest) -> tuple[ShadowBootstrapVadObservation, ...]:
    if type(value) is not list or len(cast(list[object], value)) != 1:  # noqa: E721
        raise _invalid("response.vad_native_output must contain exactly one result")
    raw = _object(cast(list[object], value)[0], frozenset({"value"}), "response.vad_native_output[0]")
    pairs = _native_pairs(raw["value"], "response.vad_native_output[0].value", reject_overlap=False)
    return tuple(
        ShadowBootstrapVadObservation(
            f"vad-segment-{position:08d}", request.source, _ticks_from_ms(start, end, request)
        )
        for position, (start, end) in enumerate(pairs)
    )


def decode_shadow_bootstrap_observation_response(
    raw_response: bytes, request: ShadowBootstrapObservationRequest
) -> DecodedShadowBootstrapObservationResponse:
    """Strictly decode one raw bootstrap response and bind it to the request."""
    if type(raw_response) is not bytes or type(request) is not ShadowBootstrapObservationRequest:  # noqa: E721
        raise _invalid("response decoding requires exact bytes and request")
    if not raw_response or len(raw_response) > request.max_response_bytes:
        raise _invalid("raw response violates the explicit request byte bound")
    raw = _strict_json_object(raw_response)
    response = _object(
        raw,
        frozenset(
            {
                "schema_version",
                "status",
                "authority_eligible",
                "independent_anchor_count",
                "request_identity_sha256",
                "source",
                "requested_range",
                "producer_identities",
                "asr_native_output",
                "vad_native_output",
            }
        ),
        "response",
    )
    if response["schema_version"] != SHADOW_BOOTSTRAP_OBSERVATION_RAW_RESPONSE_SCHEMA:
        raise _invalid("response schema is not the bootstrap raw envelope")
    if _sha(response["request_identity_sha256"], "response.request_identity_sha256") != request.canonical_hash:
        raise _invalid("response request identity drift")
    if (
        response["status"] != "untrusted"
        or response["authority_eligible"] is not False
        or response["independent_anchor_count"] != 0
        or type(response["producer_identities"]) is not list
    ):
        raise _invalid("response attempts to claim calibration authority")
    if _decode_source(response["source"]) != request.source:
        raise _invalid("response source identity drift")
    if _range(response["requested_range"], "response.requested_range") != request.requested_range:
        raise _invalid("response requested range drift")
    return DecodedShadowBootstrapObservationResponse(
        request,
        raw_response,
        "sha256:" + hashlib.sha256(raw_response).hexdigest(),
        _decode_asr(response["asr_native_output"], request),
        _decode_vad(response["vad_native_output"], request),
    )


def encode_shadow_bootstrap_observation_response(
    request: ShadowBootstrapObservationRequest, asr_native_output: object, vad_native_output: object
) -> bytes:
    """Encode only native-shaped observations that the strict decoder accepts."""
    if type(request) is not ShadowBootstrapObservationRequest:  # noqa: E721
        raise _invalid("response encoding requires an exact request")
    response = {
        "schema_version": SHADOW_BOOTSTRAP_OBSERVATION_RAW_RESPONSE_SCHEMA,
        "status": "untrusted",
        "authority_eligible": False,
        "independent_anchor_count": 0,
        "request_identity_sha256": request.canonical_hash,
        "source": request.source.to_mapping(),
        "requested_range": _range_mapping(request.requested_range),
        "producer_identities": [],
        "asr_native_output": asr_native_output,
        "vad_native_output": vad_native_output,
    }
    try:
        raw = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise _invalid("native output is not finite JSON") from error
    # Replay so encoding has the same closure as decoding and cannot bless malformed native values.
    decode_shadow_bootstrap_observation_response(raw, request)
    return raw


def project_shadow_bootstrap_observation(
    decoded: DecodedShadowBootstrapObservationResponse,
) -> ShadowBootstrapObservationResult:
    """Replay raw bytes into a permanently untrusted, anchor-free result."""
    if type(decoded) is not DecodedShadowBootstrapObservationResponse:  # noqa: E721
        raise _invalid("projection requires an exact decoded response")
    replayed = decode_shadow_bootstrap_observation_response(decoded.raw_response, decoded.request)
    if replayed != decoded:
        raise _invalid("decoded response differs from independent raw replay")
    return ShadowBootstrapObservationResult(replayed)


__all__ = [
    "DecodedShadowBootstrapObservationResponse",
    "SHADOW_BOOTSTRAP_OBSERVATION_RAW_RESPONSE_SCHEMA",
    "SHADOW_BOOTSTRAP_OBSERVATION_REQUEST_SCHEMA",
    "SHADOW_BOOTSTRAP_OBSERVATION_SCHEMA",
    "ShadowBootstrapAsrObservation",
    "ShadowBootstrapObservationError",
    "ShadowBootstrapObservationRequest",
    "ShadowBootstrapObservationResult",
    "ShadowBootstrapObservationSource",
    "ShadowBootstrapVadObservation",
    "decode_shadow_bootstrap_observation_response",
    "encode_shadow_bootstrap_observation_response",
    "project_shadow_bootstrap_observation",
]
