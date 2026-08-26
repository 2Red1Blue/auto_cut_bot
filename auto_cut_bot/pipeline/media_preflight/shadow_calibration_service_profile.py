"""Pure projection of a locked shadow source into the native service protocol.

This validates grammar and identity closure, not Git/source provenance or the
installed model trees. Controlled administration owns deployment; the service
independently checks its actual service/model bytes before starting inference.
"""

from __future__ import annotations

import json

from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.registry.authority_profiles import (
    CudaShadowCalibrationProfileSource,
    ShadowCalibrationProfileSource,
    Stage1NarrativeProfileSource,
    decode_cuda_shadow_calibration_profile_source,
    decode_shadow_calibration_profile_source,
)


class ShadowCalibrationServiceProfileError(ValueError):
    """The locked source cannot be projected without changing its identity."""


def build_funasr_shadow_service_profile(
    *,
    profile: ShadowCalibrationProfileSource,
    narrative: Stage1NarrativeProfileSource,
    expected_profile_contract_sha256: str,
) -> bytes:
    """Return service JSON bytes without writing configuration or granting authority."""
    if type(profile) is not ShadowCalibrationProfileSource:  # noqa: E721
        raise ShadowCalibrationServiceProfileError("profile must be an exact shadow source")
    mapping = profile.to_mapping()
    if canonical_json_hash(mapping) != profile.canonical_sha256:
        raise ShadowCalibrationServiceProfileError("shadow source canonical hash mismatch")
    checked = decode_shadow_calibration_profile_source(
        canonical_json_bytes(mapping),
        narrative=narrative,
        expected_profile_contract_sha256=expected_profile_contract_sha256,
    )
    # A lone optional bound is omitted by producer.to_mapping(). Reject that
    # hidden local-run state too, rather than silently erasing it in projection.
    if checked.native_timed_speech != profile.native_timed_speech:
        raise ShadowCalibrationServiceProfileError("typed native source contains non-shadow state")
    policy, native = checked.timing_policies, checked.native_timed_speech
    projected = {
        "schema_version": "funasr-shadow-calibration-profile-v1",
        **native.to_mapping(),
        "timed_speech_policy_sha256": policy.timed_speech_policy_sha256,
        "word_gap_policy_sha256": policy.word_gap_policy_sha256,
        "vad_merge_policy_sha256": policy.vad_merge_policy_sha256,
        "utterance_gap_milliseconds": policy.word_gap_ms,
        "vad_merge_gap_milliseconds": policy.vad_merge_gap_ms,
    }
    measured = canonical_sha256(
        {key: value for key, value in projected.items() if key != "native_port_identity_sha256"}
    )
    if measured != native.native_port_identity_sha256:
        raise ShadowCalibrationServiceProfileError(
            "locked native identity does not match service projection"
        )
    return json.dumps(
        projected, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def build_funasr_cuda_shadow_service_profile(
    *,
    profile: CudaShadowCalibrationProfileSource,
    narrative: Stage1NarrativeProfileSource,
    expected_profile_contract_sha256: str,
) -> bytes:
    """Return a CUDA shadow-only service profile without granting run authority.

    The build audit identity is intentionally retained next to the derived
    timing compatibility identity.  The latter is recomputed by the closed
    source decoder; callers cannot substitute either a device or a digest.
    """
    if type(profile) is not CudaShadowCalibrationProfileSource:  # noqa: E721
        raise ShadowCalibrationServiceProfileError("profile must be an exact CUDA shadow source")
    mapping = profile.to_mapping()
    if canonical_json_hash(mapping) != profile.canonical_sha256:
        raise ShadowCalibrationServiceProfileError("CUDA shadow source canonical hash mismatch")
    checked = decode_cuda_shadow_calibration_profile_source(
        canonical_json_bytes(mapping),
        narrative=narrative,
        expected_profile_contract_sha256=expected_profile_contract_sha256,
    )
    if checked != profile:
        raise ShadowCalibrationServiceProfileError("typed CUDA source contains non-shadow state")
    policy, native, compatibility = (
        checked.timing_policies,
        checked.cuda_timed_speech,
        checked.timing_compatibility,
    )
    # Keep the native protocol hash acyclic and audit-independent: it closes
    # over timing inputs, while the build audit and derived compatibility
    # identity are emitted only after protocol closure has succeeded.
    native_projection = {
        "schema_version": "funasr-cuda-shadow-calibration-profile-v1",
        **native.to_mapping(),
        "device": compatibility.device.to_mapping(),
        "timed_speech_policy_sha256": policy.timed_speech_policy_sha256,
        "word_gap_policy_sha256": policy.word_gap_policy_sha256,
        "vad_merge_policy_sha256": policy.vad_merge_policy_sha256,
        "utterance_gap_milliseconds": policy.word_gap_ms,
        "vad_merge_gap_milliseconds": policy.vad_merge_gap_ms,
    }
    native_identity_input = {
        key: value
        for key, value in native_projection.items()
        if key not in {"native_port_identity_sha256", "build_audit_sha256"}
    }
    native_identity_input["producers"] = [
        {
            key: value
            for key, value in producer.to_mapping().items()
            if key not in {"build_audit_sha256", "service_sha256"}
        }
        for producer in native.producers
    ]
    measured = canonical_sha256(native_identity_input)
    if measured != native.native_port_identity_sha256:
        raise ShadowCalibrationServiceProfileError(
            "locked CUDA native identity does not match service projection"
        )
    projected = {
        **native_projection,
        "timing_compatibility_sha256": compatibility.timing_compatibility_sha256,
    }
    return json.dumps(
        projected, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
