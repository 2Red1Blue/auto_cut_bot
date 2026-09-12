"""Provider-visible request construction for the rich observation contract.

This deliberately has no dependency on the legacy V23/V4 request factory.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from autocut_kernel.pipeline.observation_generation import (
    ObservationGenerationRequest,
    ObservationSourceBinding,
)
from autocut_kernel.store import Job
from autocut_kernel.store.models import canonical_recipe_scope
from autocut_kernel.vlm import GenerationRetryPolicy
from autocut_kernel.vlm.observation_contract import ObservationLimits, observation_response_schema
from autocut_kernel.vlm.observation_prompt import build_observation_prompt

from auto_cut_bot.pipeline.source_prep import PersistedPreparedSources

OBSERVATION_ARK_ADAPTER_STRATEGY_VERSION = "ark-files-responses-observation-v1"
OBSERVATION_PROMPT_VERSION = "vlm-observation-rich-v1"


class ObservationRuntimePolicy:
    def __init__(self, *, model_id: str, provider_id: str, limits: ObservationLimits,
                 max_output_tokens: int, video_fps: float, task: str,
                 audio_capability: str = "未知或未验证；不要描述听到的声音或对白") -> None:
        self.model_id, self.provider_id, self.limits = model_id, provider_id, limits
        self.max_output_tokens, self.video_fps = max_output_tokens, video_fps
        self.task, self.audio_capability = task, audio_capability
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")


def build_observation_request(*, source_bundle: PersistedPreparedSources, episode_index: int,
                              job: Job, idempotency_key: str, policy: ObservationRuntimePolicy,
                              retry_policy: GenerationRetryPolicy) -> ObservationGenerationRequest:
    """Bind a new observation command to exact committed SourcePrep media."""
    episode = source_bundle.prepared.episodes[episode_index]
    if episode.manifest not in episode.manifest_set.manifests:
        raise ValueError("prepared episode manifest is not in its committed set")
    prompt = build_observation_prompt(task=policy.task, audio_capability=policy.audio_capability)
    schema = observation_response_schema()
    return ObservationGenerationRequest(
        job=job, idempotency_key=idempotency_key, artifact_scope=canonical_recipe_scope(job),
        artifact_revision=1, source_binding=ObservationSourceBinding(source_bundle.receipt_id,
        source_bundle.artifact_set_id, source_bundle.command_slot_id, source_bundle.artifact_reference.content_hash,
        source_bundle.canonical_hash, episode_index), manifest=episode.manifest, manifest_set=episode.manifest_set,
        proxy_blob=episode.proxy_blob, prompt_template=prompt, prompt_version=OBSERVATION_PROMPT_VERSION,
        response_schema_json=json.dumps(schema, ensure_ascii=False, sort_keys=True),
        request_parameters_json=json.dumps({"adapter_strategy_version": OBSERVATION_ARK_ADAPTER_STRATEGY_VERSION,
        "max_output_tokens": policy.max_output_tokens, "temperature": 0, "video_fps": policy.video_fps}, sort_keys=True),
        model_id=policy.model_id, provider_id=policy.provider_id, limits=policy.limits,
        retry_policy=retry_policy)


def build_observation_ark_body(
    *,
    model_id: str,
    file_id: str,
    prompt: str,
    response_schema: Mapping[str, object],
    max_output_tokens: int,
    temperature: int | float = 0,
) -> dict[str, object]:
    """Construct the entire Ark wire body from public observation inputs only."""
    if not all(type(value) is str and value.strip() for value in (model_id, file_id, prompt)):  # noqa: E721
        raise ValueError("model_id, file_id, and prompt must be non-empty text")
    if type(max_output_tokens) is not int or max_output_tokens < 1:  # noqa: E721
        raise ValueError("max_output_tokens must be positive")
    if isinstance(temperature, bool) or type(temperature) not in (int, float):
        raise ValueError("temperature must be numeric")
    return {
        "model": model_id,
        "input": [{"role": "user", "content": [
            {"type": "input_video", "file_id": file_id},
            {"type": "input_text", "text": prompt},
        ]}],
        "text": {"format": {"type": "json_schema", "name": "vlm_observation_report", "schema": dict(response_schema), "strict": True}},
        "max_output_tokens": max_output_tokens,
        "temperature": temperature,
        "thinking": {"type": "disabled"},
        "stream": True,
        "store": True,
    }


__all__ = ["OBSERVATION_ARK_ADAPTER_STRATEGY_VERSION", "build_observation_ark_body"]
