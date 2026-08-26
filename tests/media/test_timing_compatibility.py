"""Focused closure and equality tests for timing-compatibility identities."""

from __future__ import annotations

from copy import deepcopy

import pytest
from autocut_kernel.media.timing_compatibility import (
    TIMING_COMPATIBILITY_PROFILE_SCHEMA,
    TimingCompatibilityError,
    build_timing_compatibility_profile,
    decode_timing_compatibility_profile,
)

H = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def measured_profile(*, device_class: str = "cuda") -> dict[str, object]:
    device: dict[str, str] = {"device_class": device_class}
    if device_class == "cuda":
        device.update(cuda_runtime_version="12.8", gpu_compute_capability="8.9")
    return {
        "schema_version": TIMING_COMPATIBILITY_PROFILE_SCHEMA,
        "timing_engine_compatibility_version": "funasr-timing-v1",
        "build_audit_sha256": H,
        "runtime": {
            "funasr_version": "2.0.4",
            "torch_version": "2.7.0",
            "device": device,
        },
        "decode": {
            "decoder_identity_sha256": H,
            "resampling_identity_sha256": H,
            "native_protocol_identity_sha256": H,
        },
        "policies": {
            "word_timestamp_policy_sha256": H,
            "vad_merge_policy_sha256": H,
        },
        "producers": [
            {
                "producer_kind": "asr",
                "producer_id": "sensevoice",
                "producer_version": "1.0.0",
                "model_id": "SenseVoiceSmall",
                "model_revision": "abc123",
                "model_sha256": H,
                "inference_identity_sha256": H,
            },
            {
                "producer_kind": "vad",
                "producer_id": "fsmn-vad",
                "producer_version": "1.0.0",
                "model_id": "fsmn-vad",
                "model_revision": "def456",
                "model_sha256": H,
                "inference_identity_sha256": H,
            },
        ],
    }


def test_build_audit_change_preserves_derived_timing_compatibility() -> None:
    first = build_timing_compatibility_profile(measured_profile())
    changed = measured_profile()
    changed["build_audit_sha256"] = OTHER
    second = build_timing_compatibility_profile(changed)

    assert first.build_audit_sha256 != second.build_audit_sha256
    assert first.timing_compatibility_sha256 == second.timing_compatibility_sha256
    assert first.to_mapping()["build_audit_sha256"] == H
    assert second.to_mapping()["build_audit_sha256"] == OTHER


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["producers"][0].update(model_sha256=OTHER),
        lambda value: value["runtime"].update(torch_version="2.7.1"),
        lambda value: value["runtime"].update(
            device={
                "device_class": "cuda",
                "cuda_runtime_version": "12.8",
                "gpu_compute_capability": "9.0",
            }
        ),
        lambda value: value["decode"].update(resampling_identity_sha256=OTHER),
        lambda value: value["policies"].update(vad_merge_policy_sha256=OTHER),
        lambda value: value.update(timing_engine_compatibility_version="funasr-timing-v2"),
    ],
)
def test_each_timing_relevant_identity_change_breaks_compatibility(mutate) -> None:
    original = build_timing_compatibility_profile(measured_profile())
    changed = measured_profile()
    mutate(changed)

    assert build_timing_compatibility_profile(changed).timing_compatibility_sha256 != (
        original.timing_compatibility_sha256
    )


def test_builder_rejects_claimed_or_unknown_derived_identity_and_decoder_recomputes() -> None:
    measured = measured_profile()
    profile = build_timing_compatibility_profile(measured)
    complete = profile.to_mapping()

    with pytest.raises(TimingCompatibilityError, match="unknown"):
        build_timing_compatibility_profile(complete)
    with pytest.raises(TimingCompatibilityError, match="does not match"):
        decode_timing_compatibility_profile({**complete, "timing_compatibility_sha256": OTHER})
    assert decode_timing_compatibility_profile(complete) == profile


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["runtime"].update(
            device={"device_class": "cpu", "cuda_runtime_version": "12.8"}
        ),
        lambda value: value["runtime"].update(device={"device_class": "cuda"}),
        lambda value: value["runtime"].update(device={"device_class": "cuda:0"}),
        lambda value: value["runtime"]["device"].update(device_index="0"),
        lambda value: value["producers"].__setitem__(1, deepcopy(value["producers"][0])),
    ],
)
def test_device_and_duplicate_logical_producer_identities_are_fail_closed(mutation) -> None:
    measured = measured_profile()
    mutation(measured)

    with pytest.raises(TimingCompatibilityError):
        build_timing_compatibility_profile(measured)


@pytest.mark.parametrize(
    "path",
    [
        ("build_audit_sha256",),
        ("decode", "decoder_identity_sha256"),
        ("policies", "word_timestamp_policy_sha256"),
        ("producers", 0, "model_sha256"),
    ],
)
def test_zero_or_invalid_sha_identities_are_rejected(path: tuple[object, ...]) -> None:
    measured = measured_profile()
    target: object = measured
    for member in path[:-1]:
        target = target[member]  # type: ignore[index]
    target[path[-1]] = "sha256:" + "0" * 64  # type: ignore[index]
    with pytest.raises(TimingCompatibilityError):
        build_timing_compatibility_profile(measured)
