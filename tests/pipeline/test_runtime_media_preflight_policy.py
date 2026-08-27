"""Pure application policy coverage for accepted PC CUDA timed speech."""

from __future__ import annotations

from dataclasses import replace

import pytest

from auto_cut_bot.pipeline.media_preflight.runtime_policy import (
    RuntimeMediaPreflightPolicyError,
    project_pc_cuda_runtime_timed_speech_policy,
)
from tests.authority.test_runtime_timed_speech import _capability, _runtime_measurement, _selector
from tests.pipeline.runtime_profile_fixture import media_preflight_policy


def _projection():
    measurement = _runtime_measurement()
    return _selector(measurement).select(_capability(measurement), measurement)


def _static_policy(projection, **changes: object):
    values: dict[str, object] = {
        "timed_speech_policy_sha256": projection.timed_speech_policy_sha256,
        "utterance_gap_milliseconds": 300,
        "vad_merge_gap_milliseconds": 200,
    }
    values.update(changes)
    return media_preflight_policy(**values)


def test_projects_static_operational_limits_over_exact_cuda_authority() -> None:
    projection = _projection()
    static = _static_policy(projection, speech_device="mps")

    policy = project_pc_cuda_runtime_timed_speech_policy(static, projection)

    assert policy.device == "cuda"
    assert policy.runtime_measurement_identity_sha256 == projection.runtime_measurement_identity_sha256
    assert policy.accepted_record_sha256 == projection.record_sha256
    assert policy.validation_receipt_sha256 == projection.validation_receipt_sha256
    assert policy.runtime_projection_compatibility_sha256 == projection.compatibility_hash
    assert policy.runtime_projection_sha256 == projection.canonical_hash
    assert (policy.funasr_version, policy.torch_version) == (
        projection.funasr_version,
        projection.torch_version,
    )
    assert tuple(item.calibration_record_sha256 for item in policy.producers) == (
        projection.asr_calibration_record_sha256,
        projection.vad_calibration_record_sha256,
    )
    assert tuple(item.timing_error_bound_tick for item in policy.producers) == (
        projection.asr_timing_error_bound_tick,
        projection.vad_timing_error_bound_tick,
    )
    assert policy.endpoint_url == static.timed_speech_endpoint_url
    assert policy.utterance_gap_milliseconds == static.utterance_gap_milliseconds
    assert policy.to_mapping()["build_audit_sha256"] == projection.build_audit_sha256


def test_rejects_non_pc_cuda_projection_and_static_timing_drift() -> None:
    projection = _projection()

    with pytest.raises(RuntimeMediaPreflightPolicyError, match="pc_cuda CUDA"):
        project_pc_cuda_runtime_timed_speech_policy(
            _static_policy(projection),
            replace(projection, runtime_capability_id="mac_cpu"),
        )
    with pytest.raises(RuntimeMediaPreflightPolicyError, match="static timing policy"):
        project_pc_cuda_runtime_timed_speech_policy(
            _static_policy(projection, timed_speech_policy_sha256="sha256:" + "f" * 64),
            projection,
        )


def test_rejects_tampered_projection_producer_order_and_record_reference() -> None:
    projection = _projection()

    with pytest.raises(ValueError, match="ASR then VAD"):
        replace(projection, producers=(projection.producers[1], projection.producers[0]))
    # Projection construction itself closes exact record/receipt references;
    # an altered reference cannot be passed through the application boundary.
    with pytest.raises(ValueError, match="references must be distinct"):
        replace(projection, record_sha256=projection.validation_receipt_sha256)


def test_audit_only_rebuild_changes_provenance_hash_not_compatibility_hash() -> None:
    projection = _projection()
    original = project_pc_cuda_runtime_timed_speech_policy(_static_policy(projection), projection)
    rebuilt = project_pc_cuda_runtime_timed_speech_policy(
        _static_policy(projection),
        replace(projection, build_audit_sha256="sha256:" + "e" * 64),
    )

    assert rebuilt.build_audit_sha256 != original.build_audit_sha256
    assert rebuilt.canonical_hash != original.canonical_hash
    assert rebuilt.compatibility_hash == original.compatibility_hash
