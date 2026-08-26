"""Request-bound pre-dispatch BUSY reports, not attestations or durable receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from .local_speech_window import LocalSpeechWindowError, LocalSpeechWindowRequest
from .root_evidence_codec import decode_media_evidence_json
from .types import canonical_sha256, sha256_prefixed


@dataclass(frozen=True, slots=True)
class LocalSpeechWindowBusyProof:
    """A trusted service reports admission refusal before starting inference.

    Constructing or decoding this value confers neither service authentication
    nor claim ownership. A durable adapter must establish those separately.
    """

    request_sha256: str
    binding_sha256: str
    service_profile_sha256: str

    def __post_init__(self) -> None:
        for name in ("request_sha256", "binding_sha256", "service_profile_sha256"):
            value = getattr(self, name)
            if type(value) is not str:
                raise LocalSpeechWindowError("busy proof hashes must be exact text")
            sha256_prefixed(value, name)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "local-speech-window-busy-v1",
            "invocation_state": "not_started",
            "reason": "admission_busy",
            "request_sha256": self.request_sha256,
            "binding_sha256": self.binding_sha256,
            "service_profile_sha256": self.service_profile_sha256,
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def assert_matches(self, request: LocalSpeechWindowRequest) -> None:
        if type(request) is not LocalSpeechWindowRequest:
            raise LocalSpeechWindowError("busy proof requires an exact window request")
        if (
            self.request_sha256 != request.canonical_hash
            or self.binding_sha256 != request.binding_sha256
            or self.service_profile_sha256 != request.policy.service_profile_sha256
        ):
            raise LocalSpeechWindowError("busy proof does not bind the exact window request")


def decode_local_speech_window_busy_proof(
    raw: bytes, request: LocalSpeechWindowRequest,
) -> LocalSpeechWindowBusyProof:
    """Decode canonical bounded bytes and independently compare all identities."""
    if type(request) is not LocalSpeechWindowRequest:
        raise LocalSpeechWindowError("busy proof requires an exact window request")
    value = decode_media_evidence_json(raw, max_bytes=request.max_response_bytes)
    fields = {
        "schema_version", "invocation_state", "reason", "request_sha256",
        "binding_sha256", "service_profile_sha256",
    }
    if type(value) is not dict or set(cast(dict[object, object], value)) != fields:
        raise LocalSpeechWindowError("busy proof must have exactly six declared fields")
    body = cast(dict[str, object], value)
    if any(type(item) is not str for item in body.values()):
        raise LocalSpeechWindowError("busy proof fields must be exact text")
    if (
        body["schema_version"] != "local-speech-window-busy-v1"
        or body["invocation_state"] != "not_started"
        or body["reason"] != "admission_busy"
    ):
        raise LocalSpeechWindowError("busy proof is not a pre-dispatch admission refusal")
    proof = LocalSpeechWindowBusyProof(
        cast(str, body["request_sha256"]), cast(str, body["binding_sha256"]),
        cast(str, body["service_profile_sha256"]),
    )
    proof.assert_matches(request)
    if raw != proof.to_bytes():
        raise LocalSpeechWindowError("busy proof bytes must be canonical")
    return proof
