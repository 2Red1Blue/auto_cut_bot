"""Definition-first native wire, early rejection and immutable V4 replay."""

import hashlib
import json
from dataclasses import replace

import pytest
from autocut_kernel.pipeline import GenerateVlmEvidenceCommand
from autocut_kernel.vlm import ProviderCompleted, ProviderFailed
from autocut_kernel.vlm.parser import VlmResponseRejected
from autocut_kernel.vlm.semantic_contracts import VLM_PARSER_V4, parser_contract_sha256_for
from jsonschema import Draft202012Validator

from auto_cut_bot.pipeline.runtime.semantic_authority import load_installed_semantic_run_authority
from auto_cut_bot.pipeline.vlm.bounded_video_prompt import (
    VLM_BOUNDED_VIDEO_PROMPT_VERSION,
    VLM_VIDEO_FIELD_ORDER,
    vlm_bounded_video_response_schema_json,
    vlm_stable_candidate_video_response_schema_json,
)
from auto_cut_bot.pipeline.vlm.doubao_ark_provider import DoubaoArkVlmProvider
from auto_cut_bot.pipeline.vlm.request_factory import build_doubao_vlm_request
from tests.pipeline import test_doubao_ark_provider as provider_fixture
from tests.pipeline import test_doubao_vlm_request_factory as factory_fixture
from tests.pipeline import test_pipeline_vlm_stage as stage_fixture
from tests.pipeline.test_vlm_contextual_video_prompt import _pack
from tests.pipeline.test_vlm_video_integration import _policy
from tests.vlm.test_semantic_pack_v4 import _parse, _wire


def _bounded_policy():
    return replace(_policy(), prompt_version=VLM_BOUNDED_VIDEO_PROMPT_VERSION,
                   response_schema_json=vlm_bounded_video_response_schema_json())


def _dispatch_payload():
    request = provider_fixture._dispatch()
    payload = json.loads(request.request_payload)
    policy = _bounded_policy()
    payload.update({
        "prompt_version": policy.prompt_version,
        "parser_strategy_version": VLM_PARSER_V4,
        "parser_contract_sha256": parser_contract_sha256_for(VLM_PARSER_V4),
        "request_parameters": policy.request_parameters,
        "response_schema": json.loads(policy.response_schema_json),
    })
    return request, payload


def _dispatch_with_payload(request, payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return replace(request, request_payload=raw,
                   request_payload_sha256="sha256:" + hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("old_video_prompt", [False, True])
def test_final_sdk_schema_is_definition_first_and_file_cache_is_unchanged(old_video_prompt) -> None:
    client = provider_fixture.FakeClientFactory(provider_fixture._completed_stream())
    cache = provider_fixture.MemoryFileCache()
    provider = DoubaoArkVlmProvider(provider_fixture._config(), file_cache=cache, client_factory=client)
    request, payload = _dispatch_payload()
    if old_video_prompt:
        old_payload = json.loads(json.dumps(payload))
        old_payload["prompt_version"] = _policy().prompt_version
        old_payload["response_schema"] = json.loads(_policy().response_schema_json)
        request = _dispatch_with_payload(request, old_payload)
    assert isinstance(provider.dispatch(request), ProviderCompleted)
    old_body_bytes = json.dumps(client.responses.create_calls[-1], ensure_ascii=False, separators=(",", ":")).encode()
    assert isinstance(provider.dispatch(_dispatch_with_payload(request, payload)), ProviderCompleted)
    body = client.responses.create_calls[-1]
    schema = body["text"]["format"]["schema"]
    assert tuple(schema["properties"]) == VLM_VIDEO_FIELD_ORDER
    assert tuple(schema["required"]) == VLM_VIDEO_FIELD_ORDER
    assert schema == payload["response_schema"]
    assert body["thinking"] == {"type": "disabled"}
    assert body["stream"] is True
    assert len(client.files.create_calls) == 1
    assert cache.claims[0]["preprocess_policy_hash"] == cache.claims[1]["preprocess_policy_hash"]
    assert isinstance(provider.dispatch(request), ProviderCompleted)
    replay_body_bytes = json.dumps(client.responses.create_calls[-1], ensure_ascii=False, separators=(",", ":")).encode()
    assert replay_body_bytes == old_body_bytes


def test_v23_contextual_candidate_request_reaches_ark_with_its_exact_schema() -> None:
    policy = load_installed_semantic_run_authority().vlm_policy
    bundle = factory_fixture._source_bundle()
    built = build_doubao_vlm_request(
        source_bundle=bundle,
        episode_index=0,
        job=bundle.source_job,
        artifact_revision=1,
        idempotency_key="v23-provider-wire",
        policy=policy,
        retry_policy=factory_fixture._retry_policy(),
        context_pack=_pack(),
    )
    base = provider_fixture._dispatch()
    provider_payload = json.loads(built.request_payload)
    provider_payload["proxy_blob"] = base.proxy_blob_ref.to_mapping()
    provider_payload_bytes = json.dumps(
        provider_payload, separators=(",", ":"), sort_keys=True
    ).encode()
    dispatch = replace(
        base,
        request_payload=provider_payload_bytes,
        request_payload_sha256="sha256:" + hashlib.sha256(provider_payload_bytes).hexdigest(),
    )
    client = provider_fixture.FakeClientFactory(provider_fixture._completed_stream())
    provider = DoubaoArkVlmProvider(
        provider_fixture._config(),
        file_cache=provider_fixture.MemoryFileCache(),
        client_factory=client,
    )

    assert isinstance(provider.dispatch(dispatch), ProviderCompleted)
    body = client.responses.create_calls[-1]
    assert body["text"]["format"]["schema"] == json.loads(policy.response_schema_json)
    assert body["text"]["format"]["schema"]["properties"]["candidate_hypotheses"]["maxItems"] == 8


@pytest.mark.parametrize("tamper", ["schema", "old_schema", "unknown_prompt", "parser", "adapter", "digest", "boolean_schema"])
def test_invalid_combinations_fail_before_client_cache_and_upload(tamper) -> None:
    request, payload = _dispatch_payload()
    if tamper == "schema":
        payload["response_schema"]["properties"]["facts"]["maxItems"] = 500
    elif tamper == "old_schema":
        payload["response_schema"] = json.loads(_policy().response_schema_json)
    elif tamper == "unknown_prompt":
        payload["prompt_version"] = "unregistered-bounded-video"
    elif tamper == "parser":
        payload["parser_strategy_version"] = "strict-semantic-pack-v3"
        del payload["parser_contract_sha256"]
    elif tamper == "adapter":
        payload["request_parameters"]["adapter_strategy_version"] = "doubao-ark-files-responses-stream-v4"
        del payload["request_parameters"]["thinking_type"]
    elif tamper == "digest":
        payload["parser_contract_sha256"] = "sha256:" + "f" * 64
    elif tamper == "boolean_schema":
        payload["response_schema"]["properties"]["facts"]["items"]["properties"]["support"]["properties"]["interval_ms"]["properties"]["start_ms"]["minimum"] = False

    def forbidden_client(**_kwargs):
        raise AssertionError("invalid request reached client construction")

    cache = provider_fixture.MemoryFileCache()
    provider = DoubaoArkVlmProvider(provider_fixture._config(), file_cache=cache, client_factory=forbidden_client)
    result = provider.dispatch(_dispatch_with_payload(request, payload))
    assert isinstance(result, ProviderFailed)
    assert result.failure_code == "INVALID_PROVIDER_REQUEST"
    assert cache.claims == []


def _bounded_fixture_bytes() -> bytes:
    # Rename fixture-local IDs only. This helper is never used on real output.
    text = json.dumps(_wire(), ensure_ascii=False)
    for old, new in (("entity_1", "p001"), ("fact_1", "f001"), ("event_1", "e001"), ("candidate_1", "c001")):
        text = text.replace(json.dumps(old), json.dumps(new))
    return text.encode()


def test_generation_preserves_original_raw_and_replays_without_another_call() -> None:
    bundle, blobs = stage_fixture._bundle()
    store = stage_fixture.KernelStore(source_outcome=stage_fixture._source_success(), blobs=blobs)
    raw = _bounded_fixture_bytes()
    Draft202012Validator(json.loads(_bounded_policy().response_schema_json)).validate(json.loads(raw))

    class Provider:
        calls = 0

        def dispatch(self, request):
            self.calls += 1
            request.on_provider_request_id("bounded-video-fixture")
            return ProviderCompleted(raw, "bounded-video-fixture")

        def reconcile(self, _query):
            raise AssertionError("committed replay must not query provider")

    provider = Provider()
    request = build_doubao_vlm_request(
        source_bundle=bundle, episode_index=0, job=bundle.source_job,
        artifact_revision=1, idempotency_key="bounded-video-replay", policy=_bounded_policy(),
        retry_policy=factory_fixture._retry_policy(),
    )
    command = GenerateVlmEvidenceCommand(store, provider)
    first = command.execute(request)
    assert first.outcome.state == "succeeded"
    assert first.semantic_pack.raw_response_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    second = command.execute(request)
    assert second.semantic_pack == first.semantic_pack
    assert second.outcome.receipt_id == first.outcome.receipt_id
    assert provider.calls == 1


def test_v23_nonempty_candidate_is_strictly_parsed_as_semantic_evidence() -> None:
    wire = json.loads(_bounded_fixture_bytes())
    wire["continuity"]["temporal_segments"] = []
    schema = json.loads(vlm_stable_candidate_video_response_schema_json())

    Draft202012Validator(schema).validate(wire)
    pack = _parse(wire)

    assert len(pack.candidate_hypotheses) == 1
    assert pack.candidate_hypotheses[0].local_candidate_id == "c001"


def test_schema_vocabulary_does_not_replace_actual_reference_closure() -> None:
    wire = json.loads(_bounded_fixture_bytes())
    wire["events"][0]["fact_refs"] = ["f002"]
    Draft202012Validator(json.loads(_bounded_policy().response_schema_json)).validate(wire)
    with pytest.raises(VlmResponseRejected):
        _parse(wire)
