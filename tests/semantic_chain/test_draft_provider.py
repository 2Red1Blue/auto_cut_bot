"""Text request boundary tests, not provider execution or semantic acceptance."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.semantic_chain.draft_provider import (
    DraftDispatchRequest,
    DraftProviderError,
    DraftProviderPort,
    decode_draft_request_payload,
)
from autocut_kernel.vlm.provider_port import ProviderCompleted, ProviderReconcileQuery


def body():
    return {
        "model": "doubao-test-model",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "只依据 committed observations 提出 draft。"}
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "stage1_cross_window_draft_v1",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                        "required": [],
                    },
                },
            }
        },
        "max_output_tokens": 4096,
        "temperature": 0.25,
        "stream": True,
        "store": True,
    }


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def request(*, payload=None, callback=None, provider_id="doubao-ark-text-responses-stream"):
    raw = encoded(body()) if payload is None else payload
    return DraftDispatchRequest(
        provider_id,
        "doubao-test-model",
        "attempt-1",
        raw,
        "sha256:" + hashlib.sha256(raw).hexdigest(),
        callback,
    )


def test_actual_utf8_payload_hash_and_fresh_numeric_sdk_mapping():
    value = request()
    decoded = value.to_provider_body()
    assert decoded == body()
    assert decoded["temperature"] == 0.25
    assert (
        value.request_payload_sha256
        == "sha256:" + hashlib.sha256(value.request_payload).hexdigest()
    )
    decoded["input"][0]["content"][0]["text"] = "changed"
    assert value.to_provider_body() == body()
    with pytest.raises(FrozenInstanceError):
        value.model_id = "other"
    assert not hasattr(value, "proxy_blob_ref") and not hasattr(value, "proxy_content")


@pytest.mark.parametrize("field", list(body()))
def test_every_wire_field_is_required_without_defaults(field):
    mapping = body()
    del mapping[field]
    with pytest.raises(DraftProviderError):
        request(payload=encoded(mapping))


@pytest.mark.parametrize(
    "field",
    [
        "retry_policy",
        "retry_policy_sha256",
        "job",
        "provider_id",
        "tools",
        "previous_response_id",
        "instructions",
        "proxy_blob",
    ],
)
def test_durable_envelope_and_hidden_request_features_are_not_provider_wire(field):
    mapping = body()
    mapping[field] = {}
    with pytest.raises(DraftProviderError):
        request(payload=encoded(mapping))


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_video", "file_id": "media-file"},
        {"type": "input_image", "image_url": "https://media.invalid"},
        {"type": "input_file", "file_id": "media-file"},
        {"type": "input_text", "text": "text", "file_id": "hidden"},
        {"type": "input_text", "text": ""},
    ],
)
def test_only_exact_nonempty_input_text_is_allowed(part):
    mapping = body()
    mapping["input"][0]["content"] = [part]
    with pytest.raises(DraftProviderError):
        request(payload=encoded(mapping))


@pytest.mark.parametrize(
    "field,value",
    [
        ("temperature", True),
        ("temperature", "0.2"),
        ("temperature", -0.1),
        ("temperature", 2.1),
        ("temperature", float("nan")),
        ("temperature", float("inf")),
        ("max_output_tokens", True),
        ("max_output_tokens", 1.5),
        ("max_output_tokens", 0),
        ("max_output_tokens", 32769),
        ("stream", False),
        ("stream", 1),
        ("store", False),
        ("store", 1),
        ("model", " different "),
        ("model", "different"),
    ],
)
def test_generation_parameters_are_exact_and_explicit(field, value):
    mapping = body()
    mapping[field] = value
    with pytest.raises(DraftProviderError):
        request(payload=encoded(mapping))


@pytest.mark.parametrize("temperature", [0, 2, 0.1, 1.25, 2.0])
def test_existing_finite_numeric_temperature_semantics_are_not_restricted(temperature):
    mapping = body()
    mapping["temperature"] = temperature
    assert request(payload=encoded(mapping)).to_provider_body()["temperature"] == temperature


def test_direct_schema_format_is_decodable_without_rewriting_legacy_bytes():
    mapping = body()
    schema = mapping["text"]["format"].pop("json_schema")
    mapping["text"]["format"].update(schema)
    assert request(payload=encoded(mapping)).to_provider_body() == mapping
    assert request().request_payload == encoded(body())


@pytest.mark.parametrize(
    "field,value",
    [
        ("strict", False),
        ("strict", 1),
        ("name", "bad name"),
        ("name", "x" * 65),
        ("schema", {}),
        ("schema", []),
    ],
)
def test_format_fields_are_closed_and_typed(field, value):
    mapping = body()
    mapping["text"]["format"]["json_schema"][field] = value
    with pytest.raises(DraftProviderError):
        request(payload=encoded(mapping))


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"{}",
        b"[]",
        b"null",
        b'{"model":"a","model":"b"}',
        b'{"a":{"x":1,"x":2}}',
        b'{"a":1e999}',
        b'{"a":"\\ud800"}',
        b'{"a":' + b"[" * 1000 + b"0" + b"]" * 1000 + b"}",
    ],
)
def test_strict_json_rejects_ambiguity_nonfinite_invalid_unicode_and_depth(raw):
    with pytest.raises(DraftProviderError):
        decode_draft_request_payload(raw)


def test_nested_schema_has_bounded_json_depth():
    mapping = body()
    nested = {"value": 0}
    for _ in range(70):
        nested = {"value": nested}
    mapping["text"]["format"]["json_schema"]["schema"] = nested
    with pytest.raises(DraftProviderError, match="bounded"):
        request(payload=encoded(mapping))


def test_request_structural_byte_ceiling_is_enforced(monkeypatch):
    raw = encoded(body())
    monkeypatch.setattr(
        "autocut_kernel.semantic_chain.draft_provider.MAX_DRAFT_REQUEST_BYTES", len(raw)
    )
    assert request(payload=raw).request_payload == raw
    with pytest.raises(DraftProviderError):
        request(payload=raw + b" ")
    with pytest.raises(DraftProviderError):
        decode_draft_request_payload(bytearray(raw))


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_payload_sha256", "sha256:" + "a" * 64),
        ("model_id", "foreign"),
        ("provider_id", ""),
        ("provider_idempotency_key", "\nkey"),
        ("on_provider_request_id", True),
    ],
)
def test_identity_hash_and_callback_are_validated(field, value):
    with pytest.raises(DraftProviderError):
        replace(request(), **{field: value})


def test_whitespace_changes_actual_hash_not_provider_body():
    compact = request()
    spaced = request(payload=json.dumps(body(), indent=2).encode())
    assert compact.to_provider_body() == spaced.to_provider_body()
    assert compact.request_payload_sha256 != spaced.request_payload_sha256


def test_port_reuses_existing_generation_result_and_reconcile_protocol():
    class FakeProvider:
        strategy_version = "test-text-v1"

        def dispatch(self, value):
            assert type(value) is DraftDispatchRequest
            return ProviderCompleted(b"untrusted draft", "response-1")

        def reconcile(self, query):
            assert type(query) is ProviderReconcileQuery
            return ProviderCompleted(b"same untrusted draft", "response-1")

    provider = FakeProvider()
    assert isinstance(provider, DraftProviderPort)
    assert type(provider.dispatch(request())) is ProviderCompleted
