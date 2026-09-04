"""Fixed installed transport for the governed production-QC static policy.

These bytes identify a policy, not an accepted host or a release permission.
The sibling digest detects corruption; trust in the installed code and package
still belongs to deployment/consumer verification. Runtime performs no Git,
checkout, environment, executable probe or Store access here.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from importlib import resources
from typing import cast

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    sha256_bytes,
)
from ..contracts.compiler.errors import CanonicalizationError
from ..rendering.production_qc_collector_capability import ProductionQcCollectorPolicySource
from .production_qc_policy import (
    ProductionQcPolicySourceError,
    decode_production_qc_static_policy_source,
)

_MAX_RESOURCE_BYTES = 128 * 1024
_MAX_SOURCE_BYTES = 16 * 1024
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SOURCE_ROLES = frozenset({
    "policy", "policy_schema", "registry_snapshot", "registry_snapshot_schema",
    "capability_set_profile", "capability_set_profile_schema",
})


class InstalledProductionQcResourceError(ValueError):
    """Installed QC policy transport is absent, changed or unsupported."""


@dataclass(frozen=True, slots=True)
class ProductionQcSourceProvenance:
    authority_revision: int
    authority_bundle_sha256: str
    source_commit: str
    inventory_commit: str
    lock_commit: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "authority_revision": self.authority_revision,
            "authority_bundle_sha256": self.authority_bundle_sha256,
            "source_commit": self.source_commit,
            "inventory_commit": self.inventory_commit,
            "lock_commit": self.lock_commit,
        }


# The controlled package version supports this reviewed source lineage. Changes
# to an unrelated function do not change these values or require host re-probing.
_SUPPORTED_PROVENANCE = ProductionQcSourceProvenance(
    9,
    "sha256:df6db536a305409eb0b2d87bbbd78c398e28369543f7440e1dc1373879bbce6f",
    "d0a637874771ee1401935defd0001ceaed538b49",
    "1f3941faf046699fe8c65c8a8050f96a7cd59080",
    "749b5fb4fbaf336e180c3f47f2abc21e69cb03b2",
)


@dataclass(frozen=True, slots=True)
class InstalledProductionQcResource:
    """Static source content and provenance; never live capability acceptance."""

    policy: ProductionQcCollectorPolicySource
    provenance: ProductionQcSourceProvenance
    capability_set_profile_sha256: str
    resource_sha256: str


def _object(value: object, fields: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise InstalledProductionQcResourceError(f"{label} must have exactly its registered fields")
    mapping = cast(dict[object, object], value)
    if frozenset(mapping) != fields:
        raise InstalledProductionQcResourceError(f"{label} must have exactly its registered fields")
    # Exact key equality above establishes that every key is a registered str.
    return cast(dict[str, object], value)


def _source(value: object) -> bytes:
    if (
        type(value) is not str
        or not value
        or len(value) > 4 * ((_MAX_SOURCE_BYTES + 2) // 3)
    ):
        raise InstalledProductionQcResourceError("encoded source is invalid or oversized")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise InstalledProductionQcResourceError("source is not strict base64") from None
    if (
        not 0 < len(raw) <= _MAX_SOURCE_BYTES
        or base64.b64encode(raw).decode("ascii") != value
    ):
        raise InstalledProductionQcResourceError("source encoding or size is invalid")
    return raw


def decode_production_qc_resource(
    raw: bytes, *, expected_sha256: str
) -> InstalledProductionQcResource:
    """Validate transport content; caller bytes or a digest confer no authority."""
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_RESOURCE_BYTES:
        raise InstalledProductionQcResourceError("resource is empty, invalid or oversized")
    if (
        type(expected_sha256) is not str
        or _HASH.fullmatch(expected_sha256) is None
        or sha256_bytes(raw) != expected_sha256
    ):
        raise InstalledProductionQcResourceError("installed QC resource digest mismatch")
    try:
        value, canonical = load_canonical_json_bytes(raw, origin="installed production QC")
        if raw != canonical:
            raise InstalledProductionQcResourceError("resource bytes are not canonical")
        document = _object(
            value, frozenset({"schema_version", "provenance", "sources"}), "resource"
        )
        if document["schema_version"] != "installed-production-qc-policy-v1":
            raise InstalledProductionQcResourceError("unsupported QC resource schema")
        if canonical_json_bytes(document["provenance"]) != canonical_json_bytes(
            _SUPPORTED_PROVENANCE.to_mapping()
        ):
            raise InstalledProductionQcResourceError("unsupported QC source provenance")
        encoded = _object(document["sources"], _SOURCE_ROLES, "sources")
        sources = {role: _source(encoded[role]) for role in _SOURCE_ROLES}
        policy = decode_production_qc_static_policy_source(
            policy_raw=sources["policy"],
            policy_schema_raw=sources["policy_schema"],
            registry_snapshot_raw=sources["registry_snapshot"],
            registry_snapshot_schema_raw=sources["registry_snapshot_schema"],
            capability_set_profile_raw=sources["capability_set_profile"],
            capability_set_profile_schema_raw=sources["capability_set_profile_schema"],
        )
    except (CanonicalizationError, ProductionQcPolicySourceError, RecursionError) as error:
        raise InstalledProductionQcResourceError("installed QC source closure is invalid") from error
    return InstalledProductionQcResource(
        policy, _SUPPORTED_PROVENANCE,
        sha256_bytes(sources["capability_set_profile"]), expected_sha256,
    )


def load_installed_production_qc_resource() -> InstalledProductionQcResource:
    """Read only fixed package resources; no source path or selector arguments."""
    try:
        root = resources.files("autocut_kernel.registry").joinpath("_production_qc")
        with root.joinpath("production-qc.sha256").open("rb") as stream:
            digest_raw = stream.read(73)
        if len(digest_raw) != 72 or not digest_raw.endswith(b"\n"):
            raise InstalledProductionQcResourceError("QC sibling digest has invalid framing")
        expected = digest_raw[:-1].decode("ascii")
        with root.joinpath("production-qc.json").open("rb") as stream:
            raw = stream.read(_MAX_RESOURCE_BYTES + 1)
    except (OSError, UnicodeError, ModuleNotFoundError):
        raise InstalledProductionQcResourceError("installed QC resource is unavailable") from None
    return decode_production_qc_resource(raw, expected_sha256=expected)
