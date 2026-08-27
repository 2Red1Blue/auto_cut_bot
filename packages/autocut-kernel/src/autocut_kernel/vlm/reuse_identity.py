"""Versioned semantic compatibility projections, never reuse authorization.

Only the current closed video-request parameter contract is supported. The
caller must verify that model IDs name immutable versions and that the supplied
provider scope is the original frozen origin/tenant/project identity. Neither a
matching fingerprint nor a Protocol implementation proves those authorities.

The registered prompt/adapter/parser versions own template rendering, aliases,
and adapter-added instructions. Exact rendered prompt bytes (including any
background or character context) remain a per-request dependency. New adapters
with additional messages or external context need an explicit identity version;
they cannot silently project those dependencies away here.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Protocol, cast, runtime_checkable

from ..media.types import canonical_sha256, sha256_prefixed
from .models import VlmParsePolicy, VlmRequestIdentity, VlmValidationError
from .semantic_contracts import VLM_PARSER_V4, parser_contract_sha256_for
from .window import WindowManifest, WindowManifestSet


@runtime_checkable
class VlmProviderScopeFacts(Protocol):
    @property
    def provider_scope_fingerprint(self) -> str: ...


@runtime_checkable
class VlmReuseRequestFacts(Protocol):
    """Read-only portion of the existing generation request; no runtime import."""

    @property
    def manifest(self) -> WindowManifest: ...
    @property
    def manifest_set(self) -> WindowManifestSet: ...
    @property
    def prompt_template(self) -> str: ...
    @property
    def prompt_version(self) -> str: ...
    @property
    def response_schema_json(self) -> str: ...
    @property
    def request_parameters_json(self) -> str: ...
    @property
    def model_id(self) -> str: ...
    @property
    def provider_id(self) -> str: ...
    @property
    def parse_policy(self) -> VlmParsePolicy: ...
    @property
    def parser_strategy_version(self) -> str: ...
    @property
    def parser_contract_sha256(self) -> str | None: ...
    @property
    def source_provenance_sha256(self) -> str | None: ...
    @property
    def source_manifest_sha256(self) -> str | None: ...
    @property
    def episode_index(self) -> int: ...
    @property
    def request_identity(self) -> VlmRequestIdentity: ...
    @property
    def request_payload(self) -> bytes: ...


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise VlmValidationError(f"{name} must be explicit non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise VlmValidationError(f"{name} must be valid UTF-8 text") from error
    return value


def _hash(value: object, name: str) -> str:
    if type(value) is not str:  # noqa: E721
        raise VlmValidationError(f"{name} must be an explicit sha256 identity")
    return sha256_prefixed(value, name)


def _bytes_hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VlmValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise VlmValidationError(f"non-finite JSON number: {value}")


def _json_text(value: object) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        encoded.encode("utf-8", errors="strict")
        return encoded
    except (TypeError, ValueError) as error:
        raise VlmValidationError("identity requires finite JSON values") from error


def _json_object(value: str, name: str) -> dict[str, object]:
    _text(value, name)
    try:
        parsed: object = json.loads(
            value, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant,
        )
    except (TypeError, ValueError) as error:
        raise VlmValidationError(f"{name} must be strict finite JSON") from error
    if type(parsed) is not dict:  # noqa: E721
        raise VlmValidationError(f"{name} must be a JSON object")
    # JSON decoding plus the exact dict check establishes string keys.
    result = cast(dict[str, object], parsed)
    _json_text(result)  # Also rejects exponent overflow (for example 1e999).
    return result


def _parameters(value: str) -> dict[str, object]:
    result = _json_object(value, "request_parameters_json")
    adapter_version = _text(result.get("adapter_strategy_version"), "adapter_strategy_version")
    # This is a wire-contract discriminator, not a dependency on a runtime
    # adapter. Historical four-field identities retain their original bytes.
    explicit_thinking = adapter_version == "doubao-ark-files-responses-stream-v5"
    expected_fields = {"adapter_strategy_version", "max_output_tokens", "temperature", "video_fps"}
    if explicit_thinking:
        expected_fields.add("thinking_type")
    if set(result) != expected_fields:
        raise VlmValidationError("VLM reuse v1 requires the complete closed request parameters")
    if explicit_thinking:
        thinking_type = result["thinking_type"]
        if type(thinking_type) is not str or thinking_type not in {"enabled", "disabled", "auto"}:  # noqa: E721
            raise VlmValidationError("thinking_type must be an explicit enabled, disabled, or auto mode")
    tokens = result["max_output_tokens"]
    if type(tokens) is not int or not 1 <= tokens <= 32768:  # noqa: E721
        raise VlmValidationError("max_output_tokens must be an integer between 1 and 32768")
    for name, minimum, maximum in (("video_fps", 0.1, 10), ("temperature", 0, 2)):
        numeric_value = result[name]
        if type(numeric_value) not in (int, float):
            raise VlmValidationError(f"{name} must be an explicit finite number")
        # The exact numeric type check rejects booleans and non-JSON numbers.
        number = cast(int | float, numeric_value)
        if not minimum <= number <= maximum or not math.isfinite(number):
            raise VlmValidationError(f"{name} is outside the registered request range")
        result[name] = float(number)
    return result


@dataclass(frozen=True, slots=True)
class VlmSemanticPolicyIdentityV1:
    """Explicit stage-local policy, separate from rendered episode context.

    Original request/profile hashes are not rewritten. Transport retry budgets,
    worker scheduling, paths, and run IDs do not define successful semantics.
    The byte-bearing inputs here, not caller-asserted component hashes, produce
    the canonical compatibility projection.
    """

    prompt_template: str
    prompt_version: str
    response_schema_json: str
    request_parameters_json: str
    model_id: str
    provider_id: str
    provider_scope_fingerprint: str
    parse_policy: VlmParsePolicy
    parser_strategy_version: str
    preprocess_policy_sha256: str
    window_sampling_policy_sha256: str
    parser_contract_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("prompt_template", "prompt_version", "model_id", "provider_id", "parser_strategy_version"):
            _text(getattr(self, name), name)
        for name in ("provider_scope_fingerprint", "preprocess_policy_sha256", "window_sampling_policy_sha256"):
            _hash(getattr(self, name), name)
        if type(self.parse_policy) is not VlmParsePolicy:  # noqa: E721
            raise VlmValidationError("parse_policy must be an exact VlmParsePolicy")
        object.__setattr__(
            self, "response_schema_json", _json_text(_json_object(self.response_schema_json, "response_schema_json")),
        )
        object.__setattr__(self, "request_parameters_json", _json_text(_parameters(self.request_parameters_json)))
        # Historical v3 compatibility projections have no implementation field;
        # preserve their exact bytes. The new v4 contract explicitly binds its
        # installed implementation, including the shared frozen v3 helpers.
        if self.parser_strategy_version == VLM_PARSER_V4:
            _hash(self.parser_contract_sha256, "parser_contract_sha256")
            if self.parser_contract_sha256 != parser_contract_sha256_for(self.parser_strategy_version):
                raise VlmValidationError("frozen V4 parser implementation differs from the registered parser")
        elif self.parser_contract_sha256 is not None:
            raise VlmValidationError("legacy semantic policy cannot claim a parser implementation field")

    @classmethod
    def from_request(
        cls,
        request: VlmReuseRequestFacts,
        *,
        provider_scope: VlmProviderScopeFacts,
        prompt_template: str,
    ) -> VlmSemanticPolicyIdentityV1:
        _verified_request(request)
        if not isinstance(provider_scope, VlmProviderScopeFacts):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise VlmValidationError("provider_scope must supply the original frozen scope fingerprint")
        _text(prompt_template, "prompt_template")
        if not request.prompt_template.startswith(prompt_template):
            raise VlmValidationError("rendered prompt must include the exact static prompt template prefix")
        return cls(
            prompt_template=prompt_template,
            prompt_version=request.prompt_version,
            response_schema_json=request.response_schema_json,
            request_parameters_json=request.request_parameters_json,
            model_id=request.model_id,
            provider_id=request.provider_id,
            provider_scope_fingerprint=provider_scope.provider_scope_fingerprint,
            parse_policy=request.parse_policy,
            parser_strategy_version=request.parser_strategy_version,
            parser_contract_sha256=request.parser_contract_sha256,
            preprocess_policy_sha256=request.manifest.preprocess_policy_sha256,
            window_sampling_policy_sha256=request.manifest.window_sampling_policy_sha256,
        )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": "VlmSemanticPolicyIdentity/v1",
            "prompt_template_sha256": _bytes_hash(self.prompt_template.encode("utf-8")),
            "prompt_version": self.prompt_version,
            "response_schema_sha256": _bytes_hash(self.response_schema_json.encode("utf-8")),
            "request_parameters": _parameters(self.request_parameters_json),
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "provider_scope_fingerprint": self.provider_scope_fingerprint,
            "parse_policy": self.parse_policy.to_mapping(),
            "parser_strategy_version": self.parser_strategy_version,
            "preprocess_policy_sha256": self.preprocess_policy_sha256,
            "window_sampling_policy_sha256": self.window_sampling_policy_sha256,
        }
        if self.parser_contract_sha256 is not None:
            result["parser_contract_sha256"] = self.parser_contract_sha256
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _verified_request(request: VlmReuseRequestFacts) -> VlmRequestIdentity:
    if not isinstance(request, VlmReuseRequestFacts):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise VlmValidationError("reuse requires complete typed VLM request facts")
    if type(request.request_identity) is not VlmRequestIdentity:  # noqa: E721
        raise VlmValidationError("reuse requires an exact VlmRequestIdentity")
    _hash(request.source_provenance_sha256, "source_provenance_sha256")
    _hash(request.source_manifest_sha256, "source_manifest_sha256")
    if type(request.episode_index) is not int or request.episode_index < 0:  # noqa: E721
        raise VlmValidationError("episode_index must be a non-negative integer")
    if type(request.request_payload) is not bytes:  # noqa: E721
        raise VlmValidationError("request_payload must be exact bytes")
    _text(request.prompt_template, "rendered prompt")
    _text(request.parser_strategy_version, "parser_strategy_version")
    parameters = _json_object(request.request_parameters_json, "request_parameters_json")
    schema = _json_object(request.response_schema_json, "response_schema_json")
    expected = VlmRequestIdentity.from_manifest(
        request.manifest, request.manifest_set,
        prompt_template_sha256=_bytes_hash(request.prompt_template.encode("utf-8")),
        prompt_version=request.prompt_version,
        response_schema_sha256=_bytes_hash(_json_text(schema).encode("utf-8")),
        model_id=request.model_id, provider_id=request.provider_id,
        request_parameters_sha256=_bytes_hash(_json_text(parameters).encode("utf-8")),
        request_payload_sha256=_bytes_hash(request.request_payload),
        parse_policy=request.parse_policy,
    )
    if expected != request.request_identity:
        raise VlmValidationError("origin request identity does not match exact request facts")
    payload = _json_object(request.request_payload.decode("utf-8"), "request_payload")
    semantic_payload = {
        "model_id": request.model_id,
        "provider_id": request.provider_id,
        "prompt": request.prompt_template,
        "prompt_version": request.prompt_version,
        "response_schema": schema,
        "request_parameters": parameters,
        "parse_policy": request.parse_policy.to_mapping(),
        "parser_strategy_version": request.parser_strategy_version,
        "proxy_blob": request.manifest.proxy_blob_ref.to_mapping(),
        "window_manifest_sha256": request.manifest.canonical_hash,
        "window_manifest_set_sha256": request.manifest_set.canonical_hash,
    }
    if request.parser_strategy_version == VLM_PARSER_V4:
        parser_digest = _hash(request.parser_contract_sha256, "parser_contract_sha256")
        if parser_digest != parser_contract_sha256_for(request.parser_strategy_version):
            raise VlmValidationError("origin V4 request binds a different parser implementation")
        semantic_payload["parser_contract_sha256"] = parser_digest
    elif request.parser_contract_sha256 is not None:
        raise VlmValidationError("legacy request cannot claim a parser implementation field")
    if set(payload) != set(semantic_payload) | {"retry_policy", "retry_policy_sha256"}:
        raise VlmValidationError("origin request payload fields are not closed")
    if any(payload[name] != value for name, value in semantic_payload.items()):
        raise VlmValidationError("origin request payload differs from exact semantic facts")
    return expected


@dataclass(frozen=True, slots=True)
class VlmReuseIdentityV1:
    """Per-episode exact-input identity; original producer evidence stays separate.

    There is intentionally no hash-only ``from_mapping`` admission path. A
    persisted projection must be rebuilt from verified immutable request facts.
    Callers still need source authorization, successful producer closure, and
    reachable content proofs before they can reuse any result.
    """

    semantic_policy: VlmSemanticPolicyIdentityV1
    manifest: WindowManifest
    manifest_set: WindowManifestSet
    rendered_prompt: str
    source_provenance_sha256: str
    source_manifest_sha256: str
    episode_index: int
    origin_request_identity: VlmRequestIdentity = field(compare=False)
    origin_request_payload: bytes = field(compare=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.semantic_policy) is not VlmSemanticPolicyIdentityV1:  # noqa: E721
            raise VlmValidationError("semantic_policy must be an exact VlmSemanticPolicyIdentityV1")
        if type(self.origin_request_identity) is not VlmRequestIdentity:  # noqa: E721
            raise VlmValidationError("origin_request_identity must be an exact VlmRequestIdentity")
        self.origin_request_identity.assert_manifest_binding(self.manifest, self.manifest_set)
        _hash(self.source_provenance_sha256, "source_provenance_sha256")
        _hash(self.source_manifest_sha256, "source_manifest_sha256")
        _text(self.rendered_prompt, "rendered_prompt")
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise VlmValidationError("episode_index must be a non-negative integer")
        if (
            not self.rendered_prompt.startswith(self.semantic_policy.prompt_template)
            or _bytes_hash(self.rendered_prompt.encode("utf-8"))
            != self.origin_request_identity.prompt_template_sha256
        ):
            raise VlmValidationError("rendered prompt must bind the original request and template")
        policy = self.semantic_policy
        identity = self.origin_request_identity
        if (
            type(self.origin_request_payload) is not bytes  # noqa: E721
            or _bytes_hash(self.origin_request_payload) != identity.request_payload_sha256
        ):
            raise VlmValidationError("origin request payload must match the original request identity")
        payload = _json_object(self.origin_request_payload.decode("utf-8"), "origin_request_payload")
        parameters_json = _json_text(payload.get("request_parameters"))
        expected_payload = {
            "model_id": policy.model_id,
            "provider_id": policy.provider_id,
            "prompt": self.rendered_prompt,
            "prompt_version": policy.prompt_version,
            "response_schema": _json_object(policy.response_schema_json, "response_schema_json"),
            "request_parameters": _json_object(parameters_json, "request_parameters_json"),
            "parse_policy": policy.parse_policy.to_mapping(),
            "parser_strategy_version": policy.parser_strategy_version,
            "proxy_blob": self.manifest.proxy_blob_ref.to_mapping(),
            "window_manifest_sha256": self.manifest.canonical_hash,
            "window_manifest_set_sha256": self.manifest_set.canonical_hash,
        }
        if policy.parser_contract_sha256 is not None:
            expected_payload["parser_contract_sha256"] = policy.parser_contract_sha256
        if (
            set(payload) != set(expected_payload) | {"retry_policy", "retry_policy_sha256"}
            or any(payload[name] != value for name, value in expected_payload.items())
        ):
            raise VlmValidationError("origin request payload does not match exact semantic policy facts")
        if (
            identity.prompt_version != policy.prompt_version
            or identity.model_id != policy.model_id or identity.provider_id != policy.provider_id
            or identity.response_schema_sha256 != _bytes_hash(policy.response_schema_json.encode("utf-8"))
            or identity.parse_policy_sha256 != policy.parse_policy.canonical_hash
            or identity.preprocess_policy_sha256 != policy.preprocess_policy_sha256
            or identity.window_sampling_policy_sha256 != policy.window_sampling_policy_sha256
            or identity.request_parameters_sha256 != _bytes_hash(parameters_json.encode("utf-8"))
            or _parameters(parameters_json) != _parameters(policy.request_parameters_json)
            or payload.get("parser_strategy_version") != policy.parser_strategy_version
        ):
            raise VlmValidationError("semantic policy does not match origin request identity")

    @classmethod
    def from_request(
        cls,
        request: VlmReuseRequestFacts,
        *,
        semantic_policy: VlmSemanticPolicyIdentityV1,
    ) -> VlmReuseIdentityV1:
        origin_identity = _verified_request(request)
        if type(semantic_policy) is not VlmSemanticPolicyIdentityV1:  # noqa: E721
            raise VlmValidationError("semantic_policy must be an exact VlmSemanticPolicyIdentityV1")
        expected_policy = VlmSemanticPolicyIdentityV1.from_request(
            request, provider_scope=semantic_policy, prompt_template=semantic_policy.prompt_template,
        )
        if expected_policy != semantic_policy:
            raise VlmValidationError("semantic policy does not match exact request facts")
        # _verified_request rejected null or non-string provenance above.
        provenance = _hash(request.source_provenance_sha256, "source_provenance_sha256")
        source_manifest = _hash(request.source_manifest_sha256, "source_manifest_sha256")
        return cls(
            semantic_policy, request.manifest, request.manifest_set, request.prompt_template,
            provenance, source_manifest, request.episode_index,
            origin_identity, request.request_payload,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": "VlmReuseIdentity/v1",
            "semantic_policy_sha256": self.semantic_policy.canonical_hash,
            "source_provenance_sha256": self.source_provenance_sha256,
            "source_manifest_sha256": self.source_manifest_sha256,
            "episode_index": self.episode_index,
            "source_id": self.manifest.source_id,
            "source_sha256": self.manifest.source_sha256,
            "window_manifest_sha256": self.manifest.canonical_hash,
            "window_manifest_set_sha256": self.manifest_set.canonical_hash,
            "timeline_map_sha256": self.manifest.timeline_map.canonical_hash,
            "frame_samples_sha256": self.manifest.frame_samples_sha256,
            "frame_pts_index_set_sha256": self.manifest.frame_pts_index_set_sha256,
            "rendered_prompt_sha256": _bytes_hash(self.rendered_prompt.encode("utf-8")),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())
