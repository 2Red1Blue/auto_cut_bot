from __future__ import annotations

from autocut_kernel.store.models import MaterializationLimits
from autocut_kernel.vlm import GENERATION_RETRY_STRATEGY_VERSION, GenerationRetryPolicy

from auto_cut_bot.pipeline.media_preflight import (
    LocalMediaPreflightPolicy,
    ProducerCalibrationIdentity,
)
from auto_cut_bot.pipeline.runtime import PipelineExecutionProfile
from auto_cut_bot.pipeline.vlm.request_factory import DoubaoVlmRequestPolicy


def media_preflight_policy(**changes: object) -> LocalMediaPreflightPolicy:
    digest = "sha256:" + "1" * 64
    calibration_kinds = (
        "frame",
        "audio",
        "asr",
        "vad",
        "shot",
        "scene",
        "visual",
        "subtitle",
    )
    values: dict[str, object] = {
        "policy_id": "fixture-media-policy",
        "policy_version": "fixture-media-policy-v1",
        "timed_speech_endpoint_url": "http://127.0.0.1:10095/v1/timed-speech-evidence",
        "timed_speech_provider_id": "funasr-http",
        "timed_speech_provider_version": "funasr-http-v1",
        "timed_speech_service_sha256": digest,
        "funasr_version": "1.2.7",
        "torch_version": "2.8.0",
        "speech_device": "cpu",
        "word_timing_capability": "required",
        "asr_model_id": "SenseVoiceSmall",
        "asr_model_revision": "v1.0.0",
        "asr_model_sha256": digest,
        "vad_model_id": "fsmn-vad",
        "vad_model_revision": "v1.0.0",
        "vad_model_sha256": digest,
        "timed_speech_policy_sha256": digest,
        "timed_speech_calibration_sha256": digest,
        "initial_left_expansion_milliseconds": 250,
        "initial_right_expansion_milliseconds": 250,
        "expansion_step_milliseconds": 100,
        "max_expansion_count": 3,
        "boundary_touch_margin_milliseconds": 40,
        "analysis_fps_numerator": 2,
        "analysis_fps_denominator": 1,
        "analysis_width": 32,
        "analysis_height": 18,
        "max_analysis_frames": 10,
        "max_stdout_bytes": 32 * 18 * 10,
        "max_stderr_bytes": 4096,
        "probe_timeout_seconds": 5,
        "analysis_timeout_seconds": 5,
        "timed_speech_timeout_seconds": 30,
        "timed_speech_max_response_bytes": 1_048_576,
        "utterance_gap_milliseconds": 180,
        "vad_merge_gap_milliseconds": 120,
        "black_luma_max": 10,
        "white_luma_min": 245,
        "frozen_change_ppm_max": 10_000,
        "transition_change_ppm_min": 500_000,
        "shot_change_ppm_min": 100_000,
        "scene_change_ppm_min": 200_000,
        "subtitle_edge_delta_min": 24,
        "subtitle_edge_fraction_ppm_min": 25_000,
        "subtitle_min_consecutive_samples": 2,
        "calibrations": tuple(
            ProducerCalibrationIdentity(
                producer_kind=kind,
                producer_id=f"fixture-{kind}",
                producer_version="v1",
                generation_policy_sha256=digest,
                detector_sha256=digest,
                calibration_policy_sha256=digest,
                calibration_record_sha256=digest,
                timing_error_bound_microseconds=1_000,
            )
            for kind in calibration_kinds
        ),
    }
    values.update(changes)
    return LocalMediaPreflightPolicy.from_calibrated_values(**values)


def execution_profile(
    *,
    model_id: str = "doubao-seed-2-1-pro-260628",
    media_policy: LocalMediaPreflightPolicy | None = None,
) -> PipelineExecutionProfile:
    return PipelineExecutionProfile.from_policies(
        DoubaoVlmRequestPolicy(model_id=model_id),
        media_policy or media_preflight_policy(),
        retry_policy=GenerationRetryPolicy(
            GENERATION_RETRY_STRATEGY_VERSION,
            3,
            (2, 8),
        ),
        materialization_limits=MaterializationLimits(
            max_source_bytes=8 * 1024 * 1024,
            timed_speech_max_request_bytes=8 * 1024 * 1024,
            copy_chunk_bytes=64 * 1024,
            staging_quota_bytes=16 * 1024 * 1024,
        ),
    )
