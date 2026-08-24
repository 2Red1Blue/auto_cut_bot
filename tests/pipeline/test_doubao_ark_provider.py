from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from autocut_kernel.vlm import (
    ProviderCompleted,
    ProviderDispatchRequest,
    ProviderFailed,
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
    WindowProxyBlobRef,
)

from auto_cut_bot.pipeline.vlm import (
    DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_PROVIDER_ID,
    ArkFileCacheRecord,
    DoubaoArkVlmProvider,
    DoubaoArkVlmProviderConfig,
)
from auto_cut_bot.pipeline.vlm.doubao_ark_provider import _ark_client


class MemoryFileCache:
    def __init__(self) -> None:
        self.record: ArkFileCacheRecord | None = None
        self.lease_acquired_on_replay = False

    def claim(self, **kwargs: object) -> tuple[ArkFileCacheRecord, bool]:
        if self.record is not None:
            return self.record, self.lease_acquired_on_replay
        now = datetime.now(timezone.utc)
        self.record = ArkFileCacheRecord(
            media_object_id=uuid4(),
            provider_id=str(kwargs["provider_id"]),
            provider_scope_fingerprint=str(kwargs["provider_scope_fingerprint"]),
            content_hash=str(kwargs["content_hash"]),
            byte_length=int(str(kwargs["byte_length"])),
            media_type=str(kwargs["media_type"]),
            preprocess_policy_hash=str(kwargs["preprocess_policy_hash"]),
            generation=1,
            state="reserved",
            version=0,
            provider_file_id=None,
            provider_status=None,
            failure_code=None,
            reserved_at=now,
            uploaded_at=None,
            available_at=None,
            expires_at=None,
            completed_at=None,
            lease_token="lease-memory-1",
            lease_expires_at=now + timedelta(seconds=int(str(kwargs["lease_seconds"]))),
            audit_expires_at=None,
        )
        return self.record, True

    def record_processing(self, media_object_id: object, **kwargs: object) -> ArkFileCacheRecord:
        assert self.record is not None and self.record.media_object_id == media_object_id
        self.record = replace(
            self.record,
            state="processing",
            version=self.record.version + 1,
            provider_file_id=str(kwargs["provider_file_id"]),
            provider_status=str(kwargs["provider_status"]),
            uploaded_at=datetime.now(timezone.utc),
        )
        return self.record

    def record_available(self, media_object_id: object, **kwargs: object) -> ArkFileCacheRecord:
        assert self.record is not None and self.record.media_object_id == media_object_id
        now = datetime.now(timezone.utc)
        self.record = replace(
            self.record,
            state="available",
            version=self.record.version + 1,
            provider_status=str(kwargs["provider_status"]),
            available_at=now,
            expires_at=kwargs["expires_at"],
            lease_token=None,
            lease_expires_at=None,
        )
        return self.record

    def record_indeterminate(self, media_object_id: object, **kwargs: object) -> ArkFileCacheRecord:
        assert self.record is not None and self.record.media_object_id == media_object_id
        self.record = replace(
            self.record,
            state="indeterminate",
            version=self.record.version + 1,
            provider_status=str(kwargs["provider_status"]),
            completed_at=datetime.now(timezone.utc),
            lease_token=None,
            lease_expires_at=None,
            audit_expires_at=kwargs["audit_expires_at"],
        )
        return self.record

    def record_failed(self, media_object_id: object, **kwargs: object) -> ArkFileCacheRecord:
        assert self.record is not None and self.record.media_object_id == media_object_id
        self.record = replace(
            self.record,
            state="failed",
            version=self.record.version + 1,
            provider_status=str(kwargs["provider_status"]),
            failure_code=str(kwargs["failure_code"]),
            completed_at=datetime.now(timezone.utc),
            lease_token=None,
            lease_expires_at=None,
        )
        return self.record

    def release_processing(self, media_object_id: object, **kwargs: object) -> ArkFileCacheRecord:
        assert self.record is not None and self.record.media_object_id == media_object_id
        self.record = replace(
            self.record,
            version=self.record.version + 1,
            provider_status=str(kwargs["provider_status"]),
            lease_expires_at=datetime.now(timezone.utc),
        )
        return self.record

    def mark_expired(self, media_object_id: object, **kwargs: object) -> ArkFileCacheRecord:
        assert self.record is not None and self.record.media_object_id == media_object_id
        self.record = replace(
            self.record,
            state="expired",
            version=self.record.version + 1,
            provider_status=str(kwargs["provider_status"]),
            completed_at=datetime.now(timezone.utc),
        )
        return self.record


MODEL_ID = "doubao-seed-2-1-pro-260628"


def _response(
    response_id: str,
    text: str,
    status: str = "completed",
    *,
    model: str = MODEL_ID,
) -> object:
    content = SimpleNamespace(type="output_text", text=text)
    output = [SimpleNamespace(type="message", content=[content])]
    return SimpleNamespace(id=response_id, model=model, status=status, output=output)


class FakeFiles:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.retrieve_calls: list[str] = []
        self.wait_calls: list[tuple[str, dict[str, object]]] = []
        self.status = "active"
        self.retrieve_error: Exception | None = None
        self.create_error: Exception | None = None
        self.wait_error: Exception | None = None

    def create(self, **kwargs: object) -> object:
        self.create_calls.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return SimpleNamespace(id="file-doubao-1", status="processing")

    def wait_for_processing(self, file_id: str, **kwargs: object) -> None:
        self.wait_calls.append((file_id, kwargs))
        if self.wait_error is not None:
            raise self.wait_error

    def retrieve(self, file_id: str) -> object:
        self.retrieve_calls.append(file_id)
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return SimpleNamespace(id=file_id, status=self.status)


class FakeResponses:
    def __init__(self, stream: object) -> None:
        self.stream = stream
        self.create_calls: list[dict[str, object]] = []
        self.retrieve_calls: list[str] = []
        self.retrieve_result = _response("response-reconciled", '{"schema_version":1}')

    def create(self, **kwargs: object) -> object:
        self.create_calls.append(kwargs)
        return self.stream

    def retrieve(self, response_id: str) -> object:
        self.retrieve_calls.append(response_id)
        return self.retrieve_result


class FakeClientFactory:
    def __init__(self, stream: object) -> None:
        self.files = FakeFiles()
        self.responses = FakeResponses(stream)
        self.client = SimpleNamespace(files=self.files, responses=self.responses)
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.client


def _payload() -> bytes:
    video = b"real-proxy-video"
    return json.dumps(
        {
            "model_id": "doubao-seed-2-1-pro-260628",
            "parser_strategy_version": "strict-v1",
            "prompt": "strict prompt",
            "prompt_version": "prompt-v1",
            "provider_id": DOUBAO_ARK_PROVIDER_ID,
            "proxy_blob": {
                "byte_length": len(video),
                "content_hash": "sha256:" + hashlib.sha256(video).hexdigest(),
                "media_type": "video/mp4",
                "object_id": "proxy-1",
            },
            "request_parameters": {
                "adapter_strategy_version": DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
                "max_output_tokens": 4096,
                "temperature": 0,
                "video_fps": 1.0,
            },
            "response_schema": {"type": "object"},
            "window_manifest_set_sha256": "sha256:" + "2" * 64,
            "window_manifest_sha256": "sha256:" + "3" * 64,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _dispatch(*, created_ids: list[str] | None = None) -> ProviderDispatchRequest:
    video = b"real-proxy-video"
    payload = _payload()
    return ProviderDispatchRequest(
        DOUBAO_ARK_PROVIDER_ID,
        "doubao-seed-2-1-pro-260628",
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
        (lambda _provider_request_id: None)
        if created_ids is None
        else created_ids.append,
    )


def _completed_stream() -> list[object]:
    response = _response("response-doubao-1", '{"schema_version":1}')
    return [
        SimpleNamespace(
            type="response.created",
            response=SimpleNamespace(id="response-doubao-1", model=MODEL_ID),
        ),
        SimpleNamespace(type="response.output_text.delta", delta='{"schema_'),
        SimpleNamespace(type="response.output_text.done", text='{"schema_version":1}'),
        SimpleNamespace(type="response.completed", response=response),
    ]


def _config(**overrides: object) -> DoubaoArkVlmProviderConfig:
    values: dict[str, object] = {
        "api_key": "secret",
        "tenant_id": "tenant-a",
        "project_id": "project-a",
    }
    values.update(overrides)
    return DoubaoArkVlmProviderConfig(**values)  # type: ignore[arg-type]


def test_default_ark_sdk_factory_constructs_official_client_without_network() -> None:
    client = _ark_client(
        api_key="not-a-real-secret",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        timeout=1.0,
        max_retries=0,
    )

    assert type(client).__module__.startswith("volcenginesdkarkruntime")
    assert hasattr(client, "files")
    assert hasattr(client, "responses")


def test_doubao_ark_uploads_once_and_consumes_a_completed_sse_stream() -> None:
    cache = MemoryFileCache()
    factory = FakeClientFactory(_completed_stream())
    provider = DoubaoArkVlmProvider(
        _config(),
        file_cache=cache,
        client_factory=factory,
    )

    created_ids: list[str] = []
    result = provider.dispatch(_dispatch(created_ids=created_ids))

    assert isinstance(result, ProviderCompleted)
    assert result.raw_response == b'{"schema_version":1}'
    assert result.provider_request_id == "response-doubao-1"
    assert created_ids == ["response-doubao-1"]
    assert cache.record is not None and cache.record.state == "available"
    assert len(factory.files.create_calls) == 1
    assert len(factory.responses.create_calls) == 1
    assert factory.calls[0]["max_retries"] == 0
    call = factory.responses.create_calls[0]
    assert call["stream"] is True
    assert call["store"] is True
    assert call["text"] == {
        "format": {
            "type": "json_schema",
            "name": "vlm_observation_set",
            "strict": True,
            "schema": {"type": "object"},
        }
    }
    assert call["input"][0]["content"][0] == {
        "type": "input_video",
        "file_id": "file-doubao-1",
    }


def test_doubao_ark_reuses_validated_file_id_without_uploading_again() -> None:
    cache = MemoryFileCache()
    first = FakeClientFactory(_completed_stream())
    provider = DoubaoArkVlmProvider(
        _config(), file_cache=cache, client_factory=first
    )
    assert isinstance(provider.dispatch(_dispatch()), ProviderCompleted)

    second = FakeClientFactory(_completed_stream())
    provider = DoubaoArkVlmProvider(
        _config(), file_cache=cache, client_factory=second
    )
    result = provider.dispatch(_dispatch())

    assert isinstance(result, ProviderCompleted)
    assert not second.files.create_calls
    assert second.files.retrieve_calls == ["file-doubao-1"]
    assert len(second.responses.create_calls) == 1


def test_doubao_ark_expires_a_cached_file_that_provider_no_longer_has() -> None:
    cache = MemoryFileCache()
    first = FakeClientFactory(_completed_stream())
    provider = DoubaoArkVlmProvider(
        _config(), file_cache=cache, client_factory=first
    )
    assert isinstance(provider.dispatch(_dispatch()), ProviderCompleted)

    missing = RuntimeError("not found")
    missing.status_code = 404  # type: ignore[attr-defined]
    second = FakeClientFactory(_completed_stream())
    second.files.retrieve_error = missing
    provider = DoubaoArkVlmProvider(
        _config(), file_cache=cache, client_factory=second
    )

    result = provider.dispatch(_dispatch())

    assert isinstance(result, ProviderFailed)
    assert result.failure_code == "PROVIDER_MEDIA_NOT_AVAILABLE"
    assert cache.record is not None and cache.record.state == "expired"
    assert not second.responses.create_calls


def test_doubao_ark_rejects_partial_output_from_incomplete_stream() -> None:
    response = _response("response-incomplete", '{"partial":true}', "incomplete")
    factory = FakeClientFactory(
        [
            SimpleNamespace(type="response.output_text.delta", delta='{"partial":true}'),
            SimpleNamespace(type="response.incomplete", response=response),
        ]
    )
    provider = DoubaoArkVlmProvider(
        _config(),
        file_cache=MemoryFileCache(),
        client_factory=factory,
    )

    result = provider.dispatch(_dispatch())

    assert isinstance(result, ProviderFailed)
    assert result.failure_code == "PROVIDER_RESPONSE_INCOMPLETE"
    assert len(factory.responses.create_calls) == 1


def test_doubao_ark_missing_terminal_event_is_indeterminate_and_not_resubmitted() -> None:
    factory = FakeClientFactory(
        [
            SimpleNamespace(
                type="response.created",
                response=SimpleNamespace(id="response-open", model=MODEL_ID),
            ),
            SimpleNamespace(type="response.output_text.delta", delta="partial"),
        ]
    )
    provider = DoubaoArkVlmProvider(
        _config(),
        file_cache=MemoryFileCache(),
        client_factory=factory,
    )

    result = provider.dispatch(_dispatch())

    assert isinstance(result, ProviderIndeterminate)
    assert result.provider_request_id == "response-open"
    assert len(factory.responses.create_calls) == 1


def test_doubao_ark_stream_interruption_preserves_request_id_for_reconcile() -> None:
    def interrupted_stream():  # type: ignore[no-untyped-def]
        yield SimpleNamespace(
            type="response.created",
            response=SimpleNamespace(id="response-interrupted", model=MODEL_ID),
        )
        raise TimeoutError("stream interrupted")

    factory = FakeClientFactory(interrupted_stream())
    provider = DoubaoArkVlmProvider(
        _config(),
        file_cache=MemoryFileCache(),
        client_factory=factory,
    )

    interrupted = provider.dispatch(_dispatch())

    assert isinstance(interrupted, ProviderIndeterminate)
    assert interrupted.reason_code == "PROVIDER_STREAM_INTERRUPTED"
    assert interrupted.provider_request_id == "response-interrupted"
    factory.responses.retrieve_result = _response(
        "response-interrupted", '{"schema_version":1}'
    )
    reconciled = provider.reconcile(
        ProviderReconcileQuery(
            DOUBAO_ARK_PROVIDER_ID,
            "doubao-seed-2-1-pro-260628",
            "sha256:" + "9" * 64,
            interrupted.provider_request_id,
        )
    )
    assert isinstance(reconciled, ProviderCompleted)
    assert factory.responses.retrieve_calls == ["response-interrupted"]


def test_doubao_ark_callback_failure_preserves_created_id_for_kernel_fallback() -> None:
    factory = FakeClientFactory(_completed_stream())
    provider = DoubaoArkVlmProvider(
        _config(),
        file_cache=MemoryFileCache(),
        client_factory=factory,
    )
    observed_ids: list[str] = []

    def fail_persistence(provider_request_id: str) -> None:
        observed_ids.append(provider_request_id)
        raise RuntimeError("simulated request-id CAS failure")

    result = provider.dispatch(
        replace(_dispatch(), on_provider_request_id=fail_persistence)
    )

    assert isinstance(result, ProviderIndeterminate)
    assert result.reason_code == "PROVIDER_REQUEST_ID_PERSIST_FAILED"
    assert result.provider_request_id == "response-doubao-1"
    assert observed_ids == ["response-doubao-1"]
    assert len(factory.responses.create_calls) == 1
    factory.responses.retrieve_result = _response(
        "response-doubao-1", '{"schema_version":1}'
    )

    reconciled = provider.reconcile(
        ProviderReconcileQuery(
            DOUBAO_ARK_PROVIDER_ID,
            MODEL_ID,
            "sha256:" + "6" * 64,
            result.provider_request_id,
        )
    )

    assert isinstance(reconciled, ProviderCompleted)
    assert factory.responses.retrieve_calls == ["response-doubao-1"]
    assert len(factory.responses.create_calls) == 1


def test_doubao_ark_reconcile_retrieves_instead_of_creating() -> None:
    factory = FakeClientFactory([])
    provider = DoubaoArkVlmProvider(
        _config(),
        file_cache=MemoryFileCache(),
        client_factory=factory,
    )
    query = ProviderReconcileQuery(
        DOUBAO_ARK_PROVIDER_ID,
        "doubao-seed-2-1-pro-260628",
        "sha256:" + "8" * 64,
        "response-reconciled",
    )

    result = provider.reconcile(query)

    assert isinstance(result, ProviderCompleted)
    assert factory.responses.retrieve_calls == ["response-reconciled"]
    assert not factory.responses.create_calls

    factory.responses.retrieve_result = SimpleNamespace(
        id="response-pending", model=MODEL_ID, status="in_progress", output=[]
    )
    pending = provider.reconcile(replace(query, provider_request_id="response-pending"))
    assert isinstance(pending, ProviderPending)


def test_doubao_ark_terminal_output_never_falls_back_to_delta_or_done_text() -> None:
    response = SimpleNamespace(
        id="response-no-authority",
        model=MODEL_ID,
        status="completed",
        output=[],
    )
    factory = FakeClientFactory(
        [
            SimpleNamespace(
                type="response.created",
                response=SimpleNamespace(id="response-no-authority", model=MODEL_ID),
            ),
            SimpleNamespace(type="response.output_text.delta", delta='{"partial":true}'),
            SimpleNamespace(type="response.output_text.done", text='{"partial":true}'),
            SimpleNamespace(type="response.completed", response=response),
        ]
    )
    provider = DoubaoArkVlmProvider(
        _config(), file_cache=MemoryFileCache(), client_factory=factory
    )

    result = provider.dispatch(_dispatch(created_ids=[]))

    assert isinstance(result, ProviderFailed)
    assert result.failure_code == "PROVIDER_RESPONSE_OUTPUT_INVALID"


def test_doubao_ark_rejects_created_completed_identity_or_model_change() -> None:
    for completed in (
        _response("response-other", '{"schema_version":1}'),
        _response("response-stable", '{"schema_version":1}', model="wrong-model"),
    ):
        factory = FakeClientFactory(
            [
                SimpleNamespace(
                    type="response.created",
                    response=SimpleNamespace(id="response-stable", model=MODEL_ID),
                ),
                SimpleNamespace(type="response.completed", response=completed),
            ]
        )
        provider = DoubaoArkVlmProvider(
            _config(), file_cache=MemoryFileCache(), client_factory=factory
        )

        result = provider.dispatch(_dispatch(created_ids=[]))

        assert isinstance(result, ProviderFailed)
        assert result.failure_code == "PROVIDER_RESPONSE_IDENTITY_MISMATCH"


def test_doubao_ark_enforces_bounded_stream_bytes() -> None:
    factory = FakeClientFactory(
        [
            SimpleNamespace(
                type="response.created",
                response=SimpleNamespace(id="response-large", model=MODEL_ID),
            ),
            SimpleNamespace(type="response.output_text.delta", delta="x" * 9),
            SimpleNamespace(type="response.output_text.delta", delta="y" * 9),
        ]
    )
    provider = DoubaoArkVlmProvider(
        _config(max_stream_bytes=16),
        file_cache=MemoryFileCache(),
        client_factory=factory,
    )

    result = provider.dispatch(_dispatch(created_ids=[]))

    assert isinstance(result, ProviderFailed)
    assert result.failure_code == "PROVIDER_STREAM_LIMIT_EXCEEDED"


def test_doubao_ark_unknown_upload_outcome_is_quarantined_without_blind_upload() -> None:
    cache = MemoryFileCache()
    first = FakeClientFactory(_completed_stream())
    first.files.create_error = TimeoutError("upload result unknown")
    provider = DoubaoArkVlmProvider(_config(), file_cache=cache, client_factory=first)

    result = provider.dispatch(_dispatch(created_ids=[]))

    assert isinstance(result, ProviderIndeterminate)
    assert result.reason_code == "PROVIDER_TRANSPORT_UNKNOWN"
    assert cache.record is not None and cache.record.state == "indeterminate"
    second = FakeClientFactory(_completed_stream())
    replay = DoubaoArkVlmProvider(_config(), file_cache=cache, client_factory=second).dispatch(
        _dispatch(created_ids=[])
    )
    assert isinstance(replay, ProviderIndeterminate)
    assert replay.reason_code == "PROVIDER_MEDIA_UPLOAD_OUTCOME_UNKNOWN"
    assert not second.files.create_calls


def test_doubao_ark_recovers_a_known_processing_file_by_retrieve_without_upload() -> None:
    cache = MemoryFileCache()
    first = FakeClientFactory(_completed_stream())
    first.files.status = "processing"
    first.files.wait_error = TimeoutError("processing poll interrupted")
    provider = DoubaoArkVlmProvider(_config(), file_cache=cache, client_factory=first)

    interrupted = provider.dispatch(_dispatch(created_ids=[]))

    assert isinstance(interrupted, ProviderIndeterminate)
    assert cache.record is not None and cache.record.state == "processing"
    assert cache.record.provider_file_id == "file-doubao-1"
    cache.lease_acquired_on_replay = True
    second = FakeClientFactory(_completed_stream())
    recovered = DoubaoArkVlmProvider(
        _config(), file_cache=cache, client_factory=second
    ).dispatch(_dispatch(created_ids=[]))

    assert isinstance(recovered, ProviderCompleted)
    assert not second.files.create_calls
    assert second.files.retrieve_calls == ["file-doubao-1"]


def test_doubao_ark_scope_fingerprint_changes_with_tenant_project_or_origin() -> None:
    base = _config().provider_scope_fingerprint

    assert _config(api_key="rotated-secret").provider_scope_fingerprint == base
    assert _config(tenant_id="tenant-b").provider_scope_fingerprint != base
    assert _config(project_id="project-b").provider_scope_fingerprint != base
    assert (
        _config(base_url="https://ark.example.com/api/v3").provider_scope_fingerprint
        != base
    )
