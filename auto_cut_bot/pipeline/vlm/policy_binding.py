"""Compare executed VLM policies with the controlled installed narrative source.

This checks only the VLM/source-window portion of the current HTTP plan. It does
not certify Stage 1 coverage/dependency/conflict policies or grant publication.
The caller must obtain the narrative from the fixed installed resource loader.
"""

from __future__ import annotations

import hashlib

from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.registry.authority_profiles import Stage1NarrativeProfileSource
from autocut_kernel.vlm import GenerationRetryPolicy
from autocut_kernel.vlm.parser_contract import vlm_parser_contract_sha256

from auto_cut_bot.pipeline.source_prep.command import (
    PersistedPreparedSources,
    identity_window_sample_indices,
    identity_window_sampling_policy,
    identity_window_sampling_policy_sha256,
)

from .prompt import vlm_prompt_template_sha256
from .request_factory import DoubaoVlmRequestPolicy


class InstalledVlmPolicyError(ValueError):
    """A running or resumed VLM policy differs from the installed release."""


def validate_installed_vlm_policy(
    narrative: Stage1NarrativeProfileSource,
    policy: DoubaoVlmRequestPolicy,
    retry_policy: GenerationRetryPolicy,
) -> None:
    """Check actual policy bytes, including persisted rather than default values."""
    if (type(narrative) is not Stage1NarrativeProfileSource  # noqa: E721
            or type(policy) is not DoubaoVlmRequestPolicy  # noqa: E721
            or type(retry_policy) is not GenerationRetryPolicy):  # noqa: E721
        raise InstalledVlmPolicyError("installed VLM binding requires exact typed policies")
    expected = narrative.reference.to_mapping()
    actual = {
        "provider_id": policy.provider_id,
        "model_id": policy.model_id,
        "adapter_strategy_version": policy.adapter_strategy_version,
        "prompt_version": policy.prompt_version,
        "parser_strategy_version": policy.parser_strategy_version,
        "prompt_template_sha256": vlm_prompt_template_sha256(policy.prompt_version),
        "response_schema_sha256": _text_sha256(policy.response_schema_json),
        "parser_contract_sha256": vlm_parser_contract_sha256(),
        "request_parameters_sha256": _text_sha256(policy.request_parameters_json),
        "parse_policy_sha256": policy.parse_policy.canonical_hash,
        "retry_policy_sha256": retry_policy.canonical_hash,
        "window_sampling_policy_sha256": identity_window_sampling_policy_sha256(),
    }
    for field, value in actual.items():
        if value != expected[field]:
            # Only a fixed field name is exposed, never source/config contents.
            raise InstalledVlmPolicyError(f"installed VLM policy mismatch: {field}")


def validate_installed_source_sampling(source_bundle: PersistedPreparedSources) -> None:
    """Recheck dynamic sampling and actual frame anchors of resumed input."""
    if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
        raise InstalledVlmPolicyError("installed sampling requires committed sources")
    for episode in source_bundle.prepared.episodes:
        manifest = episode.manifest
        ticks = manifest.frame_pts_index_set.pts_index.ticks
        selected = identity_window_sample_indices(len(ticks))
        expected = canonical_sha256({
            **identity_window_sampling_policy(),
            "selected_indices": list(selected),
        })
        anchors = tuple((sample.source_pts, sample.proxy_pts) for sample in manifest.frame_samples)
        if (manifest.window_sampling_policy_sha256 != expected
                or anchors != tuple((ticks[index], ticks[index]) for index in selected)):
            raise InstalledVlmPolicyError("committed source sampling differs from installed policy")


def _text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
