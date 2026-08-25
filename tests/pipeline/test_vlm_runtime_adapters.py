from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from autocut_kernel.store import BlobRef, Job
from autocut_kernel.vlm import (
    ProviderCompleted,
    ProviderDispatchRequest,
    ProviderFailed,
    ProviderIndeterminate,
    ProviderReconcileQuery,
    WindowProxyBlobRef,
)

from auto_cut_bot.pipeline.vlm import (
    DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_PROVIDER_ID,
    QWEN_ADAPTER_STRATEGY_VERSION,
    DoubaoArkVlmProvider,
    DoubaoArkVlmProviderConfig,
    IdentityProxyWindowBuilder,
    QwenVlmProvider,
    QwenVlmProviderConfig,
    build_vlm_prompt,
    vlm_response_schema_json,
)


class FakeCompletions:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result: object) -> None:
        self.completions = FakeCompletions(result)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeClientFactory:
    def __init__(self, result: object) -> None:
        self.client = FakeClient(result)
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.client


def _payload(parameters: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        {
            "model_id": "qwen3.7-plus",
            "parser_strategy_version": "strict-v1",
            "prompt": "strict prompt",
            "prompt_version": "prompt-v1",
            "provider_id": "qwen-openai-chat",
            "proxy_blob": {"content_hash": "sha256:" + "1" * 64},
            "request_parameters": parameters
            or {
                "adapter_strategy_version": QWEN_ADAPTER_STRATEGY_VERSION,
                "fps": 1.0,
                "max_tokens": 1024,
                "temperature": 0,
            },
            "response_schema": {"type": "object"},
            "window_manifest_set_sha256": "sha256:" + "2" * 64,
            "window_manifest_sha256": "sha256:" + "3" * 64,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _dispatch(parameters: dict[str, object] | None = None) -> ProviderDispatchRequest:
    video = b"mp4-bytes"
    payload = _payload(parameters)
    return ProviderDispatchRequest(
        "qwen-openai-chat",
        "qwen3.7-plus",
        "sha256:" + "4" * 64,
        payload,
        "sha256:" + hashlib.sha256(payload).hexdigest(),
        WindowProxyBlobRef(
            "proxy-1",
            "sha256:" + hashlib.sha256(video).hexdigest(),
            len(video),
            "video/mp4",
        ),
        video,
    )


def test_qwen_provider_submits_exactly_once_without_sdk_retries() -> None:
    response = SimpleNamespace(
        id="provider-request-1",
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"schema_version":1}'))],
    )
    factory = FakeClientFactory(response)
    provider = QwenVlmProvider(
        QwenVlmProviderConfig("secret"),
        client_factory=factory,
    )

    result = provider.dispatch(_dispatch())

    assert isinstance(result, ProviderCompleted)
    assert result.raw_response == b'{"schema_version":1}'
    assert result.provider_request_id == "provider-request-1"
    assert len(factory.calls) == 1
    assert factory.calls[0]["max_retries"] == 0
    assert len(factory.client.completions.calls) == 1
    call = factory.client.completions.calls[0]
    messages = call["messages"]
    assert isinstance(messages, list)
    video_part = messages[1]["content"][0]
    assert video_part["video_url"]["url"].endswith("bXA0LWJ5dGVz")
    assert call["response_format"] == {"type": "json_object"}
    assert '"schema":{"type":"object"}' not in messages[1]["content"][1]["text"]
    assert '完整 JSON Schema：{"type":"object"}' in messages[1]["content"][1]["text"]


def test_qwen_provider_maps_terminal_and_ambiguous_failures_without_retry() -> None:
    terminal = RuntimeError("do not expose this body")
    terminal.status_code = 400  # type: ignore[attr-defined]
    terminal.request_id = "request-bad"  # type: ignore[attr-defined]
    terminal_factory = FakeClientFactory(terminal)
    terminal_result = QwenVlmProvider(
        QwenVlmProviderConfig("secret"),
        client_factory=terminal_factory,
    ).dispatch(_dispatch())
    assert isinstance(terminal_result, ProviderFailed)
    assert terminal_result.failure_code == "PROVIDER_HTTP_400"
    assert "do not expose" not in terminal_result.failure_detail_json
    assert len(terminal_factory.client.completions.calls) == 1

    unknown_factory = FakeClientFactory(TimeoutError("unknown after submission"))
    unknown_result = QwenVlmProvider(
        QwenVlmProviderConfig("secret"),
        client_factory=unknown_factory,
    ).dispatch(_dispatch())
    assert isinstance(unknown_result, ProviderIndeterminate)
    assert len(unknown_factory.client.completions.calls) == 1


def test_qwen_provider_reconcile_does_not_submit_another_request() -> None:
    factory = FakeClientFactory(RuntimeError("must not be called"))
    provider = QwenVlmProvider(QwenVlmProviderConfig("secret"), client_factory=factory)

    result = provider.reconcile(
        ProviderReconcileQuery(
            "qwen-openai-chat",
            "qwen3.7-plus",
            "sha256:" + "5" * 64,
            "provider-request-1",
        )
    )

    assert isinstance(result, ProviderIndeterminate)
    assert not factory.calls


def test_qwen_provider_rejects_oversized_video_before_network() -> None:
    factory = FakeClientFactory(RuntimeError("must not be called"))
    provider = QwenVlmProvider(
        QwenVlmProviderConfig("secret", max_video_bytes=2),
        client_factory=factory,
    )

    result = provider.dispatch(_dispatch())

    assert isinstance(result, ProviderFailed)
    assert result.failure_code == "PROVIDER_MEDIA_LIMIT_EXCEEDED"
    assert not factory.calls


def test_qwen_provider_rejects_non_closed_parameters_before_network() -> None:
    factory = FakeClientFactory(RuntimeError("must not be called"))
    provider = QwenVlmProvider(QwenVlmProviderConfig("secret"), client_factory=factory)

    result = provider.dispatch(
        _dispatch(
            {
                "adapter_strategy_version": QWEN_ADAPTER_STRATEGY_VERSION,
                "fps": 1.0,
                "max_tokens": 10,
                "retries": 3,
            }
        )
    )

    assert isinstance(result, ProviderFailed)
    assert result.failure_code == "INVALID_PROVIDER_REQUEST"
    assert not factory.calls


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_identity_window_builder_binds_real_mp4_bytes_and_exact_pts(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=5:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    stored: list[bytes] = []

    class Store:
        def put_immutable_blob(
            self,
            job: Job,
            *,
            content: bytes,
            content_hash: str,
            media_type: str,
        ) -> BlobRef:
            assert job == Job("real-window", "test")
            assert content_hash == "sha256:" + hashlib.sha256(content).hexdigest()
            assert media_type == "video/mp4"
            stored.append(content)
            return BlobRef(uuid4(), content_hash, len(content), media_type)

    result = IdentityProxyWindowBuilder(sample_count=3).build(
        store=Store(),
        job=Job("real-window", "test"),
        source_path=source,
        source_id="source-real-window",
    )

    assert stored == [source.read_bytes()]
    assert result.manifest.timeline_map.certificate_kind == "translation_certificate"
    assert result.manifest.source_range == result.manifest.timeline_map.proxy_range
    assert len(result.manifest.frame_samples) == 3
    assert all(
        result.manifest.frame_pts_index_set.pts_index.contains(sample.source_pts)
        for sample in result.manifest.frame_samples
    )
    assert result.manifest_set.manifests == (result.manifest,)
    prompt = build_vlm_prompt(result.manifest)
    assert all(sample.frame_id in prompt for sample in result.manifest.frame_samples)
    assert json.loads(vlm_response_schema_json()) == json.loads(vlm_response_schema_json())


def test_doubao_v3_structured_output_keeps_streaming_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = "doubao-seed-2-1-pro-260628"
    video = b"semantic-pack-proxy"
    retry_policy = {
        "backoff_seconds": [2, 8],
        "max_attempts": 3,
        "strategy_version": "generation-retry-v1",
    }
    retry_bytes = json.dumps(retry_policy, separators=(",", ":"), sort_keys=True).encode()
    payload = json.dumps(
        {
            "model_id": model_id,
            "parser_strategy_version": "strict-v1",
            "prompt": "strict semantic pack prompt",
            "prompt_version": "vlm-semantic-pack-v3",
            "provider_id": DOUBAO_ARK_PROVIDER_ID,
            "proxy_blob": {
                "byte_length": len(video),
                "content_hash": "sha256:" + hashlib.sha256(video).hexdigest(),
                "media_type": "video/mp4",
                "object_id": "proxy-v3",
            },
            "request_parameters": {
                "adapter_strategy_version": DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
                "max_output_tokens": 16_384,
                "temperature": 0,
                "video_fps": 1.0,
            },
            "retry_policy": retry_policy,
            "retry_policy_sha256": "sha256:" + hashlib.sha256(retry_bytes).hexdigest(),
            "response_schema": {"type": "object"},
            "window_manifest_set_sha256": "sha256:" + "2" * 64,
            "window_manifest_sha256": "sha256:" + "3" * 64,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    final_response = SimpleNamespace(
        id="response-v3",
        model=model_id,
        status="completed",
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text='{"schema_version":3}')],
            )
        ],
    )
    stream = [
        SimpleNamespace(
            type="response.created",
            response=SimpleNamespace(id="response-v3", model=model_id),
        ),
        SimpleNamespace(type="response.output_text.delta", delta='{"schema_version":'),
        SimpleNamespace(type="response.completed", response=final_response),
    ]

    class Responses:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def create(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return stream

    responses = Responses()
    client = SimpleNamespace(responses=responses)
    monkeypatch.setattr(
        DoubaoArkVlmProvider,
        "_get_file_id",
        lambda *_args, **_kwargs: "file-v3",
    )
    provider = DoubaoArkVlmProvider(
        DoubaoArkVlmProviderConfig("secret", "tenant", "project"),
        file_cache=cast(Any, object()),
        client_factory=lambda **_kwargs: client,
    )
    result = provider.dispatch(
        ProviderDispatchRequest(
            DOUBAO_ARK_PROVIDER_ID,
            model_id,
            "sha256:" + "4" * 64,
            payload,
            "sha256:" + hashlib.sha256(payload).hexdigest(),
            WindowProxyBlobRef(
                "proxy-v3",
                "sha256:" + hashlib.sha256(video).hexdigest(),
                len(video),
                "video/mp4",
            ),
            video,
            lambda _provider_request_id: None,
        )
    )

    assert isinstance(result, ProviderCompleted)
    assert result.raw_response == b'{"schema_version":3}'
    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call["stream"] is True
    assert call["store"] is True
    assert call["max_output_tokens"] == 16_384
    assert call["text"] == {
        "format": {
            "type": "json_schema",
            "name": "vlm_semantic_pack_v3",
            "strict": True,
            "schema": {"type": "object"},
        }
    }
