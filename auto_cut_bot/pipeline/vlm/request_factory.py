"""Pure, fail-closed construction of Doubao Kernel VLM requests.

This module binds an already committed source-preparation episode to one
``GenerateVlmEvidenceRequest``.  It deliberately performs no Store reads,
provider calls, runtime scheduling, or media probing.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import cast

from autocut_kernel.pipeline import (
    VLM_PARSER_STRATEGY_VERSION,
    GenerateVlmEvidenceRequest,
)
from autocut_kernel.store import BlobRef, Job
from autocut_kernel.store.models import canonical_recipe_scope
from autocut_kernel.vlm import GenerationRetryPolicy, VlmParsePolicy

from auto_cut_bot.pipeline.source_prep.command import (
    PersistedPreparedSources,
    PreparedSourceEpisode,
)

from .doubao_ark_provider import (
    DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_PROVIDER_ID,
    DOUBAO_ARK_SUPPORTED_ADAPTER_STRATEGY_VERSIONS,
)
from .prompt import (
    VLM_PROMPT_VERSION,
    VLM_RESPONSE_SCHEMA,
    build_vlm_prompt,
    resolve_vlm_prompt_template,
    vlm_response_schema_json,
)

DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION = "doubao-generate-vlm-semantic-pack-v3-request-v1"
DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION = (
    "doubao-generate-vlm-semantic-pack-v3-parallel-10-v2"
)
DOUBAO_VLM_PROBE_THEN_PARALLEL_STAGE_STRATEGY_VERSION = (
    "doubao-generate-vlm-semantic-pack-v3-probe-then-parallel-10-v3"
)
DOUBAO_VLM_STAGE_STRATEGY_VERSION = DOUBAO_VLM_PROBE_THEN_PARALLEL_STAGE_STRATEGY_VERSION
DOUBAO_VLM_REQUEST_FACTORY_STRATEGY_VERSION = DOUBAO_VLM_STAGE_STRATEGY_VERSION


def _default_parse_policy() -> VlmParsePolicy:
    return VlmParsePolicy(
        max_response_bytes=2 * 1024 * 1024,
        max_entities=24,
        max_facts=48,
        max_events=24,
        max_candidate_hypotheses=8,
        max_temporal_segments=8,
        max_measurements=48,
        max_text_characters=512,
        max_total_text_characters=64 * 1024,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _strict_json_object(value: str, field_name: str) -> dict[str, object]:
    if type(value) is not str or not value:  # noqa: E721
        raise ValueError(f"{field_name} must be canonical JSON object text")
    try:
        parsed = json.loads(
            value,
            parse_constant=_reject_nonfinite_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be strict finite JSON") from error
    if type(parsed) is not dict:  # noqa: E721
        raise ValueError(f"{field_name} must contain a JSON object")
    return cast(dict[str, object], parsed)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("policy must contain only finite JSON values") from error


def _closed_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 256:  # noqa: E721
        raise ValueError(f"{field_name} must be non-empty text of at most 256 characters")
    if value != value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(f"{field_name} must be canonical text without whitespace or controls")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field_name} must contain valid Unicode") from error
    return value


def _finite_number(
    value: object,
    field_name: str,
    *,
    minimum: float,
    maximum: float,
) -> int | float:
    if isinstance(value, bool) or type(value) not in (int, float):
        raise TypeError(f"{field_name} must be an explicit JSON number")
    number = cast(int | float, value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{field_name} must be finite and between {minimum:g} and {maximum:g}")
    return number


@dataclass(frozen=True, slots=True)
class DoubaoVlmRequestPolicy:
    """Closed execution profile accepted by the Doubao request factory."""

    model_id: str
    provider_id: str = DOUBAO_ARK_PROVIDER_ID
    adapter_strategy_version: str = DOUBAO_ARK_ADAPTER_STRATEGY_VERSION
    prompt_version: str = VLM_PROMPT_VERSION
    response_schema_json: str = field(default_factory=vlm_response_schema_json)
    video_fps: float = 1.0
    # Explicit budget frozen into each request, not an automatic repair knob.
    # A length terminal requires a deliberate new policy/run, never a silent
    # increase while replaying the same provider command.
    max_output_tokens: int = 32_768
    temperature: int | float = 0
    parse_policy: VlmParsePolicy = field(default_factory=_default_parse_policy)
    parser_strategy_version: str = VLM_PARSER_STRATEGY_VERSION
    stage_strategy_version: str = DOUBAO_VLM_STAGE_STRATEGY_VERSION
    thinking_type: str | None = None

    def __post_init__(self) -> None:
        model_id = _closed_text(self.model_id, "model_id")
        if "qwen" in model_id.casefold():
            raise ValueError("Qwen models are forbidden by the Doubao-only policy")
        if self.provider_id != DOUBAO_ARK_PROVIDER_ID:
            raise ValueError("provider_id must be the registered Doubao provider")
        if self.adapter_strategy_version not in DOUBAO_ARK_SUPPORTED_ADAPTER_STRATEGY_VERSIONS:
            raise ValueError("adapter strategy must be a registered Doubao version")
        if self.adapter_strategy_version == DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION:
            if type(self.thinking_type) is not str or self.thinking_type not in {"enabled", "disabled", "auto"}:  # noqa: E721
                raise ValueError("v5 requires an explicit enabled, disabled, or auto thinking_type")
        elif self.thinking_type is not None:
            raise ValueError("legacy Ark adapters do not accept thinking_type")
        resolve_vlm_prompt_template(self.prompt_version)
        schema = _strict_json_object(self.response_schema_json, "response schema JSON")
        if schema != VLM_RESPONSE_SCHEMA or self.response_schema_json != vlm_response_schema_json():
            raise ValueError("response schema JSON must be the exact registered canonical schema")
        fps = _finite_number(self.video_fps, "video_fps", minimum=0.1, maximum=10)
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 32_768:  # noqa: E721
            raise ValueError("max_output_tokens must be an integer between 1 and 32768")
        temperature = _finite_number(self.temperature, "temperature", minimum=0, maximum=2)
        if type(self.parse_policy) is not VlmParsePolicy:  # noqa: E721
            raise TypeError("parse_policy must be an exact VlmParsePolicy")
        if self.parser_strategy_version != VLM_PARSER_STRATEGY_VERSION:
            raise ValueError("parser strategy must be the registered Kernel version")
        if self.stage_strategy_version not in {
            DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION,
            DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION,
            DOUBAO_VLM_STAGE_STRATEGY_VERSION,
        }:
            raise ValueError("stage strategy must be a registered Doubao request version")
        supported_combinations = {
            (
                DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION,
                DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION,
            ),
            (
                DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION,
                DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION,
            ),
            (DOUBAO_ARK_ADAPTER_STRATEGY_VERSION, DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION),
            (DOUBAO_ARK_ADAPTER_STRATEGY_VERSION, DOUBAO_VLM_STAGE_STRATEGY_VERSION),
            (DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION, DOUBAO_VLM_STAGE_STRATEGY_VERSION),
        }
        if (self.adapter_strategy_version, self.stage_strategy_version) not in supported_combinations:
            raise ValueError(
                "Ark adapter and VLM stage strategy are not a registered replay combination"
            )
        object.__setattr__(self, "video_fps", float(fps))
        object.__setattr__(self, "temperature", float(temperature))

    @property
    def request_parameters(self) -> dict[str, object]:
        """Return a fresh closed mapping accepted by ``DoubaoArkVlmProvider``."""

        result: dict[str, object] = {
            "adapter_strategy_version": self.adapter_strategy_version,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "video_fps": self.video_fps,
        }
        if self.adapter_strategy_version == DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION:
            result["thinking_type"] = self.thinking_type
        return result

    @property
    def request_parameters_json(self) -> str:
        return _canonical_json(self.request_parameters)

    def to_mapping(self) -> dict[str, object]:
        return {
            "adapter_strategy_version": self.adapter_strategy_version,
            "model_id": self.model_id,
            "parse_policy": self.parse_policy.to_mapping(),
            "parser_strategy_version": self.parser_strategy_version,
            "prompt_version": self.prompt_version,
            "provider_id": self.provider_id,
            "request_parameters": self.request_parameters,
            "response_schema": _strict_json_object(
                self.response_schema_json,
                "response schema JSON",
            ),
            "stage_strategy_version": self.stage_strategy_version,
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        encoded = self.canonical_json.encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _blob_matches_manifest(blob: BlobRef, prepared_episode: PreparedSourceEpisode) -> bool:
    manifest_blob = prepared_episode.manifest.proxy_blob_ref
    return (
        str(blob.object_id) == manifest_blob.object_id
        and blob.content_hash == manifest_blob.content_hash
        and blob.byte_length == manifest_blob.byte_length
        and blob.media_type == manifest_blob.media_type
    )


def build_doubao_vlm_request(
    *,
    source_bundle: PersistedPreparedSources,
    episode_index: int,
    job: Job,
    artifact_revision: int,
    idempotency_key: str,
    policy: DoubaoVlmRequestPolicy,
    retry_policy: GenerationRetryPolicy,
) -> GenerateVlmEvidenceRequest:
    """Build one request from a provenance-bearing committed source episode."""

    if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
        raise TypeError("source_bundle must be an exact PersistedPreparedSources")
    if type(job) is not Job:  # noqa: E721
        raise TypeError("job must be an exact Job")
    if job != source_bundle.source_job:
        raise ValueError("VLM Job must match the persisted source Job")
    if type(policy) is not DoubaoVlmRequestPolicy:  # noqa: E721
        raise TypeError("policy must be an exact DoubaoVlmRequestPolicy")
    if type(retry_policy) is not GenerationRetryPolicy:  # noqa: E721
        raise TypeError("retry_policy must be an exact GenerationRetryPolicy")
    if type(episode_index) is not int or not 0 <= episode_index < len(  # noqa: E721
        source_bundle.prepared.episodes
    ):
        raise ValueError("episode_index must select an exact persisted source episode")
    prepared_episode = source_bundle.prepared.episodes[episode_index]
    if prepared_episode.manifest not in prepared_episode.manifest_set.manifests:
        raise ValueError("prepared_episode manifest_set does not bind the exact manifest")
    if not _blob_matches_manifest(prepared_episode.proxy_blob, prepared_episode):
        raise ValueError("prepared_episode proxy_blob does not bind the exact manifest BlobRef")
    return GenerateVlmEvidenceRequest(
        job=job,
        idempotency_key=idempotency_key,
        artifact_scope=canonical_recipe_scope(job),
        artifact_revision=artifact_revision,
        manifest=prepared_episode.manifest,
        manifest_set=prepared_episode.manifest_set,
        proxy_blob=prepared_episode.proxy_blob,
        prompt_template=build_vlm_prompt(prepared_episode.manifest, prompt_version=policy.prompt_version),
        prompt_version=policy.prompt_version,
        response_schema_json=policy.response_schema_json,
        request_parameters_json=policy.request_parameters_json,
        model_id=policy.model_id,
        provider_id=policy.provider_id,
        parse_policy=policy.parse_policy,
        retry_policy=retry_policy,
        parser_strategy_version=policy.parser_strategy_version,
        source_provenance_sha256=source_bundle.canonical_hash,
    )


@dataclass(frozen=True, slots=True)
class DoubaoVlmRequestFactory:
    """Immutable convenience facade around :func:`build_doubao_vlm_request`."""

    policy: DoubaoVlmRequestPolicy
    retry_policy: GenerationRetryPolicy

    def __post_init__(self) -> None:
        if type(self.policy) is not DoubaoVlmRequestPolicy:  # noqa: E721
            raise TypeError("policy must be an exact DoubaoVlmRequestPolicy")
        if type(self.retry_policy) is not GenerationRetryPolicy:  # noqa: E721
            raise TypeError("retry_policy must be an exact GenerationRetryPolicy")

    def build(
        self,
        *,
        source_bundle: PersistedPreparedSources,
        episode_index: int,
        job: Job,
        artifact_revision: int,
        idempotency_key: str,
    ) -> GenerateVlmEvidenceRequest:
        return build_doubao_vlm_request(
            source_bundle=source_bundle,
            episode_index=episode_index,
            job=job,
            artifact_revision=artifact_revision,
            idempotency_key=idempotency_key,
            policy=self.policy,
            retry_policy=self.retry_policy,
        )


__all__ = [
    "DOUBAO_VLM_REQUEST_FACTORY_STRATEGY_VERSION",
    "DOUBAO_VLM_STAGE_STRATEGY_VERSION",
    "DoubaoVlmRequestFactory",
    "DoubaoVlmRequestPolicy",
    "build_doubao_vlm_request",
]
