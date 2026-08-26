"""Pure BUSY wire closure; a decoded report is neither a receipt nor authority."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.media.local_speech_window_busy import (
    LocalSpeechWindowBusyProof,
    decode_local_speech_window_busy_proof,
)

from tests.media.test_local_speech_window_codec import request_and_report


def _case():
    request, _ = request_and_report()
    return request, LocalSpeechWindowBusyProof(
        request.canonical_hash, request.binding_sha256, request.policy.service_profile_sha256,
    )


def test_closed_six_field_roundtrip_and_hash():
    request, proof = _case()
    raw = proof.to_bytes()
    assert decode_local_speech_window_busy_proof(raw, request) == proof
    assert set(proof.to_mapping()) == {
        "schema_version", "request_sha256", "binding_sha256", "service_profile_sha256",
        "invocation_state", "reason",
    }
    assert proof.canonical_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert json.loads(raw)["invocation_state"] == "not_started"
    with pytest.raises(FrozenInstanceError):
        proof.request_sha256 = "sha256:" + "a" * 64


@pytest.mark.parametrize("field", ["request_sha256", "binding_sha256", "service_profile_sha256"])
@pytest.mark.parametrize("invalid", [None, True, 1, 1.0, [], {}, "bad", "\ud800"])
def test_direct_hashes_require_exact_hash_text(field, invalid):
    _, proof = _case()
    with pytest.raises(ValueError):
        replace(proof, **{field: invalid})


@pytest.mark.parametrize("field", ["request_sha256", "binding_sha256", "service_profile_sha256"])
def test_each_foreign_identity_is_rejected_even_with_canonical_bytes(field):
    request, proof = _case()
    foreign = replace(proof, **{field: "sha256:" + "f" * 64})
    with pytest.raises(ValueError, match="exact window request"):
        decode_local_speech_window_busy_proof(foreign.to_bytes(), request)


@pytest.mark.parametrize("field", [
    "schema_version", "request_sha256", "binding_sha256", "service_profile_sha256",
    "invocation_state", "reason",
])
@pytest.mark.parametrize("invalid", [None, False, 1, 1.0, [], {}])
def test_all_wire_fields_are_exact_strings(field, invalid):
    request, proof = _case()
    body = proof.to_mapping()
    body[field] = invalid
    with pytest.raises(ValueError):
        decode_local_speech_window_busy_proof(json.dumps(body).encode(), request)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "local-speech-window-busy-v2"),
    ("reason", "inference_failed"), ("invocation_state", "started"),
])
def test_fixed_claims_cannot_be_changed(field, value):
    request, proof = _case()
    body = proof.to_mapping()
    body[field] = value
    with pytest.raises(ValueError):
        decode_local_speech_window_busy_proof(json.dumps(body).encode(), request)


def test_every_missing_and_extra_field_is_rejected():
    request, proof = _case()
    for field in proof.to_mapping():
        body = proof.to_mapping()
        del body[field]
        with pytest.raises(ValueError):
            decode_local_speech_window_busy_proof(json.dumps(body).encode(), request)
    body = {**proof.to_mapping(), "retry_authorized": True}
    with pytest.raises(ValueError):
        decode_local_speech_window_busy_proof(json.dumps(body).encode(), request)


@pytest.mark.parametrize("kind", ["duplicate", "spaces", "newline", "reordered", "nan", "overflow"])
def test_strict_parser_and_canonical_bytes(kind):
    request, proof = _case()
    raw = proof.to_bytes()
    if kind == "duplicate":
        raw = raw[:-1] + b',"reason":"admission_busy"}'
        assert json.loads(raw) == proof.to_mapping()
    elif kind == "spaces":
        raw = json.dumps(proof.to_mapping(), sort_keys=True).encode()
    elif kind == "newline":
        raw += b"\n"
    elif kind == "reordered":
        raw = json.dumps(proof.to_mapping(), separators=(",", ":")).encode()
    else:
        raw = raw.replace(b'"admission_busy"', b"NaN" if kind == "nan" else b"1e999")
    with pytest.raises(ValueError):
        decode_local_speech_window_busy_proof(raw, request)


@pytest.mark.parametrize("raw", [b"", b"\xff", b"null", b"[]", b"true", b"{}", "{}", bytearray(b"{}")])
def test_nonproof_input_cannot_authorize_retry(raw):
    request, _ = _case()
    with pytest.raises(ValueError):
        decode_local_speech_window_busy_proof(raw, request)


def test_explicit_byte_bound_before_parsing_and_exact_limit_allowed():
    request, proof = _case()
    length = len(proof.to_bytes())
    request = replace(request, max_response_bytes=length)
    proof = replace(proof, request_sha256=request.canonical_hash)
    assert len(proof.to_bytes()) == length
    assert decode_local_speech_window_busy_proof(proof.to_bytes(), request) == proof
    request = replace(request, max_response_bytes=length - 1)
    proof = replace(proof, request_sha256=request.canonical_hash)
    with pytest.raises(ValueError, match="byte limit"):
        decode_local_speech_window_busy_proof(proof.to_bytes(), request)


def test_request_itself_must_be_exact_typed_identity():
    request, proof = _case()
    with pytest.raises(ValueError):
        decode_local_speech_window_busy_proof(proof.to_bytes(), request.to_mapping())
