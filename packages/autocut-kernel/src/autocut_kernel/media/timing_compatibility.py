"""Closed timing-engine compatibility identities for calibration profiles.

This module is intentionally pure.  It records the exact build that produced
an observation for audit, while deriving compatibility from only the timing
engine inputs.  A caller may never claim a compatibility digest: builders
derive it and decoders recompute it before accepting a stored mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from .types import canonical_sha256, sha256_prefixed

TIMING_COMPATIBILITY_PROFILE_SCHEMA = "timing-compatibility-profile-v1"

_ZERO_SHA256 = "sha256:" + "0" * 64
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CUDA_VERSION = re.compile(r"^[0-9]{1,2}\.[0-9]{1,2}$")

_PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "timing_engine_compatibility_version",
        "build_audit_sha256",
        "runtime",
        "decode",
        "policies",
        "producers",
    }
)
_RUNTIME_FIELDS = frozenset({"funasr_version", "torch_version", "device"})
_CPU_DEVICE_FIELDS = frozenset({"device_class"})
_CUDA_DEVICE_FIELDS = frozenset({"device_class", "cuda_runtime_version", "gpu_compute_capability"})
_DECODE_FIELDS = frozenset(
    {"decoder_identity_sha256", "resampling_identity_sha256", "native_protocol_identity_sha256"}
)
_POLICY_FIELDS = frozenset({"word_timestamp_policy_sha256", "vad_merge_policy_sha256"})
_PRODUCER_FIELDS = frozenset(
    {
        "producer_kind",
        "producer_id",
        "producer_version",
        "model_id",
        "model_revision",
        "model_sha256",
        "inference_identity_sha256",
    }
)


class TimingCompatibilityError(ValueError):
    """A timing-compatibility mapping is incomplete, unsafe, or contradictory."""


def _object(value: object, fields: frozenset[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721 - this is a strict wire contract.
        raise TimingCompatibilityError(f"{field_name} must be an exact object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or frozenset(raw) != fields:
        raise TimingCompatibilityError(
            f"{field_name} has missing, unknown, or duplicate logical fields"
        )
    return cast(dict[str, object], raw)


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():  # noqa: E721
        raise TimingCompatibilityError(f"{field_name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise TimingCompatibilityError(f"{field_name} must be valid UTF-8") from error
    return value


def _version(value: object, field_name: str) -> str:
    version = _text(value, field_name)
    if _VERSION.fullmatch(version) is None:
        raise TimingCompatibilityError(f"{field_name} is not a protected compatibility version")
    return version


def _sha(value: object, field_name: str) -> str:
    if type(value) is not str:  # noqa: E721
        raise TimingCompatibilityError(f"{field_name} must be a lowercase SHA-256 identity")
    try:
        digest = sha256_prefixed(value, field_name)
    except ValueError as error:
        raise TimingCompatibilityError(
            f"{field_name} must be a lowercase SHA-256 identity"
        ) from error
    if digest == _ZERO_SHA256:
        raise TimingCompatibilityError(f"{field_name} must not be a zero identity")
    return digest


@dataclass(frozen=True, slots=True)
class TimingDeviceIdentity:
    """A deliberately small hardware discriminator: CPU or measured CUDA capability."""

    device_class: str
    cuda_runtime_version: str | None = None
    gpu_compute_capability: str | None = None

    def __post_init__(self) -> None:
        if self.device_class == "cpu":
            if self.cuda_runtime_version is not None or self.gpu_compute_capability is not None:
                raise TimingCompatibilityError("CPU device identity must not contain CUDA fields")
            return
        if self.device_class != "cuda":
            raise TimingCompatibilityError("device class must be exactly cpu or cuda")
        for field_name in ("cuda_runtime_version", "gpu_compute_capability"):
            value = getattr(self, field_name)
            if type(value) is not str or _CUDA_VERSION.fullmatch(value) is None:  # noqa: E721
                raise TimingCompatibilityError(
                    f"CUDA {field_name} must be an exact major.minor identity"
                )

    def to_mapping(self) -> dict[str, str]:
        if self.device_class == "cpu":
            return {"device_class": "cpu"}
        return {
            "device_class": "cuda",
            "cuda_runtime_version": cast(str, self.cuda_runtime_version),
            "gpu_compute_capability": cast(str, self.gpu_compute_capability),
        }


def _device(value: object) -> TimingDeviceIdentity:
    if type(value) is not dict:  # noqa: E721
        raise TimingCompatibilityError("runtime.device must be an exact object")
    raw = cast(dict[object, object], value)
    device_class = raw.get("device_class")
    if device_class == "cpu":
        cpu = _object(raw, _CPU_DEVICE_FIELDS, "CPU device")
        return TimingDeviceIdentity(_text(cpu["device_class"], "device class"))
    if device_class == "cuda":
        cuda = _object(raw, _CUDA_DEVICE_FIELDS, "CUDA device")
        return TimingDeviceIdentity(
            _text(cuda["device_class"], "device class"),
            _text(cuda["cuda_runtime_version"], "CUDA runtime version"),
            _text(cuda["gpu_compute_capability"], "GPU compute capability"),
        )
    raise TimingCompatibilityError("device class must be exactly cpu or cuda")


@dataclass(frozen=True, slots=True)
class TimingCompatibilityProducerIdentity:
    """The model and producer values that can change emitted ASR/VAD timings."""

    producer_kind: str
    producer_id: str
    producer_version: str
    model_id: str
    model_revision: str
    model_sha256: str
    inference_identity_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "producer_kind",
            "producer_id",
            "producer_version",
            "model_id",
            "model_revision",
        ):
            _text(getattr(self, field_name), f"producer.{field_name}")
        for field_name in ("model_sha256", "inference_identity_sha256"):
            _sha(getattr(self, field_name), f"producer.{field_name}")

    def to_mapping(self) -> dict[str, str]:
        return {
            "producer_kind": self.producer_kind,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": self.model_sha256,
            "inference_identity_sha256": self.inference_identity_sha256,
        }


def _producer(value: object) -> TimingCompatibilityProducerIdentity:
    raw = _object(value, _PRODUCER_FIELDS, "producer")
    return TimingCompatibilityProducerIdentity(
        _text(raw["producer_kind"], "producer kind"),
        _text(raw["producer_id"], "producer ID"),
        _text(raw["producer_version"], "producer version"),
        _text(raw["model_id"], "producer model ID"),
        _text(raw["model_revision"], "producer model revision"),
        _sha(raw["model_sha256"], "producer model hash"),
        _sha(raw["inference_identity_sha256"], "producer inference identity"),
    )


@dataclass(frozen=True, slots=True)
class TimingCompatibilityProfile:
    """A closed profile whose audit build hash is outside compatibility equality."""

    timing_engine_compatibility_version: str
    build_audit_sha256: str
    funasr_version: str
    torch_version: str
    device: TimingDeviceIdentity
    decoder_identity_sha256: str
    resampling_identity_sha256: str
    native_protocol_identity_sha256: str
    word_timestamp_policy_sha256: str
    vad_merge_policy_sha256: str
    producers: tuple[TimingCompatibilityProducerIdentity, TimingCompatibilityProducerIdentity]

    def __post_init__(self) -> None:
        _version(self.timing_engine_compatibility_version, "timing engine compatibility version")
        _sha(self.build_audit_sha256, "build audit hash")
        _text(self.funasr_version, "FunASR version")
        _text(self.torch_version, "Torch version")
        if type(self.device) is not TimingDeviceIdentity:  # noqa: E721
            raise TimingCompatibilityError("device must be an exact TimingDeviceIdentity")
        for field_name in (
            "decoder_identity_sha256",
            "resampling_identity_sha256",
            "native_protocol_identity_sha256",
            "word_timestamp_policy_sha256",
            "vad_merge_policy_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        if (
            type(self.producers) is not tuple
            or len(self.producers) != 2
            or any(type(item) is not TimingCompatibilityProducerIdentity for item in self.producers)
        ):
            raise TimingCompatibilityError("producers must be an exact ASR/VAD identity tuple")
        asr, vad = self.producers
        if (asr.producer_kind, vad.producer_kind) != ("asr", "vad"):
            raise TimingCompatibilityError("producers must be ordered ASR then VAD")
        if asr.producer_id == vad.producer_id:
            raise TimingCompatibilityError("ASR and VAD producer IDs must be distinct")
        if asr.model_id == vad.model_id:
            raise TimingCompatibilityError("ASR and VAD model IDs must be distinct")

    def _compatibility_mapping(self) -> dict[str, object]:
        return {
            "schema_version": TIMING_COMPATIBILITY_PROFILE_SCHEMA,
            "timing_engine_compatibility_version": self.timing_engine_compatibility_version,
            "runtime": {
                "funasr_version": self.funasr_version,
                "torch_version": self.torch_version,
                "device": self.device.to_mapping(),
            },
            "decode": {
                "decoder_identity_sha256": self.decoder_identity_sha256,
                "resampling_identity_sha256": self.resampling_identity_sha256,
                "native_protocol_identity_sha256": self.native_protocol_identity_sha256,
            },
            "policies": {
                "word_timestamp_policy_sha256": self.word_timestamp_policy_sha256,
                "vad_merge_policy_sha256": self.vad_merge_policy_sha256,
            },
            "producers": [item.to_mapping() for item in self.producers],
        }

    @property
    def timing_compatibility_sha256(self) -> str:
        """Canonical timing identity; intentionally independent of audit-only build bytes."""
        return canonical_sha256(self._compatibility_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._compatibility_mapping(),
            "build_audit_sha256": self.build_audit_sha256,
            "timing_compatibility_sha256": self.timing_compatibility_sha256,
        }


def build_timing_compatibility_profile(measured: object) -> TimingCompatibilityProfile:
    """Build a profile from measured fields, refusing caller-claimed identities."""
    raw = _object(measured, _PROFILE_FIELDS, "timing compatibility profile")
    if _text(raw["schema_version"], "profile schema") != TIMING_COMPATIBILITY_PROFILE_SCHEMA:
        raise TimingCompatibilityError("unsupported timing compatibility profile schema")
    runtime = _object(raw["runtime"], _RUNTIME_FIELDS, "runtime")
    decode = _object(raw["decode"], _DECODE_FIELDS, "decode")
    policies = _object(raw["policies"], _POLICY_FIELDS, "policies")
    if type(raw["producers"]) is not list:  # noqa: E721
        raise TimingCompatibilityError("producers must be an exact two-member array")
    producer_values = cast(list[object], raw["producers"])
    if len(producer_values) != 2:
        raise TimingCompatibilityError("producers must be an exact two-member array")
    return TimingCompatibilityProfile(
        _version(raw["timing_engine_compatibility_version"], "timing engine compatibility version"),
        _sha(raw["build_audit_sha256"], "build audit hash"),
        _text(runtime["funasr_version"], "FunASR version"),
        _text(runtime["torch_version"], "Torch version"),
        _device(runtime["device"]),
        _sha(decode["decoder_identity_sha256"], "decoder identity"),
        _sha(decode["resampling_identity_sha256"], "resampling identity"),
        _sha(decode["native_protocol_identity_sha256"], "native protocol identity"),
        _sha(policies["word_timestamp_policy_sha256"], "word timestamp policy"),
        _sha(policies["vad_merge_policy_sha256"], "VAD merge policy"),
        (_producer(producer_values[0]), _producer(producer_values[1])),
    )


def decode_timing_compatibility_profile(value: object) -> TimingCompatibilityProfile:
    """Decode a complete mapping and independently verify its derived identity."""
    raw = _object(
        value,
        _PROFILE_FIELDS | {"timing_compatibility_sha256"},
        "timing compatibility profile",
    )
    claimed = _sha(raw["timing_compatibility_sha256"], "timing compatibility hash")
    profile = build_timing_compatibility_profile(
        {key: item for key, item in raw.items() if key != "timing_compatibility_sha256"}
    )
    if claimed != profile.timing_compatibility_sha256:
        raise TimingCompatibilityError("timing compatibility hash does not match closed profile")
    return profile
