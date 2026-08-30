"""The bounded prompt is a new frozen profile, not a relabelled old request."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.models import PipelineExecutionProfile
from auto_cut_bot.pipeline.vlm.request_factory import DoubaoVlmRequestPolicy
from tests.pipeline.test_vlm_video_profile import _video_mapping, _video_policy

_OLD_VIDEO_PROFILE_HASH = "sha256:32e69fa5a80d9ba589dcbdab13ad1ddf0e3ff4c895e86e7d21e29e55d7f6d49f"
_FROZEN_V4_PARSER_HASH = "sha256:9b285e4344ab1838573eae26f041b9553308510413fd8cca3722072ec9248630"


def _bounded_policy(mode: str = "disabled") -> DoubaoVlmRequestPolicy:
    from auto_cut_bot.pipeline.vlm.bounded_video_prompt import (
        VLM_BOUNDED_VIDEO_PROMPT_VERSION,
        vlm_bounded_video_response_schema_json,
    )

    return replace(
        _video_policy(mode), prompt_version=VLM_BOUNDED_VIDEO_PROMPT_VERSION,
        response_schema_json=vlm_bounded_video_response_schema_json(),
    )


def _bounded_profile(mode: str = "disabled") -> PipelineExecutionProfile:
    original = PipelineExecutionProfile.from_mapping(_video_mapping(mode))
    return PipelineExecutionProfile.from_semantic_policies(
        _bounded_policy(mode), retry_policy=original.to_generation_retry_policy(),
    )


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


@pytest.mark.parametrize("mode", ["enabled", "disabled", "auto"])
def test_bounded_profile_roundtrips_canonical_bytes_without_defaults(mode: str) -> None:
    profile = _bounded_profile(mode)
    encoded = profile.canonical_json.encode("utf-8")
    decoded: dict[str, Any] = json.loads(encoded)
    restored = PipelineExecutionProfile.from_mapping(decoded)

    assert restored == profile
    assert restored.canonical_json.encode("utf-8") == encoded
    assert restored.canonical_hash == "sha256:" + hashlib.sha256(encoded).hexdigest()
    assert restored.to_doubao_policy() == _bounded_policy(mode)
    assert restored.is_semantic_only
    assert restored.schema_version == "pipeline-execution-profile-v10"
    assert restored.prompt_version == "vlm-semantic-pack-v6-bounded-references"
    assert restored.kernel_parser_strategy_version == "strict-semantic-pack-v4"
    assert restored.parser_contract_sha256 == _FROZEN_V4_PARSER_HASH
    assert decoded["response_schema"]["properties"]["schema_version"]["const"] == 4


def test_bounded_prompt_changes_profile_identity_without_rewriting_old_video_profile() -> None:
    old = PipelineExecutionProfile.from_mapping(_video_mapping())
    before = old.canonical_json
    bounded = _bounded_profile()
    old_mapping, bounded_mapping = old.to_mapping(), bounded.to_mapping()

    assert old.canonical_hash == _OLD_VIDEO_PROFILE_HASH
    assert bounded.canonical_hash != old.canonical_hash
    assert set(old_mapping) == set(bounded_mapping)
    assert {key for key in old_mapping if old_mapping[key] != bounded_mapping[key]} == {
        "prompt_version", "response_schema",
    }
    assert bounded.parser_contract_sha256 == old.parser_contract_sha256 == _FROZEN_V4_PARSER_HASH
    assert old.canonical_json == before
    restored = PipelineExecutionProfile.from_mapping(json.loads(before))
    assert restored.canonical_hash == _OLD_VIDEO_PROFILE_HASH
    assert restored.to_doubao_policy() == _video_policy()


def test_bounded_profile_keeps_all_explicit_thinking_modes_distinct() -> None:
    profiles = [_bounded_profile(mode) for mode in ("enabled", "disabled", "auto")]
    assert len({profile.canonical_hash for profile in profiles}) == 3
    assert {profile.parser_contract_sha256 for profile in profiles} == {_FROZEN_V4_PARSER_HASH}


@pytest.mark.parametrize("mutation", [
    "old_prompt", "old_schema", "old_parser", "old_stage", "old_adapter", "wrong_provider",
    "missing_parser_hash", "wrong_parser_hash", "unknown_field", "physical_profile",
])
def test_bounded_profile_rejects_mixed_or_unbound_contract(mutation: str) -> None:
    mapping: dict[str, Any] = _bounded_profile().to_mapping()
    old = _video_mapping()
    if mutation == "old_prompt":
        mapping["prompt_version"] = old["prompt_version"]
    elif mutation == "old_schema":
        mapping["response_schema"] = old["response_schema"]
    elif mutation == "old_parser":
        mapping["kernel_parser_strategy_version"] = "strict-semantic-pack-v3"
    elif mutation == "old_stage":
        mapping["vlm_stage_strategy_version"] = "doubao-generate-vlm-semantic-pack-v3-probe-then-parallel-10-v3"
    elif mutation == "old_adapter":
        mapping["adapter_strategy_version"] = "doubao-ark-files-responses-stream-v4"
        mapping["request_parameters"]["adapter_strategy_version"] = mapping["adapter_strategy_version"]
        mapping["request_parameters"].pop("thinking_type")
    elif mutation == "wrong_provider":
        mapping["provider_id"] = "another-provider"
    elif mutation == "missing_parser_hash":
        mapping.pop("parser_contract_sha256")
    elif mutation == "wrong_parser_hash":
        mapping["parser_contract_sha256"] = "sha256:" + "a" * 64
    elif mutation == "physical_profile":
        mapping["schema_version"] = "pipeline-execution-profile-v9"
    else:
        mapping["references_verified"] = True
    with pytest.raises(PipelineRunValidationError):
        PipelineExecutionProfile.from_mapping(mapping)


def test_bounded_profile_mapping_cannot_mutate_frozen_schema_or_hash() -> None:
    profile = _bounded_profile()
    before = profile.canonical_json
    mapping: dict[str, Any] = profile.to_mapping()
    mapping["response_schema"]["properties"].clear()
    mapping["prompt_version"] = "caller-overwrite"
    assert profile.canonical_json == before
    assert _canonical(profile.to_mapping()) == before
    with pytest.raises(FrozenInstanceError):
        profile.prompt_version = "caller-overwrite"  # type: ignore[misc]
