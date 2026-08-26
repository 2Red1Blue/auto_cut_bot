"""Synthetic decoded sources prove compatibility only, never calibration acceptance."""

from __future__ import annotations

import socket
import subprocess
from dataclasses import replace

import pytest
from autocut_kernel.media.types import TimeBase

from auto_cut_bot.pipeline.media_preflight.installed_policy import (
    InstalledMediaPolicyError,
    validate_installed_media_policy,
)
from auto_cut_bot.pipeline.media_preflight.models import LocalMediaPreflightPolicy
from tests.pipeline.installed_profile_fixture import (
    synthetic_installed_resource,
    synthetic_media_policy,
)

FOREIGN_HASH = "sha256:" + "f" * 64


@pytest.fixture(scope="module")
def synthetic_pair():
    resource = synthetic_installed_resource()
    return resource, synthetic_media_policy(resource)


def _calibration_change(policy, kind, **changes):
    return replace(policy, calibrations=tuple(
        replace(item, **changes) if item.producer_kind == kind else item
        for item in policy.calibrations
    ))


def test_matching_synthetic_policy_is_read_only_without_native_or_network(synthetic_pair, monkeypatch):
    resource, policy = synthetic_pair
    before = policy.to_mapping()

    def forbidden(*args, **kwargs):
        pytest.fail("compatibility checker attempted a native/network process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert validate_installed_media_policy(resource, policy) is None
    assert policy.to_mapping() == before
    assert policy.timed_speech_calibration_sha256 == resource.local_run.calibration.record_ref.content_hash
    for kind, source in zip(("asr", "vad"), resource.local_run.native_timed_speech.producers, strict=True):
        assert policy.calibration(kind).calibration_record_sha256 == source.producer_record_sha256
        assert policy.timed_speech_detector_sha256(kind) == source.detector_sha256


@pytest.mark.parametrize(("field", "value"), (
    ("timed_speech_provider_id", "foreign-provider"),
    ("timed_speech_provider_version", "foreign-version"),
    ("timed_speech_service_sha256", FOREIGN_HASH),
    ("funasr_version", "foreign-version"),
    ("torch_version", "foreign-version"),
    ("speech_device", "mps"),
    ("word_timing_capability", "sentence_only"),
    ("asr_model_id", "foreign-model"),
    ("asr_model_revision", "foreign-revision"),
    ("asr_model_sha256", FOREIGN_HASH),
    ("vad_model_id", "foreign-model"),
    ("vad_model_revision", "foreign-revision"),
    ("vad_model_sha256", FOREIGN_HASH),
    ("timed_speech_policy_sha256", FOREIGN_HASH),
    ("timed_speech_calibration_sha256", FOREIGN_HASH),
    ("utterance_gap_milliseconds", 181),
    ("vad_merge_gap_milliseconds", 121),
))
def test_every_source_backed_top_level_field_rejects_drift(synthetic_pair, field, value):
    resource, policy = synthetic_pair
    assert getattr(policy, field) != value
    with pytest.raises(InstalledMediaPolicyError):
        validate_installed_media_policy(resource, replace(policy, **{field: value}))


@pytest.mark.parametrize("kind", ("asr", "vad"))
@pytest.mark.parametrize(("field", "value"), (
    ("producer_id", "foreign-id"),
    ("producer_version", "foreign-version"),
    ("generation_policy_sha256", FOREIGN_HASH),
    ("detector_sha256", FOREIGN_HASH),
    ("calibration_policy_sha256", FOREIGN_HASH),
    ("calibration_record_sha256", FOREIGN_HASH),
    ("timing_error_bound_microseconds", 1),
))
def test_each_asr_vad_calibration_metadata_field_rejects_drift(synthetic_pair, kind, field, value):
    resource, policy = synthetic_pair
    assert getattr(policy.calibration(kind), field) != value
    with pytest.raises(InstalledMediaPolicyError):
        validate_installed_media_policy(resource, _calibration_change(policy, kind, **{field: value}))


@pytest.mark.parametrize("kind", ("asr", "vad"))
def test_child_hash_cannot_replace_aggregate_calibration(synthetic_pair, kind):
    resource, policy = synthetic_pair
    substituted = replace(policy, timed_speech_calibration_sha256=policy.calibration(kind).calibration_record_sha256)
    with pytest.raises(InstalledMediaPolicyError, match="aggregate calibration"):
        validate_installed_media_policy(resource, substituted)


def test_asr_vad_child_records_cannot_be_swapped(synthetic_pair):
    resource, policy = synthetic_pair
    wrong = _calibration_change(policy, "asr", calibration_record_sha256=policy.calibration("vad").calibration_record_sha256)
    wrong = _calibration_change(wrong, "vad", calibration_record_sha256=policy.calibration("asr").calibration_record_sha256)
    with pytest.raises(InstalledMediaPolicyError):
        validate_installed_media_policy(resource, wrong)


@pytest.mark.parametrize("kind", ("asr", "vad"))
def test_rounding_to_the_same_tick_cannot_substitute_a_smaller_bound(synthetic_pair, kind):
    resource, policy = synthetic_pair
    # 6999us still ceils to 7ms (and 10999us to 11ms), but is not exact.
    wrong = _calibration_change(policy, kind, timing_error_bound_microseconds=policy.calibration(kind).timing_error_bound_microseconds - 1)
    with pytest.raises(InstalledMediaPolicyError, match="exact positive installed bound"):
        validate_installed_media_policy(resource, wrong)


def test_non_integral_microsecond_source_bound_is_not_rounded_into_compatibility(synthetic_pair):
    resource, policy = synthetic_pair
    # Explicit typed test mutation: 7 ticks * 1/48000s = 145.833...us.
    source = replace(resource.local_run, source_clock_policy=replace(resource.local_run.source_clock_policy, time_base=TimeBase(1, 48_000)))
    wrong = _calibration_change(policy, "asr", timing_error_bound_microseconds=145)
    with pytest.raises(InstalledMediaPolicyError, match="exact positive installed bound"):
        validate_installed_media_policy(replace(resource, local_run=source), wrong)


@pytest.mark.parametrize("bound", (0, -1, True, 7.0))
def test_typed_source_with_invalid_bound_cannot_establish_compatibility(synthetic_pair, bound):
    resource, policy = synthetic_pair
    calibration = replace(resource.local_run.calibration, asr_timing_error_bound_tick=bound)
    source = replace(resource.local_run, calibration=calibration)
    with pytest.raises(InstalledMediaPolicyError, match="exact positive installed bound"):
        validate_installed_media_policy(replace(resource, local_run=source), policy)


@pytest.mark.parametrize("kind", ("asr", "vad"))
def test_matching_detector_claims_still_require_existing_computed_detector_identity(synthetic_pair, kind):
    resource, policy = synthetic_pair
    native = resource.local_run.native_timed_speech
    source = replace(resource.local_run, native_timed_speech=replace(native, producers=tuple(
        replace(item, detector_sha256=FOREIGN_HASH) if item.producer_kind == kind else item
        for item in native.producers
    )))
    wrong = _calibration_change(policy, kind, detector_sha256=FOREIGN_HASH)
    with pytest.raises(InstalledMediaPolicyError, match="producer identity"):
        validate_installed_media_policy(replace(resource, local_run=source), wrong)


def test_unlocked_visual_calibration_and_operational_values_are_not_certified(synthetic_pair):
    resource, policy = synthetic_pair
    changed = _calibration_change(policy, "visual", calibration_record_sha256=FOREIGN_HASH)
    changed = replace(changed, timed_speech_endpoint_url="http://127.0.0.1:10096/v1/timed-speech-evidence",
                      timed_speech_timeout_seconds=31, timed_speech_max_response_bytes=1024,
                      black_luma_max=11, policy_id="another-operational-policy")
    assert validate_installed_media_policy(resource, changed) is None


def test_bad_type_is_a_safe_value_error(synthetic_pair):
    resource, policy = synthetic_pair
    for left, right in ((object(), policy), (resource, object())):
        with pytest.raises(InstalledMediaPolicyError, match="exact installed content and media policy"):
            validate_installed_media_policy(left, right)


def test_unknown_config_fields_stay_rejected_by_existing_policy_decoder(synthetic_pair):
    _, policy = synthetic_pair
    fields = {**policy.to_mapping(), "accepted": True}
    with pytest.raises(RuntimeError, match="schema is not closed"):
        LocalMediaPreflightPolicy.from_mapping(fields)
