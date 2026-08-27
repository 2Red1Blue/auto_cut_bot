"""Immutable, self-measured timing identity for one normal runtime capability.

The build hash remains audit data.  Runtime admission uses the independently
derived timing-compatibility hash, which deliberately excludes unrelated build
changes.  This module has no Store or service dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_hash, load_canonical_json_bytes
from ..contracts.compiler.errors import CanonicalizationError
from .timing_compatibility import (
    TimingCompatibilityError,
    TimingCompatibilityProfile,
    decode_timing_compatibility_profile,
)

RUNTIME_MEASUREMENT_IDENTITY_SCHEMA = "runtime-measurement-identity-v1"
PC_CUDA_RUNTIME_CAPABILITY_ID = "pc_cuda"
MAC_CPU_RUNTIME_CAPABILITY_ID = "mac_cpu"
RUNTIME_CAPABILITY_IDS = frozenset(
    {PC_CUDA_RUNTIME_CAPABILITY_ID, MAC_CPU_RUNTIME_CAPABILITY_ID}
)


class RuntimeMeasurementIdentityError(ValueError):
    """The runtime did not provide one complete, allowed measured identity."""


def _fail(detail: str) -> RuntimeMeasurementIdentityError:
    return RuntimeMeasurementIdentityError(f"invalid runtime measurement identity: {detail}")


@dataclass(frozen=True, slots=True)
class RuntimeMeasurementIdentity:
    """The complete measured identity for either PC CUDA or Mac CPU timing."""

    runtime_capability_id: str
    timing_compatibility: TimingCompatibilityProfile

    def __post_init__(self) -> None:
        if self.runtime_capability_id not in RUNTIME_CAPABILITY_IDS:
            raise _fail("runtime_capability_id is not an allowed immutable capability")
        if type(self.timing_compatibility) is not TimingCompatibilityProfile:  # noqa: E721
            raise _fail("timing_compatibility must be an exact TimingCompatibilityProfile")
        required_device = (
            "cuda"
            if self.runtime_capability_id == PC_CUDA_RUNTIME_CAPABILITY_ID
            else "cpu"
        )
        if self.timing_compatibility.device.device_class != required_device:
            raise _fail("runtime_capability_id does not match timing compatibility device class")

    @property
    def timing_compatibility_sha256(self) -> str:
        return self.timing_compatibility.timing_compatibility_sha256

    @property
    def build_audit_sha256(self) -> str:
        return self.timing_compatibility.build_audit_sha256

    @property
    def canonical_sha256(self) -> str:
        # Build audit bytes are retained in ``to_mapping`` for provenance, but
        # are deliberately outside runtime admission identity.  A harmless
        # rebuild must not revoke a compatible accepted capability.
        return canonical_json_hash(
            {
                "schema_version": RUNTIME_MEASUREMENT_IDENTITY_SCHEMA,
                "runtime_capability_id": self.runtime_capability_id,
                "timing_compatibility_sha256": self.timing_compatibility_sha256,
            }
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": RUNTIME_MEASUREMENT_IDENTITY_SCHEMA,
            "runtime_capability_id": self.runtime_capability_id,
            "timing_compatibility": self.timing_compatibility.to_mapping(),
        }


def decode_runtime_measurement_identity(raw: bytes) -> RuntimeMeasurementIdentity:
    """Decode canonical bytes and re-derive the nested compatibility identity."""
    if type(raw) is not bytes:  # noqa: E721
        raise _fail("source must be bytes")
    try:
        value, canonical = load_canonical_json_bytes(raw, origin="runtime measurement identity")
    except (CanonicalizationError, ValueError) as error:
        raise _fail("source is not strict canonical-subset JSON") from error
    if raw != canonical or type(value) is not dict:  # noqa: E721
        raise _fail("source must be an exact canonical object")
    mapping = cast(dict[str, object], value)
    if frozenset(mapping) != {
        "schema_version", "runtime_capability_id", "timing_compatibility"
    }:
        raise _fail("source does not match its closed schema")
    if mapping["schema_version"] != RUNTIME_MEASUREMENT_IDENTITY_SCHEMA:
        raise _fail("source schema version is unsupported")
    if type(mapping["runtime_capability_id"]) is not str:  # noqa: E721
        raise _fail("runtime_capability_id must be text")
    try:
        profile = decode_timing_compatibility_profile(mapping["timing_compatibility"])
    except TimingCompatibilityError as error:
        raise _fail("timing_compatibility is invalid") from error
    return RuntimeMeasurementIdentity(cast(str, mapping["runtime_capability_id"]), profile)
