from __future__ import annotations

import json
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.registry.production_qc_policy import (
    ProductionQcPolicySourceError,
    decode_production_qc_static_policy_source,
)

_ROOT = Path(__file__).resolve().parents[2]
_GOVERNANCE = _ROOT / "governance"


def _sources() -> dict[str, bytes]:
    return {
        "policy_raw": (_GOVERNANCE / "production-qc-collector-policy.json").read_bytes(),
        "registry_snapshot_raw": (
            _GOVERNANCE / "production-qc-collector-registry-snapshot.json"
        ).read_bytes(),
        "registry_snapshot_schema_raw": (
            _GOVERNANCE / "schemas/production-qc-collector-registry-snapshot.schema.json"
        ).read_bytes(),
        "policy_schema_raw": (
            _GOVERNANCE / "schemas/production-qc-collector-policy.schema.json"
        ).read_bytes(),
        "capability_set_profile_raw": (
            _GOVERNANCE / "production-qc-collector-capability-set-profile.json"
        ).read_bytes(),
        "capability_set_profile_schema_raw": (
            _GOVERNANCE / "schemas/production-qc-collector-capability-set-profile.schema.json"
        ).read_bytes(),
    }


def _decode(sources: dict[str, bytes], **replacement: bytes):
    return decode_production_qc_static_policy_source(**(sources | replacement))


def _json(raw: bytes) -> dict[str, object]:
    value = json.loads(raw)
    assert type(value) is dict
    return value


def test_real_qc_policy_sources_decode_to_static_content_without_self_hash() -> None:
    sources = _sources()
    policy = _decode(sources)
    source = _json(sources["policy_raw"])
    assert "policy_source_sha256" not in source
    assert policy.policy_source_sha256 == sha256_bytes(sources["policy_raw"])
    assert policy.profile_id == "production-av-qc-v1"
    assert policy.registry_snapshot_sha256 == source["registry_snapshot_sha256"]


@pytest.mark.parametrize("raw_kind", ("reformatted", "duplicate", "unknown", "self_hash"))
def test_policy_source_rejects_noncanonical_or_nonclosed_bytes(raw_kind: str) -> None:
    sources = _sources()
    raw = sources["policy_raw"]
    if raw_kind == "reformatted":
        raw = b"\n" + raw
    elif raw_kind == "duplicate":
        raw = raw.replace(
            b'"profile_id":"production-av-qc-v1"',
            b'"profile_id":"production-av-qc-v1","profile_id":"production-av-qc-v1"',
        )
    else:
        source = _json(raw)
        source["policy_source_sha256" if raw_kind == "self_hash" else "caller_generated"] = (
            "sha256:" + "3" * 64
        )
        raw = canonical_json_bytes(source)
    with pytest.raises(ProductionQcPolicySourceError):
        _decode(sources, policy_raw=raw)


@pytest.mark.parametrize(
    "raw",
    (b"[" * 1_000 + b"]" * 1_000, b"x" * (16 * 1024 + 1)),
)
def test_policy_source_bounds_or_normalizes_deep_malformed_bytes(raw: bytes) -> None:
    with pytest.raises(ProductionQcPolicySourceError):
        _decode(_sources(), policy_raw=raw)


@pytest.mark.parametrize(
    "source_key",
    (
        "policy_schema_raw",
        "registry_snapshot_schema_raw",
        "capability_set_profile_schema_raw",
    ),
)
def test_qc_policy_rejects_supported_schema_drift(source_key: str) -> None:
    sources = _sources()
    schema = _json(sources[source_key])
    schema["title"] = "caller-generated schema"
    with pytest.raises(ProductionQcPolicySourceError):
        _decode(sources, **{source_key: canonical_json_bytes(schema)})


def test_qc_policy_rejects_snapshot_identity_or_static_source_mismatch() -> None:
    sources = _sources()
    policy = _json(sources["policy_raw"])
    policy["registry_snapshot_sha256"] = "sha256:" + "4" * 64
    with pytest.raises(ProductionQcPolicySourceError):
        _decode(sources, policy_raw=canonical_json_bytes(policy))

    snapshot = _json(sources["registry_snapshot_raw"])
    snapshot["collector_registry_sha256"] = "sha256:" + "5" * 64
    with pytest.raises(ProductionQcPolicySourceError):
        _decode(sources, registry_snapshot_raw=canonical_json_bytes(snapshot))


@pytest.mark.parametrize("mutation", ("missing", "duplicate", "reordered", "foreign", "optional", "bool"))
def test_qc_capability_set_profile_requires_the_exact_two_members(mutation: str) -> None:
    sources = _sources()
    profile = _json(sources["capability_set_profile_raw"])
    members = profile["members"]
    assert type(members) is list
    if mutation == "missing":
        profile.pop("members")
    elif mutation == "duplicate":
        members.append(dict(members[0]))
    elif mutation == "reordered":
        members.reverse()
    elif mutation == "foreign":
        members[1]["artifact_type"] = "foreign"
    elif mutation == "optional":
        members[0]["optional"] = True
    else:
        members[0]["ordinal"] = False
    with pytest.raises(ProductionQcPolicySourceError):
        _decode(sources, capability_set_profile_raw=canonical_json_bytes(profile))
