"""Explicit synthetic installed content; never calibrated/deployable authority."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace

from autocut_kernel.registry.installed_local_run import LocalRunResource
from autocut_kernel.vlm import GENERATION_RETRY_STRATEGY_VERSION, GenerationRetryPolicy
from autocut_kernel.vlm.parser_contract import vlm_parser_contract_sha256

from auto_cut_bot.pipeline.media_preflight.models import LocalMediaPreflightPolicy
from auto_cut_bot.pipeline.source_prep.command import identity_window_sampling_policy_sha256
from auto_cut_bot.pipeline.vlm import DoubaoVlmRequestPolicy
from auto_cut_bot.pipeline.vlm.prompt import vlm_prompt_template_sha256
from tests.authority.test_authority_profile_sources import (
    _narrative_mapping,
    _raw,
    _run_mapping,
    _shadow_mapping,
)
from tests.authority.test_installed_local_run import _decode, _rehash_chain, _resource_mapping
from tests.pipeline.runtime_profile_fixture import media_preflight_policy


def synthetic_installed_resource(
    policy: DoubaoVlmRequestPolicy | None = None,
    retry_policy: GenerationRetryPolicy | None = None,
) -> LocalRunResource:
    policy = policy or DoubaoVlmRequestPolicy(model_id="doubao-seed-2-1-pro-260628")
    retry_policy = retry_policy or GenerationRetryPolicy(GENERATION_RETRY_STRATEGY_VERSION, 3, (2, 8))
    narrative = _narrative_mapping()
    narrative["prompt"]["template_sha256"] = vlm_prompt_template_sha256()
    narrative["response_schema"]["schema_sha256"] = _digest(policy.response_schema_json)
    narrative["parser"]["contract_sha256"] = vlm_parser_contract_sha256()
    narrative["policies"].update({
        "request_parameters_sha256": _digest(policy.request_parameters_json),
        "parse_policy_sha256": policy.parse_policy.canonical_hash,
        "retry_policy_sha256": retry_policy.canonical_hash,
        "window_sampling_policy_sha256": identity_window_sampling_policy_sha256(),
    })
    shadow = _shadow_mapping(narrative)
    native = shadow["native_timed_speech"]
    detector_policy = media_preflight_policy(**_native_policy_fields(native, shadow["timing_policies"]))
    for producer in native["producers"]:
        producer["detector_sha256"] = detector_policy.timed_speech_detector_sha256(producer["producer_kind"])
    local_run = _run_mapping(narrative, shadow)
    wire = _resource_mapping()
    for chain in wire["current"], wire["predecessor"]:
        chain["narrative_raw_base64"] = base64.b64encode(_raw(narrative)).decode("ascii")
    wire["predecessor"]["profile_raw_base64"] = base64.b64encode(_raw(shadow)).decode("ascii")
    _rehash_chain(wire["predecessor"], "shadow_calibration_v1")
    local_run["predecessor_shadow_profile"].update({
        "registry_set_sha256": wire["predecessor"]["registry_set_sha256"],
        "authority_lock_sha256": wire["predecessor"]["authority_lock_sha256"],
    })
    wire["current"]["profile_raw_base64"] = base64.b64encode(_raw(local_run)).decode("ascii")
    _rehash_chain(wire["current"], "local_run_v1")
    return _decode(wire)


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _native_policy_fields(native, timing):
    fields = {
        "timed_speech_provider_id": native["provider_id"],
        "timed_speech_provider_version": native["provider_version"],
        "timed_speech_service_sha256": native["service_sha256"],
        "funasr_version": native["funasr_version"],
        "torch_version": native["torch_version"],
        "speech_device": native["device"],
        "word_timing_capability": native["word_timing_capability"],
        "timed_speech_policy_sha256": timing["timed_speech_policy_sha256"],
        "utterance_gap_milliseconds": timing["word_gap_ms"],
        "vad_merge_gap_milliseconds": timing["vad_merge_gap_ms"],
    }
    for producer in native["producers"]:
        for field in ("model_id", "model_revision", "model_sha256"):
            fields[f"{producer['producer_kind']}_{field}"] = producer[field]
    return fields


def synthetic_media_policy(resource: LocalRunResource) -> LocalMediaPreflightPolicy:
    """Match grammar fixtures only; this is not a measured deployment policy."""
    source = resource.local_run
    policy = media_preflight_policy(
        **_native_policy_fields(source.native_timed_speech.to_mapping(), source.timing_policies.to_mapping()),
        timed_speech_calibration_sha256=source.calibration.record_ref.content_hash,
    )
    calibrations = list(policy.calibrations)
    clock = source.source_clock_policy.time_base
    for index, producer in enumerate(source.native_timed_speech.producers, start=2):
        numerator = producer.timing_error_bound_tick * clock.numerator * 1_000_000
        microseconds, remainder = divmod(numerator, clock.denominator)
        assert not remainder, "synthetic source bounds must have exact microsecond representation"
        calibrations[index] = replace(
            calibrations[index],
            producer_kind=producer.producer_kind,
            producer_id=producer.producer_id,
            producer_version=producer.producer_version,
            generation_policy_sha256=producer.generation_policy_sha256,
            detector_sha256=producer.detector_sha256,
            calibration_policy_sha256=producer.calibration_policy_sha256,
            calibration_record_sha256=producer.producer_record_sha256,
            timing_error_bound_microseconds=microseconds,
        )
    return replace(policy, calibrations=tuple(calibrations))
