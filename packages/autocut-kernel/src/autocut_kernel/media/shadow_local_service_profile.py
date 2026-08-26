"""Closed pre-calibration content for the local-PCM FunASR measurement path.

The native identity covers every measured field except itself; the complete
profile identity additionally covers that derived native identity. Neither is
an accepted Record or an installation permit. The service must independently
measure model/service/decoder bytes and runtime versions before becoming ready.
No old full-source registry approval is consulted or implied here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from .calibration import CalibrationProducer
from .shadow_calibration_raw import ShadowCalibrationProducerIdentity
from .types import canonical_sha256, sha256_prefixed

SHADOW_LOCAL_SERVICE_PROFILE_SCHEMA = "funasr-shadow-local-calibration-profile-v1"

_MEASURED_FIELDS = frozenset({
    "schema_version", "provider_id", "provider_version", "service_sha256",
    "funasr_version", "torch_version", "device", "word_timing_capability",
    "max_request_bytes", "timed_speech_policy_sha256", "word_gap_policy_sha256",
    "vad_merge_policy_sha256", "utterance_gap_milliseconds",
    "vad_merge_gap_milliseconds", "decoder_identity_sha256", "producers",
})
_PRODUCER_FIELDS = frozenset({
    "producer_kind", "producer_id", "producer_version", "generation_policy_sha256",
    "detector_sha256", "calibration_policy_sha256", "model_id", "model_revision",
    "model_sha256", "service_sha256", "inference_kind",
})


class ShadowLocalServiceProfileError(ValueError):
    """The measured local service-profile content is not closed and consistent."""


def _object(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise ShadowLocalServiceProfileError("service-profile value must be an exact object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or frozenset(cast(dict[str, object], raw)) != fields:
        raise ShadowLocalServiceProfileError("service-profile object has missing or unknown fields")
    return cast(dict[str, object], value)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ShadowLocalServiceProfileError(f"{name} must be exact nonempty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ShadowLocalServiceProfileError(f"{name} must be valid UTF-8") from error
    return value


def _sha(value: object, name: str) -> str:
    try:
        result = sha256_prefixed(_text(value, name), name)
    except ValueError as error:
        raise ShadowLocalServiceProfileError(f"{name} must be a lowercase SHA-256 identity") from error
    if result == "sha256:" + "0" * 64:
        raise ShadowLocalServiceProfileError(f"{name} must not be a zero identity")
    return result


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise ShadowLocalServiceProfileError(f"{name} must be an exact safe integer >= {minimum}")
    return value


def _producer(value: object) -> ShadowCalibrationProducerIdentity:
    raw = _object(value, _PRODUCER_FIELDS)
    try:
        return ShadowCalibrationProducerIdentity(
            CalibrationProducer(_text(raw["producer_kind"], "producer kind")),
            _text(raw["producer_id"], "producer ID"),
            _text(raw["producer_version"], "producer version"),
            _sha(raw["generation_policy_sha256"], "generation policy"),
            _sha(raw["detector_sha256"], "detector"),
            _sha(raw["calibration_policy_sha256"], "calibration policy"),
            _text(raw["model_id"], "model ID"),
            _text(raw["model_revision"], "model revision"),
            _sha(raw["model_sha256"], "model hash"),
            _text(raw["inference_kind"], "inference kind"),
            _sha(raw["service_sha256"], "producer service"),
        )
    except ValueError as error:
        raise ShadowLocalServiceProfileError("invalid local service producer") from error


@dataclass(frozen=True, slots=True)
class ShadowLocalServiceProfile:
    """Expected measured content, not evidence that any measurement occurred."""

    provider_id: str
    provider_version: str
    service_sha256: str
    funasr_version: str
    torch_version: str
    device: str
    word_timing_capability: str
    max_request_bytes: int
    timed_speech_policy_sha256: str
    word_gap_policy_sha256: str
    vad_merge_policy_sha256: str
    utterance_gap_milliseconds: int
    vad_merge_gap_milliseconds: int
    decoder_identity_sha256: str
    producers: tuple[ShadowCalibrationProducerIdentity, ShadowCalibrationProducerIdentity]

    def __post_init__(self) -> None:
        for name in ("provider_id", "provider_version", "funasr_version", "torch_version",
                     "device", "word_timing_capability"):
            _text(getattr(self, name), name)
        for name in ("service_sha256", "timed_speech_policy_sha256", "word_gap_policy_sha256",
                     "vad_merge_policy_sha256", "decoder_identity_sha256"):
            _sha(getattr(self, name), name)
        if self.word_timing_capability != "required":
            raise ShadowLocalServiceProfileError("local service requires actual word timestamps")
        _integer(self.max_request_bytes, "max_request_bytes", minimum=1)
        _integer(self.utterance_gap_milliseconds, "utterance_gap_milliseconds")
        _integer(self.vad_merge_gap_milliseconds, "vad_merge_gap_milliseconds")
        if (type(self.producers) is not tuple or len(self.producers) != 2
                or any(type(item) is not ShadowCalibrationProducerIdentity for item in self.producers)):
            raise ShadowLocalServiceProfileError("producers must be an exact ASR/VAD identity tuple")
        for item in self.producers:
            # Reuse the existing role/inference contract, adding UTF-8/exact
            # leaf checks for directly constructed values at this wire boundary.
            _producer(item.to_mapping())
            if item.service_sha256 != self.service_sha256:
                raise ShadowLocalServiceProfileError("producer must bind the same measured service")
        if tuple(item.producer for item in self.producers) != (CalibrationProducer.ASR, CalibrationProducer.VAD):
            raise ShadowLocalServiceProfileError("producers must be ordered ASR then VAD")
        if self.producers[0].producer_id == self.producers[1].producer_id:
            raise ShadowLocalServiceProfileError("ASR and VAD producer IDs must be distinct")

    def _measured_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SHADOW_LOCAL_SERVICE_PROFILE_SCHEMA,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "service_sha256": self.service_sha256,
            "funasr_version": self.funasr_version,
            "torch_version": self.torch_version,
            "device": self.device,
            "word_timing_capability": self.word_timing_capability,
            "max_request_bytes": self.max_request_bytes,
            "timed_speech_policy_sha256": self.timed_speech_policy_sha256,
            "word_gap_policy_sha256": self.word_gap_policy_sha256,
            "vad_merge_policy_sha256": self.vad_merge_policy_sha256,
            "utterance_gap_milliseconds": self.utterance_gap_milliseconds,
            "vad_merge_gap_milliseconds": self.vad_merge_gap_milliseconds,
            "decoder_identity_sha256": self.decoder_identity_sha256,
            "producers": [item.to_mapping() for item in self.producers],
        }

    @property
    def native_port_identity_sha256(self) -> str:
        return canonical_sha256(self._measured_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {**self._measured_mapping(), "native_port_identity_sha256": self.native_port_identity_sha256}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def build_shadow_local_service_profile(measured: object) -> ShadowLocalServiceProfile:
    """Derive both identities from explicit fields, never accept a claimed hash.

    The exact mapping includes this version's schema, excludes the native
    identity, and contains only pre-calibration fields. It may describe expected
    measurements; no filesystem/model/decoder measurement is performed here.
    """
    raw = _object(measured, _MEASURED_FIELDS)
    if _text(raw["schema_version"], "profile schema") != SHADOW_LOCAL_SERVICE_PROFILE_SCHEMA:
        raise ShadowLocalServiceProfileError("unsupported local service-profile schema")
    producers = raw["producers"]
    if type(producers) is not list:
        raise ShadowLocalServiceProfileError("producer wire must contain exactly two array members")
    producer_values = cast(list[object], producers)
    if len(producer_values) != 2:
        raise ShadowLocalServiceProfileError("producer wire must contain exactly two array members")
    return ShadowLocalServiceProfile(
        _text(raw["provider_id"], "provider ID"),
        _text(raw["provider_version"], "provider version"),
        _sha(raw["service_sha256"], "service hash"),
        _text(raw["funasr_version"], "FunASR version"),
        _text(raw["torch_version"], "Torch version"),
        _text(raw["device"], "device"),
        _text(raw["word_timing_capability"], "word timing capability"),
        _integer(raw["max_request_bytes"], "max_request_bytes", minimum=1),
        _sha(raw["timed_speech_policy_sha256"], "timed speech policy"),
        _sha(raw["word_gap_policy_sha256"], "word gap policy"),
        _sha(raw["vad_merge_policy_sha256"], "VAD merge policy"),
        _integer(raw["utterance_gap_milliseconds"], "utterance gap"),
        _integer(raw["vad_merge_gap_milliseconds"], "VAD merge gap"),
        _sha(raw["decoder_identity_sha256"], "decoder identity"),
        (_producer(producer_values[0]), _producer(producer_values[1])),
    )


def decode_shadow_local_service_profile(value: object) -> ShadowLocalServiceProfile:
    """Decode a complete mapping and reject a contradictory derived identity.

    A byte-loading boundary must already enforce strict JSON and its own byte
    limit. This decoder accepts mappings only; it does not recover erased JSON
    duplicate keys or claim that JSON bytes were measured by the native service.
    """
    raw = _object(value, _MEASURED_FIELDS | {"native_port_identity_sha256"})
    claimed = _sha(raw["native_port_identity_sha256"], "native identity")
    profile = build_shadow_local_service_profile({
        key: item for key, item in raw.items() if key != "native_port_identity_sha256"
    })
    if claimed != profile.native_port_identity_sha256:
        raise ShadowLocalServiceProfileError("local service-profile native identity drift")
    return profile
