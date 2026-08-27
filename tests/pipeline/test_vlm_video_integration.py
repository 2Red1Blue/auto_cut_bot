"""V4 wiring checks; fake provider calls are not real video validation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from autocut_kernel.pipeline import GenerateVlmEvidenceCommand
from autocut_kernel.vlm import ProviderCompleted
from autocut_kernel.vlm.semantic_contracts import VLM_PARSER_V4, parser_contract_sha256_for
from autocut_kernel.vlm.semantic_pack_v4 import VlmSemanticPackV4
from jsonschema import Draft202012Validator

from auto_cut_bot.pipeline.runtime.models import PipelineExecutionProfile
from auto_cut_bot.pipeline.runtime.vlm_stage import VLM_EPISODE_MAX_CONCURRENCY
from auto_cut_bot.pipeline.vlm.doubao_ark_provider import (
    DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
    DoubaoArkVlmProvider,
)
from auto_cut_bot.pipeline.vlm.prompt import build_vlm_prompt, vlm_response_schema_json
from auto_cut_bot.pipeline.vlm.request_factory import (
    DOUBAO_VLM_VIDEO_STAGE_STRATEGY_VERSION,
    build_doubao_vlm_request,
)
from auto_cut_bot.pipeline.vlm.video_prompt import (
    VLM_VIDEO_PROMPT_VERSION,
    build_vlm_video_prompt,
    vlm_video_response_schema_json,
)
from tests.pipeline import test_doubao_ark_provider as provider_fixture
from tests.pipeline import test_doubao_vlm_request_factory as factory_fixture
from tests.pipeline import test_pipeline_vlm_stage as stage_fixture
from tests.vlm.test_semantic_pack_v4 import _raw, _wire


def _policy():
    return factory_fixture._policy(
        adapter_strategy_version=DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
        thinking_type="disabled", parser_strategy_version=VLM_PARSER_V4,
        parser_contract_sha256=parser_contract_sha256_for(VLM_PARSER_V4),
        prompt_version=VLM_VIDEO_PROMPT_VERSION,
        response_schema_json=vlm_video_response_schema_json(),
        stage_strategy_version=DOUBAO_VLM_VIDEO_STAGE_STRATEGY_VERSION,
    )


def test_v4_factory_freezes_schema_parser_prompt_and_does_not_mutate_v3() -> None:
    original = factory_fixture._policy()
    policy = _policy()
    bundle = factory_fixture._source_bundle()
    request = build_doubao_vlm_request(
        source_bundle=bundle, episode_index=0, job=bundle.source_job,
        artifact_revision=1, idempotency_key="v4-wire", policy=policy,
        retry_policy=factory_fixture._retry_policy(),
    )
    assert request.parser_strategy_version == VLM_PARSER_V4
    assert request.prompt_template == build_vlm_video_prompt(request.manifest)
    assert build_vlm_prompt(request.manifest, prompt_version=policy.prompt_version) == request.prompt_template
    assert json.loads(request.request_payload)["response_schema"]["properties"]["schema_version"]["const"] == 4
    Draft202012Validator(json.loads(policy.response_schema_json)).validate(_wire())
    assert factory_fixture._policy() == original
    assert parser_contract_sha256_for("strict-semantic-pack-v3") == (
        "sha256:6963125a7ac28e2131b0473dd9de818b97ad8cc7f003359cf73d1b877b7f0a19"
    )
    assert parser_contract_sha256_for(VLM_PARSER_V4) != parser_contract_sha256_for("strict-semantic-pack-v3")


@pytest.mark.parametrize("changes", [
    {"parser_strategy_version": "strict-semantic-pack-v3"},
    {"response_schema_json": vlm_response_schema_json()},
    {"prompt_version": "vlm-semantic-pack-v4-compact"},
    {"stage_strategy_version": "doubao-generate-vlm-semantic-pack-v3-probe-then-parallel-10-v3"},
    {"adapter_strategy_version": "doubao-ark-files-responses-stream-v4", "thinking_type": None},
])
def test_mixed_wire_versions_are_rejected_before_request_creation(changes) -> None:
    with pytest.raises(ValueError):
        replace(_policy(), **changes)


def test_v4_ark_uses_direct_schema_with_explicit_mode_and_same_media_cache() -> None:
    client = provider_fixture.FakeClientFactory(provider_fixture._completed_stream())
    cache = provider_fixture.MemoryFileCache()
    provider = DoubaoArkVlmProvider(provider_fixture._config(), file_cache=cache, client_factory=client)
    request = provider_fixture._dispatch()
    assert isinstance(provider.dispatch(request), ProviderCompleted)
    payload = json.loads(request.request_payload)
    payload.update({
        "request_parameters": _policy().request_parameters,
        "response_schema": json.loads(vlm_video_response_schema_json()),
        "parser_strategy_version": VLM_PARSER_V4,
        "parser_contract_sha256": parser_contract_sha256_for(VLM_PARSER_V4),
        "prompt_version": VLM_VIDEO_PROMPT_VERSION,
    })
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    result = provider.dispatch(replace(
        request, request_payload=raw,
        request_payload_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
    ))
    assert isinstance(result, ProviderCompleted)
    body = client.responses.create_calls[-1]
    assert body["thinking"] == {"type": "disabled"}
    assert body["stream"] is True
    assert body["text"]["format"]["name"] == "vlm_semantic_pack_v4"
    assert body["text"]["format"]["schema"] == payload["response_schema"]
    assert len(client.files.create_calls) == 1
    assert cache.claims[0]["preprocess_policy_hash"] == cache.claims[1]["preprocess_policy_hash"]


def test_generation_v4_commits_actual_pack_and_replays_without_provider() -> None:
    bundle, blobs = stage_fixture._bundle()
    store = stage_fixture.KernelStore(source_outcome=stage_fixture._source_success(), blobs=blobs)
    raw = _raw(_wire())

    class Provider:
        calls = 0

        def dispatch(self, request):
            self.calls += 1
            request.on_provider_request_id("fixture-v4-response")
            return ProviderCompleted(raw, "fixture-v4-response")

        def reconcile(self, _query):
            raise AssertionError("committed replay must not query provider")

    provider = Provider()
    request = build_doubao_vlm_request(
        source_bundle=bundle, episode_index=0, job=bundle.source_job,
        artifact_revision=1, idempotency_key="vlm:v4-fixture", policy=_policy(),
        retry_policy=factory_fixture._retry_policy(),
    )
    command = GenerateVlmEvidenceCommand(store, provider)
    first = command.execute(request)
    assert first.outcome.state == "succeeded"
    assert type(first.semantic_pack) is VlmSemanticPackV4
    assert first.semantic_pack.raw_response_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    replay = command.execute(request)
    assert replay.semantic_pack == first.semantic_pack
    assert replay.outcome.receipt_id == first.outcome.receipt_id
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_v4_runtime_preserves_single_probe_then_parallel_and_versions_batch(monkeypatch) -> None:
    bundle, blobs = stage_fixture._bundle(VLM_EPISODE_MAX_CONCURRENCY + 1)
    stage, _store, _provider = stage_fixture._stage(
        monkeypatch, bundle=bundle, blobs=blobs, source_outcome=stage_fixture._source_success(),
    )
    command = stage_fixture._ProbeThenParallelCommand(VLM_EPISODE_MAX_CONCURRENCY)
    finalizer = stage_fixture._ParallelBatchFinalizer()
    stage._command, stage._finalizer = command, finalizer
    context = replace(stage_fixture._context(), execution_profile=PipelineExecutionProfile.from_semantic_policies(
        _policy(), retry_policy=factory_fixture._retry_policy(),
    ))
    execution = asyncio.create_task(stage.execute(context))
    assert await asyncio.to_thread(command.probe_started.wait, 2)
    assert len(command.requests) == 1
    command.release_probe()
    assert (await execution).outcome == "succeeded"
    assert command.max_active == 10
    assert all(request.parser_strategy_version == VLM_PARSER_V4 for request in command.requests)
    assert len(finalizer.requests) == 1
    assert finalizer.requests[0].strategy_version == "vlm-batch-finalizer-v2-semantic-pack-v4"
