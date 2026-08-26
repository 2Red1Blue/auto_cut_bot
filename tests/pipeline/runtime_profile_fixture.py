from __future__ import annotations

from autocut_kernel.semantic_chain.candidate_catalog import CandidateCatalogPolicy
from autocut_kernel.semantic_chain.coverage_analysis import Stage1CoveragePolicy
from autocut_kernel.semantic_chain.dependency_projection import DependencyProjectionPolicy
from autocut_kernel.semantic_chain.editorial_command_policy import Stage3CommandPolicy
from autocut_kernel.semantic_chain.editorial_context_models import EditorialContextPolicy
from autocut_kernel.semantic_chain.editorial_draft import EditorialDraftPolicy
from autocut_kernel.semantic_chain.editorial_feasibility import EditorialFeasibilityPolicy
from autocut_kernel.semantic_chain.stage1_command_policy import (
    Stage1CommandPolicy,
    Stage1GenerationPolicy,
)
from autocut_kernel.semantic_chain.stage1_draft import Stage1DraftPolicy
from autocut_kernel.semantic_chain.story_design_command_policy import Stage2CommandPolicy
from autocut_kernel.semantic_chain.story_design_draft import StoryDesignDraftPolicy
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


def stage1_command_policy() -> Stage1CommandPolicy:
    """Explicit synthetic test policy, never an installed authority source."""
    return Stage1CommandPolicy(
        artifact_revision=1,
        generation=Stage1GenerationPolicy(
            provider_id="doubao-ark-text-responses-stream",
            model_id="synthetic-stage1-model",
            prompt_version="synthetic-stage1-prompt-v1",
            prompt_template="Build a draft from the supplied semantic observations.",
            adapter_strategy_version="doubao-ark-text-responses-stream-v1",
            max_output_tokens=1024,
            temperature="0.5",
        ),
        draft_policy=Stage1DraftPolicy(
            max_response_bytes=64_000,
            max_prompt_bytes=64_000,
            max_input_windows=4,
            max_input_objects=32,
            max_beats=4,
            max_obligations=4,
            max_story_threads=4,
            max_merge_proposals=4,
            max_references_per_item=8,
            max_text_characters=256,
            max_total_text_characters=2048,
        ),
        coverage_policy=Stage1CoveragePolicy("0.8", "strict_global"),
        dependency_policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
        retry_policy=GenerationRetryPolicy(GENERATION_RETRY_STRATEGY_VERSION, 3, (2, 8)),
    )


def stage2_command_policy() -> Stage2CommandPolicy:
    """Explicit synthetic Stage 2 policy, never an installed authority source."""
    from tests.semantic_chain.test_story_design_models import _job_policy, _story_policy

    return Stage2CommandPolicy(
        artifact_revision=1,
        generation=Stage1GenerationPolicy(
            provider_id="doubao-ark-text-responses-stream",
            model_id="synthetic-stage2-model",
            prompt_version="synthetic-stage2-prompt-v1",
            prompt_template="Build a portfolio from the admitted semantic inputs.",
            adapter_strategy_version="doubao-ark-text-responses-stream-v1",
            max_output_tokens=1024,
            temperature="0.5",
        ),
        max_prompt_bytes=64_000,
        draft_policy=StoryDesignDraftPolicy(
            max_response_bytes=64_000,
            max_json_depth=16,
            max_proposals=8,
            max_material_requirements_per_proposal=8,
            max_total_material_requirements=16,
            max_references_per_field=16,
            max_total_references=64,
            max_genre_tags=8,
            max_text_characters=512,
            max_total_text_characters=8_192,
        ),
        candidate_policy=CandidateCatalogPolicy(
            "candidate-catalog-v1", "0.5", ("reveal_strength",),
        ),
        job_policy=_job_policy(),
        story_policy=_story_policy(),
        retry_policy=GenerationRetryPolicy(GENERATION_RETRY_STRATEGY_VERSION, 2, (1,)),
    )


def stage3_command_policy() -> Stage3CommandPolicy:
    return Stage3CommandPolicy(
        1,
        Stage1GenerationPolicy("doubao-ark-text-responses-stream", "synthetic-stage3-model",
                               "synthetic-stage3-prompt-v1", "Build all frozen editorial Blueprints.",
                               "doubao-ark-text-responses-stream-v1", 1024, "0.5"),
        64_000,
        EditorialDraftPolicy("bytes", 64_000, 24, 4, 8, 16, 8, 32, 8, 64, 16, 256, 16, 32, 512, 8_192),
        EditorialContextPolicy("unpartitioned-batch-v1", "bytes", 64_000, 64_000, 100),
        EditorialFeasibilityPolicy("editorial-material-feasibility-v1", 1_000),
        GenerationRetryPolicy(GENERATION_RETRY_STRATEGY_VERSION, 2, (1,)),
        "unpartitioned-batch-v1",
    )


def execution_profile(
    *,
    model_id: str = "doubao-seed-2-1-pro-260628",
    media_policy: LocalMediaPreflightPolicy | None = None,
    stage1_policy: Stage1CommandPolicy | None = None,
    stage2_policy: Stage2CommandPolicy | None = None,
    stage3_policy: Stage3CommandPolicy | None = None,
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
        stage1_policy=stage1_command_policy() if stage1_policy is None else stage1_policy,
        stage2_policy=stage2_command_policy() if stage2_policy is None else stage2_policy,
        stage3_policy=stage3_command_policy() if stage3_policy is None else stage3_policy,
    )
