"""Pure closure tests for the unaccepted production-QC collector capability."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.rendering.production_qc_collector_capability import (
    PRODUCTION_QC_COLLECTOR_CAPABILITY_STATE,
    PRODUCTION_QC_COLLECTOR_RUNNER_SCHEMA_VERSION,
    ProductionQcCollectorCapabilityError,
    ProductionQcCollectorCapabilityRequest,
    ProductionQcCollectorExecutableIdentity,
    ProductionQcCollectorLiveProfile,
    ProductionQcCollectorPolicySource,
    decode_production_qc_collector_capability_request,
    decode_production_qc_collector_live_profile,
    decode_production_qc_collector_policy_source,
    decode_unaccepted_production_qc_collector_capability_projection,
    project_unaccepted_production_qc_collector_capability,
)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tool(name: str, *, version: str | None = None) -> ProductionQcCollectorExecutableIdentity:
    return ProductionQcCollectorExecutableIdentity(
        executable_sha256=_hash(f"{name}-binary"),
        executable_byte_length=1234,
        version_output_sha256=_hash(version or f"{name}-version"),
    )


def _policy(
    *,
    registry: str = "collector-registry",
    environment: str = "fixed-environment",
) -> ProductionQcCollectorPolicySource:
    return ProductionQcCollectorPolicySource(
        profile_id="pc_cuda_qc",
        policy_source_sha256=_hash("protected-policy-source"),
        registry_snapshot_sha256=_hash("registry-snapshot"),
        required_check_set_version="production-av-qc-v1",
        collector_registry_sha256=_hash(registry),
        runner_schema_version=PRODUCTION_QC_COLLECTOR_RUNNER_SCHEMA_VERSION,
        fixed_environment_sha256=_hash(environment),
    )


def _live(
    policy: ProductionQcCollectorPolicySource,
    *,
    ffmpeg: ProductionQcCollectorExecutableIdentity | None = None,
    ffprobe: ProductionQcCollectorExecutableIdentity | None = None,
) -> ProductionQcCollectorLiveProfile:
    return ProductionQcCollectorLiveProfile(
        profile_id=policy.profile_id,
        policy_source_sha256=policy.policy_source_sha256,
        registry_snapshot_sha256=policy.registry_snapshot_sha256,
        required_check_set_version=policy.required_check_set_version,
        collector_registry_sha256=policy.collector_registry_sha256,
        runner_schema_version=policy.runner_schema_version,
        fixed_environment_sha256=policy.fixed_environment_sha256,
        ffmpeg_identity=ffmpeg or _tool("ffmpeg"),
        ffprobe_identity=ffprobe or _tool("ffprobe"),
    )


def _request(policy: ProductionQcCollectorPolicySource | None = None) -> ProductionQcCollectorCapabilityRequest:
    source = policy or _policy()
    return ProductionQcCollectorCapabilityRequest(source, _live(source))


def test_candidate_identity_is_stable_canonical_and_explicitly_non_authoritative() -> None:
    request = _request()
    projection = project_unaccepted_production_qc_collector_capability(request)

    assert request.canonical_sha256 == _request().canonical_sha256
    assert projection.canonical_sha256 == project_unaccepted_production_qc_collector_capability(
        _request()
    ).canonical_sha256
    assert request.authority_state == PRODUCTION_QC_COLLECTOR_CAPABILITY_STATE
    assert projection.authority_state == PRODUCTION_QC_COLLECTOR_CAPABILITY_STATE
    assert projection.is_authoritative is False
    assert decode_production_qc_collector_capability_request(
        canonical_json_bytes(request.to_mapping())
    ) == request
    assert decode_unaccepted_production_qc_collector_capability_projection(
        canonical_json_bytes(projection.to_mapping())
    ) == projection


@pytest.mark.parametrize(
    ("decoder", "mapping"),
    (
        (
            decode_production_qc_collector_policy_source,
            lambda: _policy().to_mapping(),
        ),
        (
            decode_production_qc_collector_live_profile,
            lambda: _live(_policy()).to_mapping(),
        ),
        (
            decode_production_qc_collector_capability_request,
            lambda: _request().to_mapping(),
        ),
        (
            decode_unaccepted_production_qc_collector_capability_projection,
            lambda: project_unaccepted_production_qc_collector_capability(_request()).to_mapping(),
        ),
    ),
)
def test_all_wire_mappings_are_closed(decoder, mapping) -> None:
    value = mapping()
    value["unexpected"] = "nope"
    with pytest.raises(ProductionQcCollectorCapabilityError, match="closed schema"):
        decoder(canonical_json_bytes(value))


def test_tool_version_changes_alter_projection_and_policy_identity_drift_rejects() -> None:
    policy = _policy()
    baseline = project_unaccepted_production_qc_collector_capability(
        ProductionQcCollectorCapabilityRequest(policy, _live(policy))
    )
    updated = project_unaccepted_production_qc_collector_capability(
        ProductionQcCollectorCapabilityRequest(
            policy,
            _live(policy, ffmpeg=_tool("ffmpeg", version="ffmpeg-version-2")),
        )
    )
    assert updated.canonical_sha256 != baseline.canonical_sha256

    changed_registry = _policy(registry="collector-registry-2")
    changed_environment = _policy(environment="fixed-environment-2")
    assert (
        project_unaccepted_production_qc_collector_capability(
            ProductionQcCollectorCapabilityRequest(changed_registry, _live(changed_registry))
        ).canonical_sha256
        != baseline.canonical_sha256
    )
    assert (
        project_unaccepted_production_qc_collector_capability(
            ProductionQcCollectorCapabilityRequest(changed_environment, _live(changed_environment))
        ).canonical_sha256
        != baseline.canonical_sha256
    )

    with pytest.raises(ProductionQcCollectorCapabilityError, match="collector_registry_sha256"):
        ProductionQcCollectorCapabilityRequest(policy, _live(changed_registry))
    with pytest.raises(ProductionQcCollectorCapabilityError, match="fixed_environment_sha256"):
        ProductionQcCollectorCapabilityRequest(policy, _live(changed_environment))


def test_ffprobe_identity_changes_alter_projection_without_leaking_other_tool_domains() -> None:
    policy = _policy()
    baseline = project_unaccepted_production_qc_collector_capability(
        ProductionQcCollectorCapabilityRequest(policy, _live(policy))
    )
    updated = project_unaccepted_production_qc_collector_capability(
        ProductionQcCollectorCapabilityRequest(
            policy,
            _live(policy, ffprobe=_tool("ffprobe", version="ffprobe-version-2")),
        )
    )
    assert updated.canonical_sha256 != baseline.canonical_sha256

    visible_keys = set(updated.to_mapping()["request"]["live_profile"])
    assert not visible_keys & {
        "asr_identity",
        "asr_model_id",
        "funasr_identity",
        "timed_speech_policy_sha256",
        "vad_identity",
        "vad_model_id",
    }
    non_qc_input = _live(policy).to_mapping()
    non_qc_input["asr_model_id"] = "SenseVoiceSmall"
    with pytest.raises(ProductionQcCollectorCapabilityError, match="closed schema"):
        decode_production_qc_collector_live_profile(canonical_json_bytes(non_qc_input))


def test_module_ast_has_no_effectful_or_authority_imports() -> None:
    source_path = (
        Path(__file__).parents[2]
        / "packages/autocut-kernel/src/autocut_kernel/rendering/production_qc_collector_capability.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = {
        "os",
        "pathlib",
        "socket",
        "subprocess",
        "requests",
        "http",
        "importlib",
        "store",
        "production_qc_runner",
        "production_process",
        "runtime_measurement_identity",
        "calibration_record",
    }
    assert not {name.split(".")[-1] for name in imports} & forbidden
