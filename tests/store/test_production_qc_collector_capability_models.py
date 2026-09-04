"""Unit closure for production-QC collector capability bindings and members."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest
from autocut_kernel.registry.installed_production_qc import (
    InstalledProductionQcResourceError,
    load_installed_production_qc_resource,
)
from autocut_kernel.rendering.production_qc_collector_capability import (
    ProductionQcCollectorCapabilityRequest,
    ProductionQcCollectorExecutableIdentity,
    ProductionQcCollectorLiveProfile,
    project_unaccepted_production_qc_collector_capability,
)
from autocut_kernel.store import (
    PRODUCTION_QC_COLLECTOR_CAPABILITY_COMMAND_NAME,
    PRODUCTION_QC_COLLECTOR_CAPABILITY_IDEMPOTENCY_PREFIX,
    CommandSuccess,
    ProductionQcCollectorCapabilityBinding,
    StoreValidationError,
)
from autocut_kernel.store.models import artifact_set_hash, canonical_payload_hash

SHA256_A = "sha256:" + "a" * 64
SHA256_B = "sha256:" + "b" * 64
SHA256_C = "sha256:" + "c" * 64
SHA256_D = "sha256:" + "d" * 64


def _installed() -> object:
    return load_installed_production_qc_resource()


def _identity(executable: str, version: str, length: int = 128) -> ProductionQcCollectorExecutableIdentity:
    return ProductionQcCollectorExecutableIdentity(executable, length, version)


def _live_profile(policy: object) -> ProductionQcCollectorLiveProfile:
    return ProductionQcCollectorLiveProfile(
        policy.profile_id,
        policy.policy_source_sha256,
        policy.registry_snapshot_sha256,
        policy.required_check_set_version,
        policy.collector_registry_sha256,
        policy.runner_schema_version,
        policy.fixed_environment_sha256,
        _identity(SHA256_A, SHA256_B),
        _identity(SHA256_C, SHA256_D),
    )


def _binding() -> ProductionQcCollectorCapabilityBinding:
    resource = _installed()
    request = ProductionQcCollectorCapabilityRequest(resource.policy, _live_profile(resource.policy))
    return ProductionQcCollectorCapabilityBinding(request, resource.provenance)


def test_installed_resource_loads_from_source_tree() -> None:
    resource = _installed()
    assert resource.policy.profile_id == "production-av-qc-v1"
    assert resource.provenance.authority_revision >= 1
    assert resource.resource_sha256.startswith("sha256:")


def test_binding_job_binds_complete_claimed_identity() -> None:
    binding = _binding()
    policy = binding.policy
    runner = binding.request.live_profile.canonical_sha256.removeprefix("sha256:")
    expected = (
        "autocut_production_qc_collector_validator:"
        f"{policy.profile_id}"
        f":{policy.policy_source_sha256.removeprefix('sha256:')}"
        f":{policy.registry_snapshot_sha256.removeprefix('sha256:')}"
        f":{runner}"
    )
    assert binding.job.job_key == expected
    assert binding.job.profile == "authority"


def test_binding_idempotency_key_pins_complete_request() -> None:
    binding = _binding()
    key = binding.attempt_idempotency_key
    assert key.startswith(PRODUCTION_QC_COLLECTOR_CAPABILITY_IDEMPOTENCY_PREFIX)
    assert binding.request.canonical_sha256.removeprefix("sha256:") in key
    changed = replace(
        binding.request.live_profile,
        ffmpeg_identity=_identity("sha256:" + "1" * 64, SHA256_B),
    )
    changed_request = ProductionQcCollectorCapabilityRequest(binding.request.policy_source, changed)
    changed_binding = ProductionQcCollectorCapabilityBinding(
        changed_request, binding.provenance
    )
    assert changed_binding.attempt_idempotency_key != key


def test_binding_claim_is_deterministic_validator_identity() -> None:
    binding = _binding()
    claim = binding.claim
    assert claim.command_name == PRODUCTION_QC_COLLECTOR_CAPABILITY_COMMAND_NAME
    assert claim.execution_kind == "deterministic"
    assert claim.job == binding.job
    assert claim.idempotency_key == binding.attempt_idempotency_key
    assert claim.request_hash == binding.request_hash


def test_binding_members_are_closed_and_hashed() -> None:
    binding = _binding()
    measurement, decision = binding.members
    assert (measurement.artifact_type, measurement.logical_id, measurement.revision) == (
        "production_qc_collector_measurement",
        "measurement",
        1,
    )
    assert (decision.artifact_type, decision.logical_id, decision.revision) == (
        "production_qc_collector_capability",
        "decision",
        1,
    )
    for member in (measurement, decision):
        assert canonical_payload_hash(member.payload_json) == member.content_hash
        assert member.scope == binding.scope
    decision_payload = json.loads(decision.payload_json)
    assert decision_payload["measurement_member_sha256"] == measurement.content_hash
    assert decision_payload["decision"] == "accepted"
    assert decision_payload["authority_provenance"] == binding.provenance.to_mapping()
    assert decision_payload["policy_source"] == binding.policy.to_mapping()


def test_binding_expected_set_hash_matches_command_success() -> None:
    binding = _binding()
    success = CommandSuccess(
        uuid.uuid4(),
        binding.expected_set_hash,
        binding.members,
    )
    assert success.set_hash == artifact_set_hash(binding.members)


def test_binding_rejects_wrong_provenance_type() -> None:
    resource = _installed()
    request = ProductionQcCollectorCapabilityRequest(resource.policy, _live_profile(resource.policy))
    with pytest.raises(StoreValidationError):
        ProductionQcCollectorCapabilityBinding(request, "not-provenance")  # pyright: ignore[reportArgumentType]


def test_binding_scope_key_is_deterministic_from_tuple() -> None:
    binding = _binding()
    policy = binding.policy
    runner = binding.request.live_profile.canonical_sha256.removeprefix("sha256:")
    assert binding.scope_key == (
        "production_qc_collector_capability:"
        f"{policy.profile_id}"
        f":{policy.policy_source_sha256.removeprefix('sha256:')}"
        f":{policy.registry_snapshot_sha256.removeprefix('sha256:')}"
        f":{runner}"
    )
    assert binding.scope.namespace == "autocut_authority"
    assert binding.scope.kind == "production_qc_collector_capability"


def test_mismatched_live_profile_is_rejected_before_claim() -> None:
    resource = _installed()
    policy = resource.policy
    drifted = ProductionQcCollectorLiveProfile(
        policy.profile_id,
        policy.policy_source_sha256,
        policy.registry_snapshot_sha256,
        policy.required_check_set_version,
        "sha256:" + "e" * 64,  # collector registry drift
        policy.runner_schema_version,
        policy.fixed_environment_sha256,
        _identity(SHA256_A, SHA256_B),
        _identity(SHA256_C, SHA256_D),
    )
    with pytest.raises(Exception, match="collector_registry_sha256"):
        ProductionQcCollectorCapabilityRequest(policy, drifted)


def test_unaccepted_projection_stays_non_authoritative() -> None:
    resource = _installed()
    request = ProductionQcCollectorCapabilityRequest(resource.policy, _live_profile(resource.policy))
    projection = project_unaccepted_production_qc_collector_capability(request)
    assert projection.is_authoritative is False
    assert projection.authority_state == "store_acceptance_required"


def test_installed_resource_rejects_digest_drift() -> None:
    from autocut_kernel.registry.installed_production_qc import decode_production_qc_resource

    resource = _installed()
    with pytest.raises(InstalledProductionQcResourceError):
        decode_production_qc_resource(b"{}", expected_sha256=resource.resource_sha256)
