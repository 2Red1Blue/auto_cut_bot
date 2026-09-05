"""Text-only provider side effects, not a durable request or semantic authority.

The payload is the explicit Responses request body. It is *not* the Command's
durable envelope (which separately owns retry policy, inputs and parser hashes).
Actual UTF-8 bytes are hashed: finite provider numbers are not compiler-JCS.
The Command must audit returned bytes before independently parsing the draft.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from ..vlm.provider_port import (
    ProviderReconcileQuery,
    ProviderRequestIdCallback,
    ProviderResult,
)

# Structural safety ceiling, not an implicit model/prompt budget. The Command
# freezes its tighter prompt policy; adapters require an explicit byte budget.
MAX_DRAFT_REQUEST_BYTES = 16 * 1024 * 1024
DRAFT_LEGACY_ADAPTER_STRATEGY_VERSION = "doubao-ark-text-responses-stream-v1"
DRAFT_DIRECT_SCHEMA_ADAPTER_STRATEGY_VERSION = "doubao-ark-text-responses-stream-v2"
DRAFT_SUPPORTED_ADAPTER_STRATEGY_VERSIONS = frozenset({
    DRAFT_LEGACY_ADAPTER_STRATEGY_VERSION, DRAFT_DIRECT_SCHEMA_ADAPTER_STRATEGY_VERSION,
})
_MAX_DEPTH = 64
_FIELDS = {"model", "input", "text", "max_output_tokens", "thinking", "temperature", "stream", "store"}


class DraftProviderError(ValueError):
    """Malformed or identity-unbound text request, before provider I/O."""


def _text(value: object, name: str, *, identifier: bool = False) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise DraftProviderError(f"{name} must be non-empty text")
    if identifier and (
        len(value) > 256
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise DraftProviderError(f"{name} must be a bounded canonical identifier")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise DraftProviderError(f"{name} must be valid UTF-8") from error
    return value


def _closed(value: object, fields: set[str], name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise DraftProviderError(f"{name} has missing or unknown fields")
    mapping = cast(dict[str, object], value)
    if set(mapping) != fields:
        raise DraftProviderError(f"{name} has missing or unknown fields")
    return mapping


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DraftProviderError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise DraftProviderError("nonfinite JSON numbers are forbidden")


def _json_values(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > _MAX_DEPTH:
            raise DraftProviderError("draft request JSON is too deep")
        if isinstance(item, dict):
            mapping = cast(dict[str, object], item)
            for key, child in mapping.items():
                key.encode("utf-8", errors="strict")
                pending.append((child, depth + 1))
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in cast(list[object], item))
        elif isinstance(item, str):
            item.encode("utf-8", errors="strict")
        elif type(item) is float and not math.isfinite(item):  # noqa: E721
            raise DraftProviderError("nonfinite JSON numbers are forbidden")


def decode_draft_text_format(
    text: object, *, adapter_strategy_version: str | None = None,
) -> dict[str, object]:
    """Validate a registered wire shape without rewriting persisted request bytes."""
    config = _closed(text, {"format"}, "text configuration")
    value = config["format"]
    if type(value) is not dict:  # noqa: E721
        raise DraftProviderError("format must be an exact object")
    mapping = cast(dict[str, object], value)
    if set(mapping) == {"type", "json_schema"}:
        strategy = DRAFT_LEGACY_ADAPTER_STRATEGY_VERSION
        descriptor = _closed(mapping["json_schema"], {"name", "strict", "schema"}, "json_schema")
    else:
        strategy = DRAFT_DIRECT_SCHEMA_ADAPTER_STRATEGY_VERSION
        _closed(mapping, {"type", "name", "strict", "schema"}, "format")
        descriptor = {key: mapping[key] for key in ("name", "strict", "schema")}
    if adapter_strategy_version is not None and adapter_strategy_version != strategy:
        raise DraftProviderError("draft format differs from registered adapter strategy")
    if mapping["type"] != "json_schema":
        raise DraftProviderError("draft format must be explicit json_schema")
    name = _text(descriptor["name"], "schema name")
    if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) is None or descriptor["strict"] is not True:
        raise DraftProviderError("draft schema name and strict mode must be explicit")
    if type(descriptor["schema"]) is not dict or not descriptor["schema"]:  # noqa: E721
        raise DraftProviderError("response schema must be a non-empty JSON object")
    return descriptor


def build_draft_text_format(
    adapter_strategy_version: str, schema_name: str, response_schema: dict[str, object],
) -> dict[str, object]:
    """Choose the frozen policy's wire version, never the current SDK annotation."""
    descriptor: dict[str, object] = {"name": schema_name, "strict": True, "schema": response_schema}
    if adapter_strategy_version == DRAFT_LEGACY_ADAPTER_STRATEGY_VERSION:
        result = {"format": {"type": "json_schema", "json_schema": descriptor}}
    elif adapter_strategy_version == DRAFT_DIRECT_SCHEMA_ADAPTER_STRATEGY_VERSION:
        result = {"format": {"type": "json_schema", **descriptor}}
    else:
        raise DraftProviderError("unregistered draft adapter strategy")
    decode_draft_text_format(result, adapter_strategy_version=adapter_strategy_version)
    return cast(dict[str, object], result)


def decode_draft_request_payload(raw: bytes) -> dict[str, object]:
    """Decode a fresh, closed SDK text body; no model output or schema is trusted."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_DRAFT_REQUEST_BYTES:  # noqa: E721
        raise DraftProviderError("draft request must be bounded exact bytes")
    try:
        value: object = json.loads(
            raw.decode("utf-8", errors="strict"), object_pairs_hook=_pairs, parse_constant=_constant
        )
        _json_values(value)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise DraftProviderError("draft request must be strict bounded UTF-8 JSON") from error
    body = _closed(value, _FIELDS, "draft request")
    _text(body["model"], "model", identifier=True)
    messages = body["input"]
    if type(messages) is not list or len(cast(list[object], messages)) != 1:  # noqa: E721
        raise DraftProviderError("draft input requires exactly one text-only user message")
    message = _closed(cast(list[object], messages)[0], {"role", "content"}, "message")
    if message["role"] != "user":
        raise DraftProviderError("draft message role must be user")
    parts = message["content"]
    if type(parts) is not list or len(cast(list[object], parts)) != 1:  # noqa: E721
        raise DraftProviderError("draft message requires exactly one input_text")
    part = _closed(cast(list[object], parts)[0], {"type", "text"}, "text content")
    if part["type"] != "input_text":
        raise DraftProviderError("draft input must not contain media or file references")
    _text(part["text"], "prompt")
    decode_draft_text_format(body["text"])
    tokens = body["max_output_tokens"]
    if type(tokens) is not int or not 1 <= tokens <= 32768:  # noqa: E721
        raise DraftProviderError("max_output_tokens must be an exact integer from 1 to 32768")
    reasoning = body["thinking"]
    if type(reasoning) is not dict or set(reasoning) != {"type"} or reasoning.get("type") != "disabled":  # noqa: E721
        raise DraftProviderError("thinking must be exactly {\"type\": \"disabled\"}")
    temperature = body["temperature"]
    if type(temperature) not in (int, float) or not 0 <= cast(int | float, temperature) <= 2:
        raise DraftProviderError("temperature must be an explicit finite number from 0 to 2")
    if body["stream"] is not True or body["store"] is not True:
        raise DraftProviderError("draft dispatch requires streaming and stored reconciliation")
    return body


@dataclass(frozen=True, slots=True)
class DraftDispatchRequest:
    provider_id: str
    model_id: str
    provider_idempotency_key: str
    request_payload: bytes
    request_payload_sha256: str
    on_provider_request_id: ProviderRequestIdCallback | None = None

    def __post_init__(self) -> None:
        self.to_provider_body()

    def to_provider_body(self) -> dict[str, object]:
        """Revalidate exact bytes and return a fresh SDK body, never a capability."""
        for name in ("provider_id", "model_id", "provider_idempotency_key"):
            _text(getattr(self, name), name, identifier=True)
        body = decode_draft_request_payload(self.request_payload)
        if body["model"] != self.model_id:
            raise DraftProviderError("draft model does not match dispatch identity")
        if type(self.request_payload_sha256) is not str or (  # noqa: E721
            self.request_payload_sha256
            != "sha256:" + hashlib.sha256(self.request_payload).hexdigest()
        ):
            raise DraftProviderError("draft request payload hash mismatch")
        if self.on_provider_request_id is not None and not callable(self.on_provider_request_id):
            raise DraftProviderError("provider request-ID callback must be callable")
        return body


@runtime_checkable
class DraftProviderPort(Protocol):
    @property
    def strategy_version(self) -> str: ...

    def dispatch(self, request: DraftDispatchRequest) -> ProviderResult: ...

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult: ...
