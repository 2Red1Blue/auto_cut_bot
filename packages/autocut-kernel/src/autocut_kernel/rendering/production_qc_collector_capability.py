"""Pure closure for a *candidate* production-QC collector capability.

The production-QC runner needs two different identities before it may inspect
an output: a protected, static policy source and a fresh, machine-local
measurement of the collector tools.  This module closes those values together
without reading configuration, starting a process, or writing a Store record.

Its result is intentionally **not authority**.  A later protected Store command
must independently accept an :class:`UnacceptedProductionQcCollectorCapabilityProjection`
before a runtime may treat it as an accepted collector capability.  In
particular, this is unrelated to ASR/VAD timing calibration; it carries neither
speech-model nor timing evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    load_canonical_json_bytes,
)
from ..contracts.compiler.errors import CanonicalizationError

PRODUCTION_QC_COLLECTOR_POLICY_SOURCE_SCHEMA = "production-qc-collector-policy-source-v1"
PRODUCTION_QC_COLLECTOR_LIVE_PROFILE_SCHEMA = "production-qc-collector-live-profile-v1"
PRODUCTION_QC_COLLECTOR_CAPABILITY_REQUEST_SCHEMA = "production-qc-collector-capability-request-v1"
PRODUCTION_QC_COLLECTOR_CAPABILITY_PROJECTION_SCHEMA = (
    "production-qc-collector-capability-projection-v1"
)
PRODUCTION_QC_COLLECTOR_RUNNER_SCHEMA_VERSION = "production-qc-runner-v1"
PRODUCTION_QC_COLLECTOR_REQUIRED_CHECK_SET_VERSION = "production-av-qc-v1"
PRODUCTION_QC_COLLECTOR_CAPABILITY_STATE = "store_acceptance_required"

_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROFILE_ID_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z")
_ZERO_SHA256 = "sha256:" + "0" * 64


class ProductionQcCollectorCapabilityError(ValueError):
    """A closed collector policy/profile/candidate projection is invalid."""


def _fail(detail: str) -> ProductionQcCollectorCapabilityError:
    return ProductionQcCollectorCapabilityError(f"invalid production QC collector capability: {detail}")


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():  # noqa: E721
        raise _fail(f"{field_name} must be canonical non-empty text")
    return value


def _profile_id(value: object, field_name: str = "profile_id") -> str:
    text = _text(value, field_name)
    if _PROFILE_ID_PATTERN.fullmatch(text) is None:
        raise _fail(f"{field_name} is invalid")
    return text


def _sha256(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:  # noqa: E721
        raise _fail(f"{field_name} must be a lowercase sha256 identity")
    if value == _ZERO_SHA256:
        raise _fail(f"{field_name} must be non-zero")
    return value


def _closed_mapping(value: object, fields: frozenset[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[str, object], value)) != fields:  # noqa: E721
        raise _fail(f"{field_name} does not match its closed schema")
    return cast(dict[str, object], value)


def _strict_bytes(raw: bytes, field_name: str) -> dict[str, object]:
    if type(raw) is not bytes:  # noqa: E721
        raise _fail(f"{field_name} must be bytes")
    try:
        value, canonical = load_canonical_json_bytes(raw, origin=field_name)
    except (CanonicalizationError, ValueError) as error:
        raise _fail(f"{field_name} must be strict UTF-8 canonical-subset JSON") from error
    if raw != canonical or type(value) is not dict:  # noqa: E721
        raise _fail(f"{field_name} must be an exact canonical object")
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class ProductionQcCollectorExecutableIdentity:
    """Path-free, exact identity of one executable used by collector measurement."""

    executable_sha256: str
    executable_byte_length: int
    version_output_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.executable_sha256, "executable_identity.executable_sha256")
        if type(self.executable_byte_length) is not int or self.executable_byte_length <= 0:  # noqa: E721
            raise _fail("executable_identity.executable_byte_length must be a positive integer")
        _sha256(self.version_output_sha256, "executable_identity.version_output_sha256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "executable_byte_length": self.executable_byte_length,
            "executable_sha256": self.executable_sha256,
            "version_output_sha256": self.version_output_sha256,
        }


@dataclass(frozen=True, slots=True)
class ProductionQcCollectorPolicySource:
    """Protected static source that defines one allowed collector profile shape.

    ``policy_source_sha256`` is the digest of the separately protected source
    bytes.  It is not this object's self-hash, so the source never becomes
    self-referential.  A configuration/authority reader is responsible for
    supplying this already protected value in a later integration wave.
    """

    profile_id: str
    policy_source_sha256: str
    registry_snapshot_sha256: str
    required_check_set_version: str
    collector_registry_sha256: str
    runner_schema_version: str
    fixed_environment_sha256: str

    def __post_init__(self) -> None:
        _profile_id(self.profile_id)
        for field_name in (
            "policy_source_sha256",
            "registry_snapshot_sha256",
            "collector_registry_sha256",
            "fixed_environment_sha256",
        ):
            _sha256(getattr(self, field_name), f"policy_source.{field_name}")
        if self.required_check_set_version != PRODUCTION_QC_COLLECTOR_REQUIRED_CHECK_SET_VERSION:
            raise _fail("policy_source.required_check_set_version is unregistered")
        if self.runner_schema_version != PRODUCTION_QC_COLLECTOR_RUNNER_SCHEMA_VERSION:
            raise _fail("policy_source.runner_schema_version is unsupported")

    @property
    def canonical_sha256(self) -> str:
        return canonical_json_hash(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "collector_registry_sha256": self.collector_registry_sha256,
            "fixed_environment_sha256": self.fixed_environment_sha256,
            "policy_source_sha256": self.policy_source_sha256,
            "profile_id": self.profile_id,
            "registry_snapshot_sha256": self.registry_snapshot_sha256,
            "required_check_set_version": self.required_check_set_version,
            "runner_schema_version": self.runner_schema_version,
            "schema_version": PRODUCTION_QC_COLLECTOR_POLICY_SOURCE_SCHEMA,
        }


@dataclass(frozen=True, slots=True)
class ProductionQcCollectorLiveProfile:
    """Freshly measured collector environment, intentionally without authority."""

    profile_id: str
    policy_source_sha256: str
    registry_snapshot_sha256: str
    required_check_set_version: str
    collector_registry_sha256: str
    runner_schema_version: str
    fixed_environment_sha256: str
    ffmpeg_identity: ProductionQcCollectorExecutableIdentity
    ffprobe_identity: ProductionQcCollectorExecutableIdentity

    def __post_init__(self) -> None:
        _profile_id(self.profile_id)
        for field_name in (
            "policy_source_sha256",
            "registry_snapshot_sha256",
            "collector_registry_sha256",
            "fixed_environment_sha256",
        ):
            _sha256(getattr(self, field_name), f"live_profile.{field_name}")
        if self.required_check_set_version != PRODUCTION_QC_COLLECTOR_REQUIRED_CHECK_SET_VERSION:
            raise _fail("live_profile.required_check_set_version is unregistered")
        if self.runner_schema_version != PRODUCTION_QC_COLLECTOR_RUNNER_SCHEMA_VERSION:
            raise _fail("live_profile.runner_schema_version is unsupported")
        if type(self.ffmpeg_identity) is not ProductionQcCollectorExecutableIdentity:  # noqa: E721
            raise _fail("live_profile.ffmpeg_identity must be exact")
        if type(self.ffprobe_identity) is not ProductionQcCollectorExecutableIdentity:  # noqa: E721
            raise _fail("live_profile.ffprobe_identity must be exact")

    @property
    def canonical_sha256(self) -> str:
        return canonical_json_hash(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "collector_registry_sha256": self.collector_registry_sha256,
            "ffmpeg_identity": self.ffmpeg_identity.to_mapping(),
            "ffprobe_identity": self.ffprobe_identity.to_mapping(),
            "fixed_environment_sha256": self.fixed_environment_sha256,
            "policy_source_sha256": self.policy_source_sha256,
            "profile_id": self.profile_id,
            "registry_snapshot_sha256": self.registry_snapshot_sha256,
            "required_check_set_version": self.required_check_set_version,
            "runner_schema_version": self.runner_schema_version,
            "schema_version": PRODUCTION_QC_COLLECTOR_LIVE_PROFILE_SCHEMA,
        }


def _verify_profile_matches_policy(
    policy_source: ProductionQcCollectorPolicySource,
    live_profile: ProductionQcCollectorLiveProfile,
) -> None:
    for field_name in (
        "profile_id",
        "policy_source_sha256",
        "registry_snapshot_sha256",
        "required_check_set_version",
        "collector_registry_sha256",
        "runner_schema_version",
        "fixed_environment_sha256",
    ):
        if getattr(policy_source, field_name) != getattr(live_profile, field_name):
            raise _fail(f"live_profile.{field_name} does not match protected policy source")


@dataclass(frozen=True, slots=True)
class ProductionQcCollectorCapabilityRequest:
    """Closed request for later Store acceptance; it grants no capability itself."""

    policy_source: ProductionQcCollectorPolicySource
    live_profile: ProductionQcCollectorLiveProfile

    def __post_init__(self) -> None:
        if type(self.policy_source) is not ProductionQcCollectorPolicySource:  # noqa: E721
            raise _fail("capability request policy_source must be exact")
        if type(self.live_profile) is not ProductionQcCollectorLiveProfile:  # noqa: E721
            raise _fail("capability request live_profile must be exact")
        _verify_profile_matches_policy(self.policy_source, self.live_profile)

    @property
    def canonical_sha256(self) -> str:
        return canonical_json_hash(self.to_mapping())

    @property
    def authority_state(self) -> str:
        return PRODUCTION_QC_COLLECTOR_CAPABILITY_STATE

    def to_mapping(self) -> dict[str, object]:
        return {
            "authority_state": PRODUCTION_QC_COLLECTOR_CAPABILITY_STATE,
            "live_profile": self.live_profile.to_mapping(),
            "policy_source": self.policy_source.to_mapping(),
            "schema_version": PRODUCTION_QC_COLLECTOR_CAPABILITY_REQUEST_SCHEMA,
        }


@dataclass(frozen=True, slots=True)
class UnacceptedProductionQcCollectorCapabilityProjection:
    """A non-authoritative projection of one closed collector capability request.

    The name and fixed state are deliberate: code must not mistake successful
    pure closure for the protected Store acceptance that will be introduced in a
    later wave.
    """

    request: ProductionQcCollectorCapabilityRequest
    request_sha256: str

    def __post_init__(self) -> None:
        if type(self.request) is not ProductionQcCollectorCapabilityRequest:  # noqa: E721
            raise _fail("capability projection request must be exact")
        _sha256(self.request_sha256, "capability_projection.request_sha256")
        if self.request_sha256 != self.request.canonical_sha256:
            raise _fail("capability_projection.request_sha256 does not match request")

    @property
    def canonical_sha256(self) -> str:
        return canonical_json_hash(self.to_mapping())

    @property
    def authority_state(self) -> str:
        return PRODUCTION_QC_COLLECTOR_CAPABILITY_STATE

    @property
    def is_authoritative(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, object]:
        return {
            "authority_state": PRODUCTION_QC_COLLECTOR_CAPABILITY_STATE,
            "request": self.request.to_mapping(),
            "request_sha256": self.request_sha256,
            "schema_version": PRODUCTION_QC_COLLECTOR_CAPABILITY_PROJECTION_SCHEMA,
        }


def project_unaccepted_production_qc_collector_capability(
    request: ProductionQcCollectorCapabilityRequest,
) -> UnacceptedProductionQcCollectorCapabilityProjection:
    """Project only a closed candidate; this function cannot accept authority."""

    if type(request) is not ProductionQcCollectorCapabilityRequest:  # noqa: E721
        raise _fail("capability request must be exact")
    return UnacceptedProductionQcCollectorCapabilityProjection(request, request.canonical_sha256)


def decode_production_qc_collector_policy_source(raw: bytes) -> ProductionQcCollectorPolicySource:
    mapping = _closed_mapping(
        _strict_bytes(raw, "policy source"),
        frozenset(
            {
                "collector_registry_sha256",
                "fixed_environment_sha256",
                "policy_source_sha256",
                "profile_id",
                "registry_snapshot_sha256",
                "required_check_set_version",
                "runner_schema_version",
                "schema_version",
            }
        ),
        "policy source",
    )
    if mapping["schema_version"] != PRODUCTION_QC_COLLECTOR_POLICY_SOURCE_SCHEMA:
        raise _fail("policy source schema version is unsupported")
    return ProductionQcCollectorPolicySource(
        profile_id=_profile_id(mapping["profile_id"]),
        policy_source_sha256=_sha256(mapping["policy_source_sha256"], "policy_source_sha256"),
        registry_snapshot_sha256=_sha256(
            mapping["registry_snapshot_sha256"], "registry_snapshot_sha256"
        ),
        required_check_set_version=_text(
            mapping["required_check_set_version"], "required_check_set_version"
        ),
        collector_registry_sha256=_sha256(
            mapping["collector_registry_sha256"], "collector_registry_sha256"
        ),
        runner_schema_version=_text(mapping["runner_schema_version"], "runner_schema_version"),
        fixed_environment_sha256=_sha256(
            mapping["fixed_environment_sha256"], "fixed_environment_sha256"
        ),
    )


def _decode_executable_identity(value: object, field_name: str) -> ProductionQcCollectorExecutableIdentity:
    mapping = _closed_mapping(
        value,
        frozenset({"executable_byte_length", "executable_sha256", "version_output_sha256"}),
        field_name,
    )
    return ProductionQcCollectorExecutableIdentity(
        executable_sha256=_sha256(mapping["executable_sha256"], f"{field_name}.executable_sha256"),
        executable_byte_length=_positive_integer(
            mapping["executable_byte_length"], f"{field_name}.executable_byte_length"
        ),
        version_output_sha256=_sha256(
            mapping["version_output_sha256"], f"{field_name}.version_output_sha256"
        ),
    )


def _positive_integer(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0:  # noqa: E721
        raise _fail(f"{field_name} must be a positive integer")
    return value


def decode_production_qc_collector_live_profile(raw: bytes) -> ProductionQcCollectorLiveProfile:
    mapping = _closed_mapping(
        _strict_bytes(raw, "live profile"),
        frozenset(
            {
                "collector_registry_sha256",
                "ffmpeg_identity",
                "ffprobe_identity",
                "fixed_environment_sha256",
                "policy_source_sha256",
                "profile_id",
                "registry_snapshot_sha256",
                "required_check_set_version",
                "runner_schema_version",
                "schema_version",
            }
        ),
        "live profile",
    )
    if mapping["schema_version"] != PRODUCTION_QC_COLLECTOR_LIVE_PROFILE_SCHEMA:
        raise _fail("live profile schema version is unsupported")
    return ProductionQcCollectorLiveProfile(
        profile_id=_profile_id(mapping["profile_id"]),
        policy_source_sha256=_sha256(mapping["policy_source_sha256"], "policy_source_sha256"),
        registry_snapshot_sha256=_sha256(
            mapping["registry_snapshot_sha256"], "registry_snapshot_sha256"
        ),
        required_check_set_version=_text(
            mapping["required_check_set_version"], "required_check_set_version"
        ),
        collector_registry_sha256=_sha256(
            mapping["collector_registry_sha256"], "collector_registry_sha256"
        ),
        runner_schema_version=_text(mapping["runner_schema_version"], "runner_schema_version"),
        fixed_environment_sha256=_sha256(
            mapping["fixed_environment_sha256"], "fixed_environment_sha256"
        ),
        ffmpeg_identity=_decode_executable_identity(mapping["ffmpeg_identity"], "ffmpeg_identity"),
        ffprobe_identity=_decode_executable_identity(
            mapping["ffprobe_identity"], "ffprobe_identity"
        ),
    )


def decode_production_qc_collector_capability_request(
    raw: bytes,
) -> ProductionQcCollectorCapabilityRequest:
    mapping = _closed_mapping(
        _strict_bytes(raw, "capability request"),
        frozenset({"authority_state", "live_profile", "policy_source", "schema_version"}),
        "capability request",
    )
    if mapping["schema_version"] != PRODUCTION_QC_COLLECTOR_CAPABILITY_REQUEST_SCHEMA:
        raise _fail("capability request schema version is unsupported")
    if mapping["authority_state"] != PRODUCTION_QC_COLLECTOR_CAPABILITY_STATE:
        raise _fail("capability request authority_state is invalid")
    return ProductionQcCollectorCapabilityRequest(
        decode_production_qc_collector_policy_source(canonical_json_bytes(mapping["policy_source"])),
        decode_production_qc_collector_live_profile(canonical_json_bytes(mapping["live_profile"])),
    )


def decode_unaccepted_production_qc_collector_capability_projection(
    raw: bytes,
) -> UnacceptedProductionQcCollectorCapabilityProjection:
    mapping = _closed_mapping(
        _strict_bytes(raw, "capability projection"),
        frozenset({"authority_state", "request", "request_sha256", "schema_version"}),
        "capability projection",
    )
    if mapping["schema_version"] != PRODUCTION_QC_COLLECTOR_CAPABILITY_PROJECTION_SCHEMA:
        raise _fail("capability projection schema version is unsupported")
    if mapping["authority_state"] != PRODUCTION_QC_COLLECTOR_CAPABILITY_STATE:
        raise _fail("capability projection authority_state is invalid")
    request = decode_production_qc_collector_capability_request(canonical_json_bytes(mapping["request"]))
    return UnacceptedProductionQcCollectorCapabilityProjection(
        request,
        _sha256(mapping["request_sha256"], "capability_projection.request_sha256"),
    )
