"""Single-attempt text transport using a fake SDK only; no network/model/Store."""

import inspect
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import get_type_hints

import pytest
from autocut_kernel.semantic_chain.draft_provider import (
    DRAFT_DIRECT_SCHEMA_ADAPTER_STRATEGY_VERSION,
    DraftProviderPort,
)
from autocut_kernel.semantic_chain.stage1_draft import stage1_draft_response_schema
from autocut_kernel.vlm.provider_port import (
    ProviderCompleted,
    ProviderFailed,
    ProviderFailureDisposition,
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
)

from auto_cut_bot.pipeline.vlm.ark_responses_transport import (
    ArkResponsesTransport,
    ArkResponsesTransportConfig,
    _error_snapshot,
    create_ark_client,
)
from auto_cut_bot.pipeline.vlm.doubao_draft_provider import (
    DOUBAO_DRAFT_ADAPTER_STRATEGY_VERSION,
    DOUBAO_DRAFT_PROVIDER_ID,
    DoubaoDraftProvider,
)
from tests.semantic_chain.test_draft_provider import body, encoded, request
from tests.semantic_chain.test_stage1_draft import POLICY

MODEL = "doubao-test-model"
RESPONSE = "response-1"


def response(
    *, text='{"untrusted":"draft"}', response_id=RESPONSE, model=MODEL, status="completed"
):
    return SimpleNamespace(
        id=response_id,
        model=model,
        status=status,
        output=[
            SimpleNamespace(
                type="message", content=[SimpleNamespace(type="output_text", text=text)]
            )
        ],
    )


def created(response_id=RESPONSE, model=MODEL):
    return SimpleNamespace(
        type="response.created",
        response=response(response_id=response_id, model=model, status="in_progress"),
    )


def completed(**kwargs):
    return SimpleNamespace(type="response.completed", response=response(**kwargs))


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = 0

    def __iter__(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event

    def close(self):
        self.closed += 1


class FakeSDK:
    """There is intentionally no Files API on this fake client."""

    def __init__(self, events=None):
        self.stream = Stream([created(), completed()] if events is None else events)
        self.creates = []
        self.retrieves = []
        self.factories = []
        self.closed = 0
        self.result = response()
        self.create_error = None
        self.retrieve_error = None
        self.responses = self

    def factory(self, **kwargs):
        self.factories.append(kwargs)
        return self

    def create(self, **kwargs):
        self.creates.append(kwargs)
        if self.create_error:
            raise self.create_error
        return self.stream

    def retrieve(self, response_id):
        self.retrieves.append(response_id)
        if self.retrieve_error:
            raise self.retrieve_error
        return self.result

    def close(self):
        self.closed += 1


def provider(sdk, *, max_stream_bytes=10000, max_request_bytes=100000,
             adapter_strategy_version=DOUBAO_DRAFT_ADAPTER_STRATEGY_VERSION):
    return DoubaoDraftProvider(
        ArkResponsesTransportConfig("fake-key", "https://ark.invalid/api/v3", 20, max_stream_bytes),
        max_request_bytes=max_request_bytes,
        adapter_strategy_version=adapter_strategy_version,
        client_factory=sdk.factory,
    )


def query(response_id=RESPONSE):
    return ProviderReconcileQuery(DOUBAO_DRAFT_PROVIDER_ID, MODEL, "attempt-1", response_id)


def test_single_text_dispatch_exact_wire_and_callback_before_completion():
    sdk = FakeSDK()
    seen = []
    value = body()
    value["text"]["format"]["json_schema"]["schema"] = stage1_draft_response_schema(POLICY)
    raw = encoded(value)

    def persisted(response_id):
        assert len(sdk.creates) == 1 and sdk.closed == 0
        seen.append(response_id)

    adapter = provider(sdk)
    result = adapter.dispatch(request(payload=raw, callback=persisted))
    assert isinstance(adapter, DraftProviderPort)
    assert adapter.strategy_version == DOUBAO_DRAFT_ADAPTER_STRATEGY_VERSION
    assert type(result) is ProviderCompleted
    assert result.raw_response == b'{"untrusted":"draft"}'
    assert result.provider_request_id == RESPONSE and seen == [RESPONSE]
    assert sdk.creates == [value]  # no prefix, media, retry envelope or extra SDK feature
    assert sdk.factories == [
        {
            "api_key": "fake-key",
            "base_url": "https://ark.invalid/api/v3",
            "timeout": 20,
            "max_retries": 0,
        }
    ]
    assert sdk.stream.closed == sdk.closed == 1
    assert not sdk.retrieves


def test_sdk_annotations_are_not_endpoint_acceptance_evidence():
    from volcenginesdkarkruntime.resources.responses import Responses
    from volcenginesdkarkruntime.types.responses.response_text_config_param import (
        ResponseTextConfigParam,
    )
    from volcenginesdkarkruntime.types.shared_params.response_format_json_schema import (
        JSONSchema,
        ResponseFormatJSONSchema,
    )

    assert set(get_type_hints(ResponseFormatJSONSchema)) == {"json_schema", "type"}
    assert {"name", "schema", "strict"} <= set(get_type_hints(JSONSchema))
    assert "format" in get_type_hints(ResponseTextConfigParam)
    # The SDK decorator erases inspect.signature; inspect installed source,
    # without constructing or sending a provider request.
    assert "text: ResponseTextConfigParam" in inspect.getsource(Responses)


def test_v2_forwards_direct_schema_exactly_without_touching_prompt():
    sdk = FakeSDK()
    value = body()
    descriptor = value["text"]["format"].pop("json_schema")
    value["text"]["format"].update(descriptor)
    adapter = provider(sdk, adapter_strategy_version=DRAFT_DIRECT_SCHEMA_ADAPTER_STRATEGY_VERSION)
    result = adapter.dispatch(request(payload=encoded(value), callback=lambda _id: None))
    assert type(result) is ProviderCompleted
    assert sdk.creates == [value]


def test_installed_sdk_serializes_v2_direct_format_unchanged():
    """Exercise the real SDK serializer over an in-memory HTTP transport."""
    import httpx
    from volcenginesdkarkruntime import Ark

    captured = []

    def respond(http_request):
        captured.append(json.loads(http_request.content))
        base = {"id": RESPONSE, "model": MODEL, "created_at": 0,
                "object": "response", "tools": [], "output": []}
        created_body = {**base, "status": "in_progress"}
        completed_body = {**base, "status": "completed", "output": [
            {"type": "message", "role": "assistant", "id": "message-1",
             "content": [{"type": "output_text", "text": "{}", "annotations": []}]}]}
        events = [{"type": "response.created", "response": created_body},
                  {"type": "response.completed", "response": completed_body}]
        stream = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)

    def factory(**kwargs):
        return Ark(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))

    value = body()
    descriptor = value["text"]["format"].pop("json_schema")
    value["text"]["format"].update(descriptor)
    adapter = DoubaoDraftProvider(
        ArkResponsesTransportConfig("fake-key", "https://ark.invalid/api/v3", 20, 10000),
        max_request_bytes=100000,
        adapter_strategy_version=DRAFT_DIRECT_SCHEMA_ADAPTER_STRATEGY_VERSION,
        client_factory=factory,
    )
    result = adapter.dispatch(request(payload=encoded(value), callback=lambda _id: None))
    assert type(result) is ProviderCompleted
    assert len(captured) == 1
    sent = captured[0]
    assert {key: sent[key] for key in value} == value
    # This SDK emits omitted optional parameters as JSON null. They must not
    # become hidden non-null features; supplied prompt/schema remain exact.
    nullable_sdk_fields = {
        "instructions", "parallel_tool_calls", "previous_response_id", "thinking",
        "caching", "tool_choice", "tools", "top_p", "max_tool_calls",
        "context_management", "expire_at", "reasoning", "session", "service_tier",
    }
    extras = set(sent) - set(value)
    assert extras <= nullable_sdk_fields
    assert all(sent[key] is None for key in extras)


@pytest.mark.parametrize("direct", [False, True])
def test_wire_strategy_mismatch_rejected_before_client_creation(direct):
    sdk = FakeSDK()
    value = body()
    if direct:
        descriptor = value["text"]["format"].pop("json_schema")
        value["text"]["format"].update(descriptor)
    strategy = (DOUBAO_DRAFT_ADAPTER_STRATEGY_VERSION if direct
                else DRAFT_DIRECT_SCHEMA_ADAPTER_STRATEGY_VERSION)
    result = provider(sdk, adapter_strategy_version=strategy).dispatch(
        request(payload=encoded(value), callback=lambda _id: None))
    assert type(result) is ProviderFailed
    assert result.failure_code == "INVALID_PROVIDER_REQUEST"
    assert not sdk.factories and not sdk.creates


@pytest.mark.parametrize("nested", [False, True])
def test_error_diagnostics_are_bounded_redacted_and_not_retry_authority(nested):
    error = RuntimeError("must not serialize exception")
    details = {"code": "InvalidParameter", "param": "text.format", "type": "invalid_request",
               "message": "bad fake-key Bearer other-secret " + "x" * 3000,
               "headers": {"Authorization": "secret"}, "request": "do not copy"}
    error.body = {"error": details} if nested else details
    sdk = FakeSDK()
    sdk.create_error = error
    error.status_code = 400
    result = provider(sdk).dispatch(request(callback=lambda _id: None))
    snapshot = _error_snapshot(error, result, "fake-key")
    saved = snapshot["provider_error"]
    assert saved["param"] == "text.format"
    assert set(saved) == {"code", "param", "type", "message"}
    assert len(saved["message"]) == 2048
    assert "fake-key" not in json.dumps(snapshot) and "other-secret" not in json.dumps(snapshot)
    assert result.disposition is ProviderFailureDisposition.NONRETRYABLE
    assert len(sdk.creates) == 1
    error.body = {"message": "x" * 65537}
    assert _error_snapshot(error, result, "fake-key")["provider_error"]["message"].startswith("[omitted")


def test_default_sdk_factory_explicitly_disables_environment_proxy_and_retries(monkeypatch):
    captured = {}
    http_calls = []

    class Client:
        def __init__(self, **kwargs):
            http_calls.append(kwargs)

        def close(self):
            captured["http_closed"] = True

    def constructor(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted-proxy.invalid:8888")
    monkeypatch.setattr("auto_cut_bot.pipeline.vlm.ark_responses_transport.httpx.Client", Client)
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.vlm.ark_responses_transport.importlib.import_module",
        lambda name: SimpleNamespace(Ark=constructor),
    )
    create_ark_client(api_key="fake", base_url="https://ark.invalid", timeout=10, max_retries=0)
    assert http_calls == [{"timeout": 10, "trust_env": False}]
    assert captured["max_retries"] == 0 and type(captured["http_client"]) is Client
    with pytest.raises(ValueError):
        create_ark_client(api_key="fake", base_url="https://ark.invalid", timeout=10, max_retries=1)
    assert len(http_calls) == 1


@pytest.mark.parametrize(
    "failure", ["timeout_before_id", "timeout_after_id", "truncated", "error_event"]
)
def test_unknown_dispatch_never_retries_and_exact_id_reconcile_only(failure):
    if failure == "timeout_before_id":
        sdk = FakeSDK()
        sdk.create_error = TimeoutError("remote outcome unknown")
    elif failure == "timeout_after_id":
        sdk = FakeSDK([created(), TimeoutError("stream lost")])
    elif failure == "truncated":
        sdk = FakeSDK(
            [created(), SimpleNamespace(type="response.output_text.delta", delta="partial")]
        )
    else:
        sdk = FakeSDK([created(), SimpleNamespace(type="error")])
    adapter = provider(sdk)
    seen = []
    result = adapter.dispatch(request(callback=seen.append))
    assert type(result) is ProviderIndeterminate
    assert len(sdk.creates) == 1
    expected_id = None if failure == "timeout_before_id" else RESPONSE
    assert result.provider_request_id == expected_id
    recovered = adapter.reconcile(query(expected_id))
    if expected_id is None:
        assert type(recovered) is ProviderIndeterminate
        assert len(sdk.factories) == 1 and not sdk.retrieves
    else:
        assert type(recovered) is ProviderCompleted
        assert sdk.retrieves == [RESPONSE]
    assert len(sdk.creates) == 1


def test_callback_failure_stops_stream_and_retains_reconciliation_identity():
    sdk = FakeSDK()

    def broken(_response_id):
        raise RuntimeError("durable callback failed")

    result = provider(sdk).dispatch(request(callback=broken))
    assert type(result) is ProviderIndeterminate
    assert result.reason_code == "PROVIDER_REQUEST_ID_PERSIST_FAILED"
    assert result.provider_request_id == RESPONSE
    assert sdk.stream.closed == sdk.closed == 1
    assert len(sdk.creates) == 1


@pytest.mark.parametrize(
    "events",
    [
        [completed()],
        [created(model="foreign"), completed()],
        [created(), completed(model="foreign")],
        [created(), completed(response_id="foreign")],
        [created(), created("foreign"), completed()],
    ],
)
def test_stream_identity_mismatch_cannot_be_completed(events):
    sdk = FakeSDK(events)
    result = provider(sdk).dispatch(request(callback=lambda _id: None))
    assert type(result) is ProviderFailed
    assert result.failure_code == "PROVIDER_RESPONSE_IDENTITY_MISMATCH"
    assert len(sdk.creates) == 1 and sdk.stream.closed == 1


def test_repeated_created_event_persists_id_only_once():
    sdk = FakeSDK([created(), created(), completed()])
    seen = []
    assert type(provider(sdk).dispatch(request(callback=seen.append))) is ProviderCompleted
    assert seen == [RESPONSE]


@pytest.mark.parametrize("telemetry", [False, True])
def test_utf8_stream_byte_limit_and_terminal_response_limit_are_bounded(telemetry):
    events = [created()]
    events.append(
        SimpleNamespace(type="response.output_text.delta", delta="中中")
        if telemetry
        else completed(text="中中")
    )
    sdk = FakeSDK(events)
    result = provider(sdk, max_stream_bytes=5).dispatch(request(callback=lambda _id: None))
    assert type(result) is ProviderFailed
    assert result.failure_code == "PROVIDER_STREAM_LIMIT_EXCEEDED"
    assert result.disposition is ProviderFailureDisposition.REPAIRABLE
    assert len(sdk.creates) == 1 and sdk.closed == 1


@pytest.mark.parametrize(
    "status,expected",
    [
        ("queued", ProviderPending),
        ("in_progress", ProviderPending),
        ("completed", ProviderCompleted),
        ("failed", ProviderFailed),
        ("incomplete", ProviderFailed),
        ("cancelled", ProviderFailed),
        ("unknown", ProviderIndeterminate),
    ],
)
def test_reconcile_uses_shared_existing_result_states_without_redispatch(status, expected):
    sdk = FakeSDK()
    sdk.result = response(status=status)
    result = provider(sdk).reconcile(query())
    assert type(result) is expected
    assert sdk.retrieves == [RESPONSE] and not sdk.creates
    assert sdk.closed == 1


@pytest.mark.parametrize("changed", ["id", "model", "output", "limit"])
def test_reconcile_rechecks_identity_authoritative_body_and_bound(changed):
    sdk = FakeSDK()
    if changed == "id":
        sdk.result.id = "foreign"
    elif changed == "model":
        sdk.result.model = "foreign"
    elif changed == "output":
        sdk.result.output = []
    result = provider(sdk, max_stream_bytes=1 if changed == "limit" else 10000).reconcile(query())
    assert type(result) is ProviderFailed
    assert not sdk.creates


@pytest.mark.parametrize(
    "status,disposition",
    [
        (503, ProviderFailureDisposition.RETRYABLE),
        (429, ProviderFailureDisposition.RETRYABLE),
        (401, ProviderFailureDisposition.NONRETRYABLE),
        (422, ProviderFailureDisposition.NONRETRYABLE),
    ],
)
def test_explicit_http_failures_are_classified_without_adapter_retry_or_trace_id_substitution(
    status, disposition
):
    sdk = FakeSDK()
    error = RuntimeError("provider HTTP response")
    error.status_code = status
    error.request_id = "http-trace-not-response-id"
    sdk.create_error = error
    result = provider(sdk).dispatch(request(callback=lambda _id: None))
    assert type(result) is ProviderFailed and result.disposition is disposition
    assert result.provider_request_id is None
    assert (
        json.loads(result.failure_detail_json)["provider_trace_id"] == "http-trace-not-response-id"
    )
    assert len(sdk.creates) == 1


def test_reconcile_transport_failure_is_unknown_not_permission_to_retry_create():
    sdk = FakeSDK()
    sdk.retrieve_error = TimeoutError("unknown")
    result = provider(sdk).reconcile(query())
    assert type(result) is ProviderIndeterminate and result.provider_request_id == RESPONSE
    assert not sdk.creates and sdk.retrieves == [RESPONSE]


@pytest.mark.parametrize("kind", ["provider", "callback", "limit", "tamper"])
def test_invalid_or_over_budget_request_rejected_before_sdk_construction(kind):
    sdk = FakeSDK()
    value = request(callback=lambda _id: None)
    if kind == "provider":
        value = replace(value, provider_id="foreign")
    elif kind == "callback":
        value = replace(value, on_provider_request_id=None)
    elif kind == "tamper":
        # Public content DTOs are not capabilities: even a bypassed frozen
        # constructor is revalidated before external side effects.
        object.__setattr__(value, "request_payload_sha256", "sha256:" + "a" * 64)
    result = provider(sdk, max_request_bytes=1 if kind == "limit" else 100000).dispatch(value)
    assert type(result) is ProviderFailed
    assert not sdk.factories and not sdk.creates


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_seconds", True),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", 0),
        ("max_stream_bytes", True),
        ("max_stream_bytes", 0),
        ("base_url", "http://ark.invalid"),
        ("base_url", "https://user:secret@ark.invalid"),
        ("base_url", "https://ark.invalid?secret=x"),
    ],
)
def test_transport_configuration_is_explicit_and_finite(field, value):
    config = {
        "api_key": "fake",
        "base_url": "https://ark.invalid",
        "timeout_seconds": 10,
        "max_stream_bytes": 1000,
    }
    config[field] = value
    with pytest.raises(ValueError):
        ArkResponsesTransportConfig(**config)


def test_both_adapters_share_one_response_transport(monkeypatch):
    from auto_cut_bot.pipeline.vlm import doubao_ark_provider
    from tests.pipeline.test_doubao_ark_provider import (
        FakeClientFactory,
        MemoryFileCache,
        _completed_stream,
        _config,
        _dispatch,
    )

    calls = []

    def capture(self, body, **kwargs):
        calls.append(body)
        return ProviderIndeterminate("test-shared-transport")

    monkeypatch.setattr(ArkResponsesTransport, "dispatch", capture)
    sdk = FakeSDK()
    provider(sdk).dispatch(request(callback=lambda _id: None))
    existing = doubao_ark_provider.DoubaoArkVlmProvider(
        _config(),
        file_cache=MemoryFileCache(),
        client_factory=FakeClientFactory(_completed_stream()),
    )
    existing.dispatch(_dispatch(created_ids=[]))
    assert len(calls) == 2
    assert calls[0]["input"][0]["content"][0]["type"] == "input_text"
    assert calls[1]["input"][0]["content"][0]["type"] == "input_video"
    assert not hasattr(doubao_ark_provider, "_consume_stream")


def test_provider_result_is_raw_bytes_not_a_draft_parser_or_acceptance():
    sdk = FakeSDK([created(), completed(text="invalid JSON is still audit bytes")])
    result = provider(sdk).dispatch(request(callback=lambda _id: None))
    assert type(result) is ProviderCompleted
    assert result.raw_response == b"invalid JSON is still audit bytes"
    assert not hasattr(result, "admission")


def sdk_response_event(event_type, *, response_id=RESPONSE, model=MODEL, status, error=None):
    """Validate real SDK event shapes, without constructing an SDK client."""
    from volcenginesdkarkruntime.types.responses.response_created_event import (
        ResponseCreatedEvent,
    )
    from volcenginesdkarkruntime.types.responses.response_failed_event import (
        ResponseFailedEvent,
    )
    from volcenginesdkarkruntime.types.responses.response_incomplete_event import (
        ResponseIncompleteEvent,
    )

    event_class = {
        "created": ResponseCreatedEvent,
        "failed": ResponseFailedEvent,
        "incomplete": ResponseIncompleteEvent,
    }[event_type]
    return event_class.model_validate(
        {
            "type": f"response.{event_type}",
            "response": {
                "created_at": 0,
                "id": response_id,
                "model": model,
                "object": "response",
                "output": [],
                "status": status,
                "tools": [],
                "error": error,
            },
        }
    )


@pytest.mark.parametrize(
    "terminal,error,disposition",
    [
        ("failed", None, ProviderFailureDisposition.NONRETRYABLE),
        (
            "failed",
            {"code": "server_error", "message": "transient upstream failure"},
            ProviderFailureDisposition.RETRYABLE,
        ),
        ("incomplete", None, ProviderFailureDisposition.REPAIRABLE),
    ],
)
def test_sdk_typed_failure_terminal_preserves_verified_failure_classification(
    terminal, error, disposition
):
    sdk = FakeSDK(
        [
            sdk_response_event("created", status="in_progress"),
            sdk_response_event(terminal, status=terminal, error=error),
        ]
    )
    seen = []
    result = provider(sdk).dispatch(request(callback=seen.append))

    assert type(result) is ProviderFailed
    assert result.failure_code == f"PROVIDER_RESPONSE_{terminal.upper()}"
    assert result.disposition is disposition
    assert result.provider_request_id == RESPONSE and seen == [RESPONSE]
    assert len(sdk.creates) == 1 and not sdk.retrieves
    assert sdk.stream.closed == sdk.closed == 1


@pytest.mark.parametrize("terminal", ["failed", "incomplete"])
@pytest.mark.parametrize(
    "mismatch", ["id", "model", "status", "empty_id", "empty_model", "unannounced"]
)
def test_sdk_typed_unrelated_failure_is_unknown_and_can_only_reconcile_known_id(
    terminal, mismatch
):
    # Each event validates with the installed SDK. SDK shape validation cannot
    # establish the cross-event invocation identity or event/status agreement.
    terminal_event = sdk_response_event(
        terminal,
        response_id="foreign-response" if mismatch == "id" else (
            "" if mismatch == "empty_id" else RESPONSE
        ),
        model="foreign-model" if mismatch == "model" else (
            "" if mismatch == "empty_model" else MODEL
        ),
        status="completed" if mismatch == "status" else terminal,
        error={"code": "server_error", "message": "would otherwise authorize retry"},
    )
    events = [] if mismatch == "unannounced" else [
        sdk_response_event("created", status="in_progress")
    ]
    sdk = FakeSDK([*events, terminal_event])
    adapter = provider(sdk)
    seen = []
    result = adapter.dispatch(request(callback=seen.append))

    expected_id = None if mismatch == "unannounced" else RESPONSE
    assert type(result) is ProviderIndeterminate
    assert result.reason_code == "PROVIDER_TERMINAL_IDENTITY_UNVERIFIED"
    assert result.provider_request_id == expected_id
    assert seen == ([] if expected_id is None else [RESPONSE])
    assert len(sdk.creates) == 1 and not sdk.retrieves
    assert sdk.stream.closed == sdk.closed == 1

    recovered = adapter.reconcile(query(result.provider_request_id))
    if expected_id is None:
        assert type(recovered) is ProviderIndeterminate
        assert not sdk.retrieves and len(sdk.factories) == 1
    else:
        assert type(recovered) is ProviderCompleted
        assert recovered.provider_request_id == RESPONSE
        assert sdk.retrieves == [RESPONSE]
    assert len(sdk.creates) == 1  # never dispatch a successor from this uncertainty
