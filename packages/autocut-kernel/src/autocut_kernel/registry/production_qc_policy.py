"""Pure decoder for the closed production-QC static policy source set."""

from __future__ import annotations

from typing import cast

from ..contracts.compiler.canonical import (
    canonical_json_hash,
    load_canonical_json_bytes,
    sha256_bytes,
)
from ..contracts.compiler.errors import CanonicalizationError
from ..rendering.production_qc_collector_capability import ProductionQcCollectorPolicySource

_MAX_SOURCE_BYTES = 16 * 1024
_POLICY_SCHEMA_SHA256 = "sha256:9b6c58f6592a0d76ab6c577a8a9a5611db35124b2371c2e44f15500a4aae0844"
_SNAPSHOT_SCHEMA_SHA256 = "sha256:366e0682872e8736e3b3056770396d452e071cc3fb1ccc1bffddec155a3390e7"
_PROFILE_SCHEMA_SHA256 = "sha256:62e6e69aa02d96741dd4c6f3913febacd0a012396949845680b57db9ad92777c"

_POLICY_SCHEMA = "production-qc-collector-policy-source-v1"
_SNAPSHOT_SCHEMA = "production-qc-collector-registry-snapshot-v1"
_PROFILE_SCHEMA = "production-qc-collector-capability-set-profile-v1"
_SNAPSHOT_IDENTITY_SCHEMA = "production-qc-collector-registry-snapshot-identity-v1"
_PROFILE_ID = "production-av-qc-v1"
_CHECK_SET = "production-av-qc-v1"
_RUNNER_SCHEMA = "production-qc-runner-v1"
_REGISTRY_SHA256 = "sha256:285cbd72a611ea4fe3bded0a2dd0774a90b89fc3f52eeef146ecc03150c18e7e"
_ENVIRONMENT_SHA256 = "sha256:1d36b32cd19a15c1e5d98f1ac689b632b8ebde9a41a24cbb0185844a13728e78"

_STATIC_FIELDS = frozenset(
    {
        "profile_id",
        "required_check_set_version",
        "collector_registry_sha256",
        "runner_schema_version",
        "fixed_environment_sha256",
    }
)
_STATIC_VALUES = {
    "profile_id": _PROFILE_ID,
    "required_check_set_version": _CHECK_SET,
    "collector_registry_sha256": _REGISTRY_SHA256,
    "runner_schema_version": _RUNNER_SCHEMA,
    "fixed_environment_sha256": _ENVIRONMENT_SHA256,
}
_POLICY_FIELDS = _STATIC_FIELDS | {"schema_version", "registry_snapshot_sha256"}
_SNAPSHOT_FIELDS = _STATIC_FIELDS | {"schema_version"}
_PROFILE_FIELDS = frozenset({"schema_version", "artifact_set_profile", "members"})
_MEMBER_FIELDS = frozenset({"ordinal", "artifact_type", "logical_id", "revision"})
_EXPECTED_MEMBERS = (
    (0, "production_qc_collector_measurement", "measurement", 1),
    (1, "production_qc_collector_capability", "decision", 1),
)


class ProductionQcPolicySourceError(ValueError):
    """The governed static policy source closure is invalid."""


def _invalid(detail: str) -> ProductionQcPolicySourceError:
    return ProductionQcPolicySourceError(f"invalid production QC policy source: {detail}")


def _object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_SOURCE_BYTES:  # noqa: E721
        raise _invalid(f"{label} bytes are invalid or oversized")
    try:
        value, canonical = load_canonical_json_bytes(raw, origin=label)
    except (CanonicalizationError, RecursionError, ValueError) as error:
        raise _invalid(f"{label} is not strict canonical JSON") from error
    if raw != canonical or type(value) is not dict:  # noqa: E721
        raise _invalid(f"{label} must be an exact canonical object")
    return cast(dict[str, object], value)


def _closed(value: object, fields: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[str, object], value)) != fields:  # noqa: E721
        raise _invalid(f"{label} has unsupported, missing, or duplicate fields")
    return cast(dict[str, object], value)


def _pinned_schema(raw: bytes, expected_sha256: str, label: str) -> None:
    _object(raw, label)
    if sha256_bytes(raw) != expected_sha256:
        raise _invalid(f"{label} does not match the supported schema identity")


def _static_source(
    raw: bytes, *, fields: frozenset[str], schema: str, label: str
) -> dict[str, object]:
    source = _closed(_object(raw, label), fields, label)
    if source["schema_version"] != schema:
        raise _invalid(f"{label} schema version is unsupported")
    for key, expected in _STATIC_VALUES.items():
        if type(source[key]) is not str or source[key] != expected:  # noqa: E721
            raise _invalid(f"{label}.{key} is not the registered value")
    return source


def _profile(raw: bytes) -> None:
    profile = _closed(_object(raw, "capability set profile"), _PROFILE_FIELDS, "capability set profile")
    members_value = profile["members"]
    if (
        profile["schema_version"] != _PROFILE_SCHEMA
        or profile["artifact_set_profile"] != "production_qc_collector_capability_v1"
        or type(members_value) is not list  # noqa: E721
    ):
        raise _invalid("capability set profile is unsupported")
    members = cast(list[object], members_value)
    if len(members) != len(_EXPECTED_MEMBERS):
        raise _invalid("capability set profile is unsupported")
    for member, expected in zip(members, _EXPECTED_MEMBERS):
        mapping = _closed(member, _MEMBER_FIELDS, "capability set profile member")
        ordinal, artifact_type, logical_id, revision = expected
        if (
            type(mapping["ordinal"]) is not int  # noqa: E721
            or type(mapping["revision"]) is not int  # noqa: E721
            or mapping["ordinal"] != ordinal
            or mapping["artifact_type"] != artifact_type
            or mapping["logical_id"] != logical_id
            or mapping["revision"] != revision
        ):
            raise _invalid("capability set profile members are not the exact registered order")


def decode_production_qc_static_policy_source(
    *,
    policy_raw: bytes,
    registry_snapshot_raw: bytes,
    registry_snapshot_schema_raw: bytes,
    policy_schema_raw: bytes,
    capability_set_profile_raw: bytes,
    capability_set_profile_schema_raw: bytes,
) -> ProductionQcCollectorPolicySource:
    """Decode six immutable governed bytes into static policy content only."""

    _pinned_schema(policy_schema_raw, _POLICY_SCHEMA_SHA256, "policy schema")
    _pinned_schema(registry_snapshot_schema_raw, _SNAPSHOT_SCHEMA_SHA256, "snapshot schema")
    _pinned_schema(capability_set_profile_schema_raw, _PROFILE_SCHEMA_SHA256, "profile schema")
    policy = _static_source(
        policy_raw, fields=_POLICY_FIELDS, schema=_POLICY_SCHEMA, label="policy"
    )
    _static_source(
        registry_snapshot_raw, fields=_SNAPSHOT_FIELDS, schema=_SNAPSHOT_SCHEMA, label="snapshot"
    )
    _profile(capability_set_profile_raw)
    snapshot_identity = canonical_json_hash(
        {
            "schema_version": _SNAPSHOT_IDENTITY_SCHEMA,
            "sources": [
                {"role": "registry_snapshot", "sha256": sha256_bytes(registry_snapshot_raw)},
                {
                    "role": "registry_snapshot_schema",
                    "sha256": sha256_bytes(registry_snapshot_schema_raw),
                },
            ],
        }
    )
    if policy["registry_snapshot_sha256"] != snapshot_identity:
        raise _invalid("policy registry snapshot identity does not match immutable source bytes")
    return ProductionQcCollectorPolicySource(
        profile_id=_PROFILE_ID,
        policy_source_sha256=sha256_bytes(policy_raw),
        registry_snapshot_sha256=snapshot_identity,
        required_check_set_version=_CHECK_SET,
        collector_registry_sha256=_REGISTRY_SHA256,
        runner_schema_version=_RUNNER_SCHEMA,
        fixed_environment_sha256=_ENVIRONMENT_SHA256,
    )
