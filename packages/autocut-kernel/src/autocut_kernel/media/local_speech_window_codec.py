"""One strict window wire decoder shared by native service, Runtime and readers."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import cast

from .local_audio_window import LocalAudioWindowSpec
from .local_speech_window import (
    DecodedLocalPcmReport,
    LocalSpeechWindowError,
    LocalSpeechWindowPolicy,
    LocalSpeechWindowRequest,
)
from .root_evidence_codec import decode_time_base
from .types import TickRange, require_pts, sha256_prefixed


def _object(value: object, fields: tuple[str, ...], schema: str | None = None) -> dict[str, object]:
    if type(value) is not dict or set(cast(dict[object, object], value)) != set(fields):
        raise LocalSpeechWindowError("window object has missing or unknown fields")
    result = cast(dict[str, object], value)
    if schema is not None and result["schema_version"] != schema:
        raise LocalSpeechWindowError("unsupported window wire schema")
    return result


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise LocalSpeechWindowError("window text must be exact and nonempty")
    value.encode("utf-8")
    return value


def _hash(value: object) -> str:
    return sha256_prefixed(_text(value), "window hash")


def _int(value: object) -> int:
    return require_pts(value, "window integer")


def _range(value: object) -> TickRange:
    raw = _object(value, ("start_pts", "end_pts"))
    return TickRange(_int(raw["start_pts"]), _int(raw["end_pts"]))


def decode_local_audio_window_spec(value: object) -> LocalAudioWindowSpec:
    r = _object(value, (
        "schema_version", "source_id", "source_sha256", "audio_stream_index", "clock_id",
        "time_base", "source_range", "requested_range", "sample_rate", "channels",
        "audio_boundary_set_sha256", "decoder_identity_sha256", "max_source_bytes",
        "max_decode_frames", "max_frame_bytes", "max_pcm_bytes",
    ), "local-audio-window-spec-v1")
    return LocalAudioWindowSpec(
        _text(r["source_id"]), _hash(r["source_sha256"]), _int(r["audio_stream_index"]),
        _text(r["clock_id"]), decode_time_base(r["time_base"]), _range(r["source_range"]),
        _range(r["requested_range"]), _int(r["sample_rate"]), _int(r["channels"]),
        _hash(r["audio_boundary_set_sha256"]), _hash(r["decoder_identity_sha256"]),
        _int(r["max_source_bytes"]), _int(r["max_decode_frames"]),
        _int(r["max_frame_bytes"]), _int(r["max_pcm_bytes"]),
    )


def decode_decoded_local_pcm_report(value: object) -> DecodedLocalPcmReport:
    r = _object(value, (
        "schema_version", "source_sha256", "spec_sha256", "decoder_identity_sha256",
        "pcm_sha256", "wav_sha256", "wav_byte_length", "sample_rate", "channels",
        "sample_count", "decoded_frames",
    ), "decoded-local-pcm-report-v1")
    return DecodedLocalPcmReport(
        _hash(r["source_sha256"]), _hash(r["spec_sha256"]), _hash(r["decoder_identity_sha256"]),
        _hash(r["pcm_sha256"]), _hash(r["wav_sha256"]), _int(r["wav_byte_length"]),
        _int(r["sample_rate"]), _int(r["channels"]), _int(r["sample_count"]), _int(r["decoded_frames"]),
    )


def decode_local_speech_window_policy(value: object) -> LocalSpeechWindowPolicy:
    r = _object(value, (
        "schema_version", "service_profile_sha256", "asr_producer_id", "asr_generation_policy_sha256",
        "vad_producer_id", "vad_generation_policy_sha256", "utterance_gap_milliseconds",
        "vad_merge_gap_milliseconds",
    ), "local-speech-window-policy-v1")
    return LocalSpeechWindowPolicy(
        _hash(r["service_profile_sha256"]), _text(r["asr_producer_id"]),
        _hash(r["asr_generation_policy_sha256"]), _text(r["vad_producer_id"]),
        _hash(r["vad_generation_policy_sha256"]), _int(r["utterance_gap_milliseconds"]),
        _int(r["vad_merge_gap_milliseconds"]),
    )


def decode_local_speech_window_request(value: object) -> LocalSpeechWindowRequest:
    r = _object(value, ("schema_version", "extraction", "policy", "binding_sha256", "max_response_bytes"),
                "local-speech-window-request-v1")
    return LocalSpeechWindowRequest(
        decode_local_audio_window_spec(r["extraction"]), decode_local_speech_window_policy(r["policy"]),
        _hash(r["binding_sha256"]), _int(r["max_response_bytes"]),
    )


def _reject_constant(value: str) -> object:
    raise LocalSpeechWindowError(f"nonfinite JSON constant {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise LocalSpeechWindowError("overflowing JSON number")
    return number


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LocalSpeechWindowError("duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class DecodedLocalSpeechWindow:
    request: LocalSpeechWindowRequest
    report: DecodedLocalPcmReport
    asr_native_output: object
    vad_native_output: object
    response_sha256: str
    raw_response: bytes


def decode_local_speech_window_response(raw: bytes, request: LocalSpeechWindowRequest) -> DecodedLocalSpeechWindow:
    if type(request) is not LocalSpeechWindowRequest or type(raw) is not bytes:
        raise LocalSpeechWindowError("window response requires exact bytes and request")
    if not raw or len(raw) > request.max_response_bytes:
        raise LocalSpeechWindowError("window response exceeds explicit byte bound")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise LocalSpeechWindowError("window response must be bounded strict UTF-8 JSON") from error
    r = _object(value, ("schema_version", "request_sha256", "extraction_report",
                        "asr_native_output", "vad_native_output"), "local-speech-window-response-v1")
    if _hash(r["request_sha256"]) != request.canonical_hash:
        raise LocalSpeechWindowError("window response request identity drift")
    report = decode_decoded_local_pcm_report(r["extraction_report"])
    report.validate_for(request.extraction)
    return DecodedLocalSpeechWindow(request, report, r["asr_native_output"], r["vad_native_output"],
                                   "sha256:" + hashlib.sha256(raw).hexdigest(), raw)


def encode_local_speech_window_response(
    request: LocalSpeechWindowRequest, report: DecodedLocalPcmReport, asr: object, vad: object,
) -> bytes:
    report.validate_for(request.extraction)
    try:
        raw = json.dumps({
            "schema_version": "local-speech-window-response-v1", "request_sha256": request.canonical_hash,
            "extraction_report": report.to_mapping(), "asr_native_output": asr, "vad_native_output": vad,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError) as error:
        raise LocalSpeechWindowError("native response is not finite JSON") from error
    if len(raw) > request.max_response_bytes:
        raise LocalSpeechWindowError("native response exceeds explicit byte bound")
    return raw
