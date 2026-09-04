from __future__ import annotations

import hashlib
from pathlib import Path

from autocut_kernel.contracts.compiler.canonical import canonical_json_hash, load_canonical_json_bytes
from autocut_kernel.rendering.production_process import (
    FIXED_PROCESS_ENVIRONMENT,
    ProductionExecutableIdentity,
)
from autocut_kernel.rendering.production_qc_runner import ProductionRenderQcCollectorProfile


_ROOT = Path(__file__).resolve().parents[2]
_GOVERNANCE = _ROOT / "governance"
_REGISTRY_HASH = "sha256:285cbd72a611ea4fe3bded0a2dd0774a90b89fc3f52eeef146ecc03150c18e7e"
_ENVIRONMENT_HASH = "sha256:1d36b32cd19a15c1e5d98f1ac689b632b8ebde9a41a24cbb0185844a13728e78"


def _canonical_object(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    parsed, canonical = load_canonical_json_bytes(raw, origin=str(path))
    assert raw == canonical
    assert type(parsed) is dict
    return parsed


def test_production_qc_policy_source_has_no_self_hash_and_binds_snapshot() -> None:
    snapshot_path = _GOVERNANCE / "production-qc-collector-registry-snapshot.json"
    snapshot_schema_path = _GOVERNANCE / "schemas/production-qc-collector-registry-snapshot.schema.json"
    policy = _canonical_object(_GOVERNANCE / "production-qc-collector-policy.json")
    snapshot = _canonical_object(snapshot_path)

    assert set(snapshot) == {
        "schema_version",
        "profile_id",
        "required_check_set_version",
        "collector_registry_sha256",
        "runner_schema_version",
        "fixed_environment_sha256",
    }
    assert snapshot == {
        "schema_version": "production-qc-collector-registry-snapshot-v1",
        "profile_id": "production-av-qc-v1",
        "required_check_set_version": "production-av-qc-v1",
        "collector_registry_sha256": _REGISTRY_HASH,
        "runner_schema_version": "production-qc-runner-v1",
        "fixed_environment_sha256": _ENVIRONMENT_HASH,
    }
    synthetic_tool = ProductionExecutableIdentity(
        "sha256:" + "1" * 64,
        1,
        "sha256:" + "2" * 64,
    )
    runner_profile = ProductionRenderQcCollectorProfile(
        "production-av-qc-v1", synthetic_tool, synthetic_tool
    )
    assert snapshot["collector_registry_sha256"] == runner_profile.registry_sha256
    assert snapshot["fixed_environment_sha256"] == canonical_json_hash(
        dict(FIXED_PROCESS_ENVIRONMENT)
    )
    assert set(policy) == {
        "schema_version",
        "profile_id",
        "registry_snapshot_sha256",
        "required_check_set_version",
        "collector_registry_sha256",
        "runner_schema_version",
        "fixed_environment_sha256",
    }
    assert "policy_source_sha256" not in policy
    assert {key: policy[key] for key in snapshot if key != "schema_version"} == {
        key: snapshot[key] for key in snapshot if key != "schema_version"
    }
    identity = {
        "schema_version": "production-qc-collector-registry-snapshot-identity-v1",
        "sources": [
            {
                "role": "registry_snapshot",
                "sha256": "sha256:" + hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            },
            {
                "role": "registry_snapshot_schema",
                "sha256": "sha256:"
                + hashlib.sha256(snapshot_schema_path.read_bytes()).hexdigest(),
            },
        ],
    }
    assert policy["registry_snapshot_sha256"] == canonical_json_hash(identity)


def test_production_qc_capability_profile_is_exactly_two_closed_members() -> None:
    profile = _canonical_object(_GOVERNANCE / "production-qc-collector-capability-set-profile.json")
    assert profile == {
        "schema_version": "production-qc-collector-capability-set-profile-v1",
        "artifact_set_profile": "production_qc_collector_capability_v1",
        "members": [
            {
                "ordinal": 0,
                "artifact_type": "production_qc_collector_measurement",
                "logical_id": "measurement",
                "revision": 1,
            },
            {
                "ordinal": 1,
                "artifact_type": "production_qc_collector_capability",
                "logical_id": "decision",
                "revision": 1,
            },
        ],
    }


def test_production_qc_source_schemas_are_closed_and_indexed() -> None:
    index = (_GOVERNANCE / "schema-index.yaml").read_text(encoding="utf-8")
    assert (
        "ProductionQcCollectorRegistrySnapshot:"
        " schemas/production-qc-collector-registry-snapshot.schema.json" in index
    )
    assert (
        "ProductionQcCollectorPolicySource:"
        " schemas/production-qc-collector-policy.schema.json" in index
    )
    assert (
        "ProductionQcCollectorCapabilitySetProfile:"
        " schemas/production-qc-collector-capability-set-profile.schema.json" in index
    )
    for filename in (
        "production-qc-collector-registry-snapshot.schema.json",
        "production-qc-collector-policy.schema.json",
        "production-qc-collector-capability-set-profile.schema.json",
    ):
        schema = _canonical_object(_GOVERNANCE / "schemas" / filename)
        assert schema["additionalProperties"] is False
