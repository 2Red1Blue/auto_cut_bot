"""Compare operational ASR/VAD configuration with fixed installed source content.

This check performs no inference or Store reads and grants no acceptance. The
installed loader and accepted-anchor resolver remain required. Visual/physical
calibrations, endpoint placement and operational budgets have no corresponding
source fields here and are not certified by this comparison.
"""

from __future__ import annotations

from typing import Literal

from autocut_kernel.registry.installed_local_run import LocalRunResource

from .models import LocalMediaPreflightPolicy


class InstalledMediaPolicyError(ValueError):
    """Operational timed-speech policy differs from the installed source."""


def validate_installed_media_policy(
    resource: LocalRunResource, policy: LocalMediaPreflightPolicy,
) -> None:
    """Reject incompatible configuration before paid VLM or native dispatch."""
    if type(resource) is not LocalRunResource or type(policy) is not LocalMediaPreflightPolicy:  # noqa: E721
        raise InstalledMediaPolicyError("requires exact installed content and media policy")
    source = resource.local_run
    native = source.native_timed_speech
    expected_native = native.to_mapping()
    configured = policy.to_mapping()
    for policy_field, native_field in (
        ("timed_speech_provider_id", "provider_id"),
        ("timed_speech_provider_version", "provider_version"),
        ("timed_speech_service_sha256", "service_sha256"),
        ("funasr_version", "funasr_version"),
        ("torch_version", "torch_version"),
        ("speech_device", "device"),
        ("word_timing_capability", "word_timing_capability"),
    ):
        if configured[policy_field] != expected_native[native_field]:
            raise InstalledMediaPolicyError("media service identity differs from installed source")
    if (
        policy.timed_speech_policy_sha256 != source.timing_policies.timed_speech_policy_sha256
        or policy.utterance_gap_milliseconds != source.timing_policies.word_gap_ms
        or policy.vad_merge_gap_milliseconds != source.timing_policies.vad_merge_gap_ms
        # The normal service's profile calibration names the aggregate, not a child.
        or policy.timed_speech_calibration_sha256 != source.calibration.record_ref.content_hash
    ):
        raise InstalledMediaPolicyError("media timing policy or aggregate calibration differs from installed source")
    kinds: tuple[Literal["asr", "vad"], ...] = ("asr", "vad")
    for kind, producer, child_hash, bound in zip(
        kinds, native.producers,
        (source.calibration.asr_producer_record_sha256, source.calibration.vad_producer_record_sha256),
        (source.calibration.asr_timing_error_bound_tick, source.calibration.vad_timing_error_bound_tick),
        strict=True,
    ):
        calibration = policy.calibration(kind)
        microseconds = calibration.timing_error_bound_microseconds
        clock = source.source_clock_policy.time_base
        # Exact rational equality is stricter than the request owner's ceil:
        # an integral value converts unchanged; rounding cannot replace a bound.
        if (
            type(bound) is not int or bound <= 0  # noqa: E721
            or type(microseconds) is not int or microseconds <= 0  # noqa: E721
            or microseconds * clock.denominator != bound * clock.numerator * 1_000_000
            or producer.producer_record_sha256 != child_hash
            or producer.timing_error_bound_tick != bound
        ):
            raise InstalledMediaPolicyError("media calibration bound is not an exact positive installed bound")
        actual = {
            **calibration.to_mapping(),
            "timing_error_bound_tick": bound,
            "model_id": configured[f"{kind}_model_id"],
            "model_revision": configured[f"{kind}_model_revision"],
            "model_sha256": configured[f"{kind}_model_sha256"],
            "service_sha256": policy.timed_speech_service_sha256,
            "inference_kind": "sensevoice-word-timestamp" if kind == "asr" else "fsmn-vad-direct",
        }
        del actual["timing_error_bound_microseconds"]
        expected = {
            **producer.common_mapping(),
            "calibration_record_sha256": child_hash,
            "timing_error_bound_tick": bound,
        }
        if actual != expected or policy.timed_speech_detector_sha256(kind) != producer.detector_sha256:
            raise InstalledMediaPolicyError("media producer identity differs from installed source")
