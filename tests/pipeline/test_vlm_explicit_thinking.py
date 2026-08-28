"""Thinking is an explicit versioned request fact, never a transport default."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest
from autocut_kernel.vlm import ProviderCompleted, ProviderDispatchRequest, ProviderFailed

from auto_cut_bot.pipeline.runtime.models import PipelineExecutionProfile
from auto_cut_bot.pipeline.runtime.semantic_authority import load_installed_semantic_run_authority
from auto_cut_bot.pipeline.vlm.doubao_ark_provider import (
    DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION,
    DoubaoArkVlmProvider,
)
from auto_cut_bot.pipeline.vlm.request_factory import (
    DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION,
    build_doubao_vlm_request,
)
from auto_cut_bot.pipeline.vlm.reuse import derive_vlm_reuse_identity
from tests.pipeline import test_doubao_ark_provider as provider_fixture
from tests.pipeline import test_doubao_vlm_request_factory as factory_fixture
from tests.vlm import test_reuse_identity as reuse_fixture

_V5 = DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION
_MODES = ("enabled", "disabled", "auto")
_INVALID_MODES = (None, "", "Disabled", " disabled", "unknown", True, 1, {}, [])


def _parameters(mode: str = "disabled") -> dict[str, object]:
    return factory_fixture._policy(adapter_strategy_version=_V5, thinking_type=mode).request_parameters


def _dispatch(parameters: dict[str, object]) -> ProviderDispatchRequest:
    original = provider_fixture._dispatch()
    payload = cast(dict[str, object], json.loads(original.request_payload))
    payload["request_parameters"] = parameters
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return replace(
        original, request_payload=raw,
        request_payload_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
    )


def test_legacy_policy_and_factory_payload_hash_remain_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory_fixture, "uuid4", lambda: UUID("00000000-0000-0000-0000-000000000001"))
    policy = factory_fixture._policy()
    assert policy.adapter_strategy_version == "doubao-ark-files-responses-stream-v4"
    assert policy.thinking_type is None
    assert policy.request_parameters_json == (
        '{"adapter_strategy_version":"doubao-ark-files-responses-stream-v4",'
        '"max_output_tokens":32768,"temperature":0.0,"video_fps":1.0}'
    )
    assert "thinking_type" not in policy.to_mapping()
    bundle = factory_fixture._source_bundle()
    request = build_doubao_vlm_request(
        source_bundle=bundle, episode_index=0, job=bundle.source_job,
        artifact_revision=1, idempotency_key="compact-regression", policy=policy,
        retry_policy=factory_fixture._retry_policy(),
    )
    assert request.request_hash == "sha256:0e9bcd9c14ef0ac9f7507d959e1419f8fc7c6594be0da4496ae2c88998c8f665"
    assert request.request_identity.request_payload_sha256 == "sha256:e1d6e7ef12e81a40d0242ab57521852fd203fc0a8ac69462bf23ec976ba2010a"


@pytest.mark.parametrize("mode", _MODES)
def test_v5_provider_body_adds_only_explicit_thinking_and_shares_v4_media_cache(mode: str) -> None:
    factory = provider_fixture.FakeClientFactory(provider_fixture._completed_stream())
    cache = provider_fixture.MemoryFileCache()
    provider = DoubaoArkVlmProvider(provider_fixture._config(), file_cache=cache, client_factory=factory)
    legacy_parameters = factory_fixture._policy().request_parameters
    assert isinstance(provider.dispatch(_dispatch(legacy_parameters)), ProviderCompleted)
    assert isinstance(provider.dispatch(_dispatch(_parameters(mode))), ProviderCompleted)
    legacy_body, explicit_body = factory.responses.create_calls
    assert "thinking" not in legacy_body
    assert explicit_body == {**legacy_body, "thinking": {"type": mode}}
    assert explicit_body["max_output_tokens"] == 32768
    assert explicit_body["text"] == {
        "format": {
            "type": "json_schema", "name": "vlm_semantic_pack_v3",
            "strict": True, "schema": {"type": "object"},
        },
    }
    assert len(factory.files.create_calls) == 1
    assert cache.claims[0]["preprocess_policy_hash"] == cache.claims[1]["preprocess_policy_hash"]

    # A cold v5 upload must preserve v4's explicit multipart MIME contract too.
    cold_factory = provider_fixture.FakeClientFactory(provider_fixture._completed_stream())
    cold_provider = DoubaoArkVlmProvider(
        provider_fixture._config(), file_cache=provider_fixture.MemoryFileCache(), client_factory=cold_factory,
    )
    assert isinstance(cold_provider.dispatch(_dispatch(_parameters(mode))), ProviderCompleted)
    assert cold_factory.files.create_calls == factory.files.create_calls


@pytest.mark.parametrize("mode", _INVALID_MODES)
def test_v5_policy_rejects_missing_or_invalid_mode(mode: object) -> None:
    with pytest.raises(ValueError, match="thinking_type"):
        factory_fixture._policy(adapter_strategy_version=_V5, thinking_type=mode)


@pytest.mark.parametrize("adapter,stage", [
    (DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION, DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION),
    (DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION, DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION),
    (DOUBAO_ARK_ADAPTER_STRATEGY_VERSION, DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION),
])
def test_legacy_adapters_never_accept_explicit_thinking(adapter: str, stage: str) -> None:
    legacy = factory_fixture._policy(adapter_strategy_version=adapter, stage_strategy_version=stage)
    assert "thinking_type" not in legacy.request_parameters
    for mode in _MODES:
        with pytest.raises(ValueError, match="legacy Ark adapters"):
            replace(legacy, thinking_type=mode)


@pytest.mark.parametrize("stage", [DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION, DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION])
def test_v5_requires_latest_registered_stage_combination(stage: str) -> None:
    with pytest.raises(ValueError, match="replay combination"):
        factory_fixture._policy(adapter_strategy_version=_V5, thinking_type="disabled", stage_strategy_version=stage)


def _invalid_parameters() -> list[dict[str, object]]:
    valid = _parameters()
    missing = {key: value for key, value in valid.items() if key != "thinking_type"}
    return [
        missing,
        *({**valid, "thinking_type": mode} for mode in _INVALID_MODES),
        {**valid, "thinking": {"type": "disabled"}},
        {**valid, "adapter_strategy_version": DOUBAO_ARK_ADAPTER_STRATEGY_VERSION},
        {**valid, "adapter_strategy_version": "unknown-adapter"},
    ]


@pytest.mark.parametrize("parameters", _invalid_parameters())
def test_invalid_wire_parameters_fail_before_client_creation(parameters: dict[str, object]) -> None:
    factory = provider_fixture.FakeClientFactory(provider_fixture._completed_stream())
    cache = provider_fixture.MemoryFileCache()
    result = DoubaoArkVlmProvider(
        provider_fixture._config(), file_cache=cache, client_factory=factory,
    ).dispatch(_dispatch(parameters))
    assert isinstance(result, ProviderFailed)
    assert result.failure_code == "INVALID_PROVIDER_REQUEST"
    assert factory.calls == []
    assert cache.claims == []


@pytest.mark.parametrize("parameters", _invalid_parameters())
def test_reuse_identity_rejects_nonclosed_thinking_variants(parameters: dict[str, object]) -> None:
    request = replace(reuse_fixture._request(), request_parameters_json=json.dumps(parameters))
    with pytest.raises(ValueError):
        reuse_fixture._identity(request)


def test_legacy_reuse_hashes_remain_exact_and_each_explicit_mode_changes_identity() -> None:
    request = reuse_fixture._request()
    legacy = reuse_fixture._identity(request)
    assert legacy.canonical_hash == "sha256:e9d905885f7445f207733c1c07c72bf457835fbe0578a68c196a3b08078afb38"
    assert legacy.semantic_policy.canonical_hash == "sha256:f976fa62c813ecc88a7ddb2b8d2f88e8175d083b763a3fbb5bf9039cd2ddd5a2"
    identities = [
        reuse_fixture._identity(replace(request, request_parameters_json=json.dumps(_parameters(mode))))
        for mode in _MODES
    ]
    assert len({legacy.canonical_hash, *(identity.canonical_hash for identity in identities)}) == 4
    assert len({identity.semantic_policy.canonical_hash for identity in identities}) == 3
    assert [json.loads(identity.semantic_policy.request_parameters_json)["thinking_type"] for identity in identities] == list(_MODES)
    for identity, mode in zip(identities, _MODES, strict=True):
        reordered = replace(request, request_parameters_json=json.dumps(dict(reversed(tuple(_parameters(mode).items())))))
        assert reuse_fixture._identity(reordered).canonical_hash == identity.canonical_hash


def test_each_v5_profile_mode_roundtrips_exactly_with_distinct_hashes() -> None:
    hashes: set[str] = set()
    for mode in _MODES:
        policy = factory_fixture._policy(adapter_strategy_version=_V5, thinking_type=mode)
        profile = PipelineExecutionProfile.from_semantic_policies(policy, retry_policy=factory_fixture._retry_policy())
        rebuilt = PipelineExecutionProfile.from_mapping(json.loads(profile.canonical_json))
        assert rebuilt.schema_version == "pipeline-execution-profile-v10"
        assert rebuilt.canonical_json == profile.canonical_json
        assert rebuilt.to_doubao_policy() == policy
        hashes.add(rebuilt.canonical_hash)
        with pytest.raises(ValueError, match="requires execution profile v10"):
            replace(profile, schema_version="pipeline-execution-profile-v9")
    assert len(hashes) == 3


@pytest.mark.parametrize("parameters", _invalid_parameters())
def test_profile_rejects_nonclosed_thinking_variants(parameters: dict[str, object]) -> None:
    policy = factory_fixture._policy(adapter_strategy_version=_V5, thinking_type="disabled")
    profile = PipelineExecutionProfile.from_semantic_policies(policy, retry_policy=factory_fixture._retry_policy())
    with pytest.raises(ValueError):
        replace(profile, request_parameters_json=json.dumps(parameters, separators=(",", ":"), sort_keys=True))
    document = profile.to_mapping()
    document["request_parameters"] = parameters
    with pytest.raises(ValueError):
        PipelineExecutionProfile.from_mapping(document)


def test_semantic_authority_explicitly_disables_thinking_without_changing_budget_or_default() -> None:
    policy = load_installed_semantic_run_authority().vlm_policy
    assert policy.adapter_strategy_version == _V5
    assert policy.thinking_type == "disabled"
    assert policy.prompt_version == "vlm-semantic-pack-v8-context-assisted-core-observations"
    assert policy.max_output_tokens == factory_fixture._policy().max_output_tokens == 32768
    assert factory_fixture._policy().thinking_type is None


def test_factory_binds_mode_into_request_payload_and_source_reuse_identity() -> None:
    bundle = factory_fixture._source_bundle()
    request_hashes: set[str] = set()
    identity_hashes: set[str] = set()
    for mode in _MODES:
        policy = factory_fixture._policy(adapter_strategy_version=_V5, thinking_type=mode)
        request = build_doubao_vlm_request(
            source_bundle=bundle, episode_index=0, job=bundle.source_job,
            artifact_revision=1, idempotency_key="explicit-mode-test", policy=policy,
            retry_policy=factory_fixture._retry_policy(),
        )
        assert json.loads(request.request_payload)["request_parameters"] == policy.request_parameters
        assert request.manifest == bundle.prepared.episodes[0].manifest
        identity = derive_vlm_reuse_identity(request, source_bundle=bundle, provider_scope=provider_fixture._config())
        assert identity.origin_request_payload == request.request_payload
        assert identity.semantic_policy.request_parameters_json == policy.request_parameters_json
        request_hashes.add(request.request_hash)
        identity_hashes.add(identity.canonical_hash)
    assert len(request_hashes) == len(identity_hashes) == 3
