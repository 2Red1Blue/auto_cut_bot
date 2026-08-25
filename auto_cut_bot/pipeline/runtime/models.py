"""Closed request and status values for the pipeline run control plane."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import InitVar, dataclass
from typing import TYPE_CHECKING, Literal, Mapping, cast
from uuid import UUID

from .errors import PipelineRunValidationError

if TYPE_CHECKING:
    from autocut_kernel.vlm import GenerationRetryPolicy

    from auto_cut_bot.pipeline.media_preflight import LocalMediaPreflightPolicy
    from auto_cut_bot.pipeline.vlm.request_factory import DoubaoVlmRequestPolicy

PipelineProfile = Literal["test", "shadow"]
PipelineRunStatus = Literal["accepted", "running", "succeeded", "denied", "failed"]
PipelineCommandStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "denied",
    "failed",
    "indeterminate",
    "blocked",
]
PipelineStageOutcome = Literal["succeeded", "denied", "failed", "indeterminate"]
PipelineExecutionProfileKind = Literal["doubao_vlm", "legacy_unresolved"]

_RUN_ID = re.compile(r"pipeline_run_[0-9a-f]{32}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def validate_run_id(run_id: str) -> None:
    if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:  # noqa: E721
        raise PipelineRunValidationError("run_id must be pipeline_run_<32 lowercase-hex>")


def validate_idempotency_key(idempotency_key: str) -> None:
    if (
        type(idempotency_key) is not str  # noqa: E721
        or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
    ):
        raise PipelineRunValidationError(
            "Idempotency-Key must contain 1-128 letters, digits, '.', '_', ':' or '-'"
        )


def _required_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise PipelineRunValidationError(f"{field_name} must be a non-empty string")
    return value


def _profile_text(value: object, field_name: str) -> str:
    result = _required_text(value, field_name)
    if (
        result != result.strip()
        or len(result) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        raise PipelineRunValidationError(
            f"{field_name} must be canonical text of at most 256 characters"
        )
    try:
        result.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise PipelineRunValidationError(f"{field_name} must contain valid Unicode") from error
    return result


class _DuplicateJsonKeyError(ValueError):
    pass


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _canonical_json(value: object) -> str:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        rendered.encode("utf-8", errors="strict")
        return rendered
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise PipelineRunValidationError("execution profile contains invalid JSON") from error


def _decode_canonical_json(raw: object, field_name: str) -> dict[str, object]:
    if type(raw) is not str:  # noqa: E721
        raise PipelineRunValidationError(f"{field_name} must be canonical JSON text")
    try:
        value = cast(
            object,
            json.loads(
                raw,
                object_pairs_hook=_closed_json_object,
                parse_constant=_reject_json_constant,
            ),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        raise PipelineRunValidationError(f"{field_name} must be strict canonical JSON") from error
    if type(value) is not dict:  # noqa: E721
        raise PipelineRunValidationError(f"{field_name} must encode a JSON object")
    result = cast(dict[str, object], value)
    if _canonical_json(result) != raw:
        raise PipelineRunValidationError(f"{field_name} must be canonical JSON")
    return result


def _require_closed_response_schema(schema: dict[str, object]) -> None:
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise PipelineRunValidationError("response_schema_json must be a closed object schema")

    def visit(value: object) -> None:
        if type(value) is dict:  # noqa: E721
            node = cast(dict[str, object], value)
            if "properties" in node and type(node["properties"]) is not dict:  # noqa: E721
                raise PipelineRunValidationError(
                    "response_schema_json properties must be an object"
                )
            if node.get("type") == "object" and node.get("additionalProperties") is not False:
                raise PipelineRunValidationError(
                    "response_schema_json contains an open object schema"
                )
            for member in node.values():
                visit(member)
        elif type(value) is list:  # noqa: E721
            for member in cast(list[object], value):
                visit(member)

    visit(schema)


_REQUEST_PARAMETER_FIELDS = frozenset(
    {"adapter_strategy_version", "max_output_tokens", "temperature", "video_fps"}
)
_PARSE_POLICY_FIELDS = frozenset(
    {
        "max_response_bytes",
        "max_entities",
        "max_facts",
        "max_events",
        "max_candidate_hypotheses",
        "max_temporal_segments",
        "max_measurements",
        "max_text_characters",
        "max_total_text_characters",
    }
)
_LEGACY_PARSE_POLICY_FIELDS = frozenset(
    {
        "max_observations",
        "max_response_bytes",
        "max_summary_characters",
        "max_total_summary_characters",
        "minimum_confidence",
    }
)
_EXECUTION_PROFILE_SCHEMA_VERSION_V1 = "pipeline-execution-profile-v1"
_EXECUTION_PROFILE_SCHEMA_VERSION_V2 = "pipeline-execution-profile-v2"
_EXECUTION_PROFILE_SCHEMA_VERSION_V3 = "pipeline-execution-profile-v3"
_EXECUTION_PROFILE_SCHEMA_VERSION_V4 = "pipeline-execution-profile-v4"
_RETRY_POLICY_FIELDS = frozenset({"backoff_seconds", "max_attempts", "strategy_version"})
_HISTORICAL_PROFILE_READ_TOKEN = object()
_FAIL_CLOSED_BOOTSTRAP_STAGES = ("source_prep", "vlm", "media_preflight")


@dataclass(frozen=True, slots=True)
class PipelineExecutionProfile:
    """Frozen, hash-bound strategy needed to reconstruct a Doubao VLM request.

    Embedded JSON is retained as canonical immutable text.  The persisted
    profile stores those documents as JSON objects, while this value closes
    their schemas and prevents process defaults from participating in replay.
    """

    provider_id: str | None
    model_id: str | None
    adapter_strategy_version: str | None
    prompt_version: str | None
    kernel_parser_strategy_version: str | None
    response_schema_json: str | None
    request_parameters_json: str | None
    parse_policy_json: str | None
    vlm_stage_strategy_version: str | None
    generation_retry_policy_json: str | None = None
    media_preflight_policy_json: str | None = None
    media_preflight_policy_hash: str | None = None
    schema_version: str = _EXECUTION_PROFILE_SCHEMA_VERSION_V4
    kind: PipelineExecutionProfileKind = "doubao_vlm"
    _historical_read_token: InitVar[object | None] = None

    def __post_init__(self, _historical_read_token: object | None) -> None:
        if self.kind == "legacy_unresolved":
            if any(
                value is not None
                for value in (
                    self.provider_id,
                    self.model_id,
                    self.adapter_strategy_version,
                    self.prompt_version,
                    self.kernel_parser_strategy_version,
                    self.response_schema_json,
                    self.request_parameters_json,
                    self.parse_policy_json,
                    self.vlm_stage_strategy_version,
                    self.generation_retry_policy_json,
                    self.media_preflight_policy_json,
                    self.media_preflight_policy_hash,
                )
            ):
                raise PipelineRunValidationError(
                    "legacy-unresolved execution profile cannot claim a VLM strategy"
                )
            if self.schema_version != _EXECUTION_PROFILE_SCHEMA_VERSION_V1:
                raise PipelineRunValidationError(
                    "legacy-unresolved execution profile must retain schema v1"
                )
            return
        if self.kind != "doubao_vlm":
            raise PipelineRunValidationError("execution profile kind is unsupported")
        if (
            self.schema_version != _EXECUTION_PROFILE_SCHEMA_VERSION_V4
            and _historical_read_token is not _HISTORICAL_PROFILE_READ_TOKEN
        ):
            raise PipelineRunValidationError(
                "historical execution profiles can only be reconstructed from persisted mappings"
            )
        for field_name in (
            "provider_id",
            "model_id",
            "adapter_strategy_version",
            "prompt_version",
            "kernel_parser_strategy_version",
            "vlm_stage_strategy_version",
        ):
            _profile_text(getattr(self, field_name), f"execution_profile.{field_name}")
        response_schema = _decode_canonical_json(
            self.response_schema_json,
            "response_schema_json",
        )
        _require_closed_response_schema(response_schema)
        request_parameters = _decode_canonical_json(
            self.request_parameters_json,
            "request_parameters_json",
        )
        if frozenset(request_parameters) != _REQUEST_PARAMETER_FIELDS:
            raise PipelineRunValidationError(
                "request_parameters_json must match the closed Doubao parameter contract"
            )
        if request_parameters["adapter_strategy_version"] != self.adapter_strategy_version:
            raise PipelineRunValidationError(
                "request parameters must bind adapter_strategy_version"
            )
        tokens = request_parameters["max_output_tokens"]
        temperature = request_parameters["temperature"]
        video_fps = request_parameters["video_fps"]
        if type(tokens) is not int or not 1 <= tokens <= 32_768:  # noqa: E721
            raise PipelineRunValidationError("request_parameters_json.max_output_tokens is invalid")
        for value, field_name, minimum, maximum in (
            (temperature, "temperature", 0, 2),
            (video_fps, "video_fps", 0.1, 10),
        ):
            if isinstance(value, bool) or type(value) not in (int, float):
                raise PipelineRunValidationError(f"request_parameters_json.{field_name} is invalid")
            numeric_value = cast(int | float, value)
            if not minimum <= numeric_value <= maximum:
                raise PipelineRunValidationError(f"request_parameters_json.{field_name} is invalid")
        parse_policy = _decode_canonical_json(self.parse_policy_json, "parse_policy_json")
        parse_policy_fields = frozenset(parse_policy)
        if self.schema_version == _EXECUTION_PROFILE_SCHEMA_VERSION_V4:
            expected_parse_policy_fields = _PARSE_POLICY_FIELDS
        elif self.schema_version in {
            _EXECUTION_PROFILE_SCHEMA_VERSION_V1,
            _EXECUTION_PROFILE_SCHEMA_VERSION_V2,
            _EXECUTION_PROFILE_SCHEMA_VERSION_V3,
        }:
            expected_parse_policy_fields = _LEGACY_PARSE_POLICY_FIELDS
        else:
            raise PipelineRunValidationError("execution profile schema version is unsupported")
        if parse_policy_fields != expected_parse_policy_fields:
            raise PipelineRunValidationError(
                "parse_policy_json must match the closed VLM parse policy contract"
            )
        integer_fields = expected_parse_policy_fields - {"minimum_confidence"}
        for field_name in integer_fields:
            value = parse_policy[field_name]
            if type(value) is not int or value <= 0:  # noqa: E721
                raise PipelineRunValidationError(
                    f"parse_policy_json.{field_name} must be a positive integer"
                )
        per_field_budget = (
            "max_text_characters"
            if expected_parse_policy_fields == _PARSE_POLICY_FIELDS
            else "max_summary_characters"
        )
        total_budget = (
            "max_total_text_characters"
            if expected_parse_policy_fields == _PARSE_POLICY_FIELDS
            else "max_total_summary_characters"
        )
        if cast(int, parse_policy[per_field_budget]) > cast(int, parse_policy[total_budget]):
            raise PipelineRunValidationError(
                "parse policy per-field text budget exceeds its total budget"
            )
        if expected_parse_policy_fields == _LEGACY_PARSE_POLICY_FIELDS:
            confidence = parse_policy["minimum_confidence"]
            if type(confidence) is not str or confidence not in {  # noqa: E721
                "0.70",
                "0.75",
                "0.80",
                "0.85",
                "0.90",
                "0.95",
            }:
                raise PipelineRunValidationError(
                    "legacy parse_policy.minimum_confidence is invalid"
                )
        if self.schema_version == _EXECUTION_PROFILE_SCHEMA_VERSION_V1:
            if any(
                value is not None
                for value in (
                    self.generation_retry_policy_json,
                    self.media_preflight_policy_json,
                    self.media_preflight_policy_hash,
                )
            ):
                raise PipelineRunValidationError(
                    "execution profile v1 cannot claim retry or media-preflight policy"
                )
        elif self.schema_version == _EXECUTION_PROFILE_SCHEMA_VERSION_V2:
            if any(
                value is not None
                for value in (
                    self.media_preflight_policy_json,
                    self.media_preflight_policy_hash,
                )
            ):
                raise PipelineRunValidationError(
                    "execution profile v2 cannot claim a media-preflight policy"
                )
            self._decode_generation_retry_policy()
        elif self.schema_version in {
            _EXECUTION_PROFILE_SCHEMA_VERSION_V3,
            _EXECUTION_PROFILE_SCHEMA_VERSION_V4,
        }:
            self._decode_generation_retry_policy()
            self._decode_media_preflight_policy()
        else:
            raise PipelineRunValidationError("execution profile schema version is unsupported")
        if expected_parse_policy_fields == _PARSE_POLICY_FIELDS:
            _build_registered_doubao_policy(self)

    @classmethod
    def legacy_unresolved(cls) -> PipelineExecutionProfile:
        """Return the explicit fail-closed marker for pre-0008 run rows."""

        return cls(
            provider_id=None,
            model_id=None,
            adapter_strategy_version=None,
            prompt_version=None,
            kernel_parser_strategy_version=None,
            response_schema_json=None,
            request_parameters_json=None,
            parse_policy_json=None,
            vlm_stage_strategy_version=None,
            generation_retry_policy_json=None,
            media_preflight_policy_json=None,
            media_preflight_policy_hash=None,
            schema_version=_EXECUTION_PROFILE_SCHEMA_VERSION_V1,
            kind="legacy_unresolved",
        )

    @classmethod
    def from_policies(
        cls,
        policy: DoubaoVlmRequestPolicy,
        media_preflight_policy: LocalMediaPreflightPolicy,
        *,
        retry_policy: GenerationRetryPolicy,
    ) -> PipelineExecutionProfile:
        """Freeze every process-supplied VLM and physical-evidence strategy."""

        from autocut_kernel.vlm import GenerationRetryPolicy

        from auto_cut_bot.pipeline.media_preflight import LocalMediaPreflightPolicy
        from auto_cut_bot.pipeline.vlm.request_factory import DoubaoVlmRequestPolicy

        if type(policy) is not DoubaoVlmRequestPolicy:  # noqa: E721
            raise PipelineRunValidationError(
                "execution profile requires an exact DoubaoVlmRequestPolicy"
            )
        if type(media_preflight_policy) is not LocalMediaPreflightPolicy:  # noqa: E721
            raise PipelineRunValidationError(
                "execution profile requires an exact LocalMediaPreflightPolicy"
            )
        if type(retry_policy) is not GenerationRetryPolicy:  # noqa: E721
            raise PipelineRunValidationError(
                "execution profile requires an exact GenerationRetryPolicy"
            )
        return cls(
            provider_id=policy.provider_id,
            model_id=policy.model_id,
            adapter_strategy_version=policy.adapter_strategy_version,
            prompt_version=policy.prompt_version,
            kernel_parser_strategy_version=policy.parser_strategy_version,
            response_schema_json=policy.response_schema_json,
            request_parameters_json=policy.request_parameters_json,
            parse_policy_json=_canonical_json(policy.parse_policy.to_mapping()),
            vlm_stage_strategy_version=policy.stage_strategy_version,
            generation_retry_policy_json=_canonical_json(retry_policy.to_mapping()),
            media_preflight_policy_json=_canonical_json(media_preflight_policy.to_mapping()),
            media_preflight_policy_hash=media_preflight_policy.canonical_hash,
            schema_version=_EXECUTION_PROFILE_SCHEMA_VERSION_V4,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PipelineExecutionProfile:
        if any(type(key) is not str for key in value):  # noqa: E721
            raise PipelineRunValidationError("execution profile field names must be strings")
        kind = value.get("kind")
        schema_version = value.get("schema_version")
        if kind == "legacy_unresolved":
            allowed = {"kind", "schema_version"}
            unsupported = set(value) - allowed
            if unsupported:
                raise PipelineRunValidationError(
                    f"execution profile contains unsupported fields: {', '.join(sorted(unsupported))}"
                )
            if set(value) != allowed or schema_version != _EXECUTION_PROFILE_SCHEMA_VERSION_V1:
                raise PipelineRunValidationError("legacy execution profile marker is invalid")
            return cls.legacy_unresolved()
        base_allowed = {
            "adapter_strategy_version",
            "kind",
            "kernel_parser_strategy_version",
            "model_id",
            "parse_policy",
            "prompt_version",
            "provider_id",
            "request_parameters",
            "response_schema",
            "schema_version",
            "vlm_stage_strategy_version",
        }
        if schema_version == _EXECUTION_PROFILE_SCHEMA_VERSION_V1:
            allowed = base_allowed
        elif schema_version == _EXECUTION_PROFILE_SCHEMA_VERSION_V2:
            allowed = base_allowed | {"generation_retry_policy"}
        elif schema_version in {
            _EXECUTION_PROFILE_SCHEMA_VERSION_V3,
            _EXECUTION_PROFILE_SCHEMA_VERSION_V4,
        }:
            allowed = base_allowed | {
                "generation_retry_policy",
                "media_preflight_policy",
                "media_preflight_policy_hash",
            }
        else:
            raise PipelineRunValidationError("execution profile schema version is invalid")
        unsupported = set(value) - allowed
        if unsupported:
            raise PipelineRunValidationError(
                f"execution profile contains unsupported fields: {', '.join(sorted(unsupported))}"
            )
        if set(value) != allowed:
            missing = allowed - set(value)
            raise PipelineRunValidationError(
                f"execution profile is missing fields: {', '.join(sorted(missing))}"
            )
        if kind != "doubao_vlm":
            raise PipelineRunValidationError("execution profile kind or schema version is invalid")
        embedded_objects = {"response_schema", "request_parameters", "parse_policy"}
        if schema_version in {
            _EXECUTION_PROFILE_SCHEMA_VERSION_V3,
            _EXECUTION_PROFILE_SCHEMA_VERSION_V4,
        }:
            embedded_objects.add("media_preflight_policy")
        for field_name in embedded_objects:
            if type(value[field_name]) is not dict:  # noqa: E721
                raise PipelineRunValidationError(
                    f"execution profile {field_name} must be a JSON object"
                )
        parse_policy_value = cast(dict[str, object], value["parse_policy"])
        expected_parse_fields = (
            _PARSE_POLICY_FIELDS
            if schema_version == _EXECUTION_PROFILE_SCHEMA_VERSION_V4
            else _LEGACY_PARSE_POLICY_FIELDS
        )
        if frozenset(parse_policy_value) != expected_parse_fields:
            raise PipelineRunValidationError(
                "execution profile parse_policy does not match its schema major"
            )
        return cls(
            provider_id=_profile_text(value["provider_id"], "execution_profile.provider_id"),
            model_id=_profile_text(value["model_id"], "execution_profile.model_id"),
            adapter_strategy_version=_profile_text(
                value["adapter_strategy_version"],
                "execution_profile.adapter_strategy_version",
            ),
            prompt_version=_profile_text(
                value["prompt_version"],
                "execution_profile.prompt_version",
            ),
            kernel_parser_strategy_version=_profile_text(
                value["kernel_parser_strategy_version"],
                "execution_profile.kernel_parser_strategy_version",
            ),
            response_schema_json=_canonical_json(value["response_schema"]),
            request_parameters_json=_canonical_json(value["request_parameters"]),
            parse_policy_json=_canonical_json(value["parse_policy"]),
            vlm_stage_strategy_version=_profile_text(
                value["vlm_stage_strategy_version"],
                "execution_profile.vlm_stage_strategy_version",
            ),
            generation_retry_policy_json=(
                None
                if schema_version == _EXECUTION_PROFILE_SCHEMA_VERSION_V1
                else _canonical_json(value["generation_retry_policy"])
            ),
            media_preflight_policy_json=(
                _canonical_json(value["media_preflight_policy"])
                if schema_version
                in {
                    _EXECUTION_PROFILE_SCHEMA_VERSION_V3,
                    _EXECUTION_PROFILE_SCHEMA_VERSION_V4,
                }
                else None
            ),
            media_preflight_policy_hash=(
                _profile_text(
                    value["media_preflight_policy_hash"],
                    "execution_profile.media_preflight_policy_hash",
                )
                if schema_version
                in {
                    _EXECUTION_PROFILE_SCHEMA_VERSION_V3,
                    _EXECUTION_PROFILE_SCHEMA_VERSION_V4,
                }
                else None
            ),
            schema_version=cast(str, schema_version),
            _historical_read_token=(
                _HISTORICAL_PROFILE_READ_TOKEN
                if schema_version
                in {
                    _EXECUTION_PROFILE_SCHEMA_VERSION_V1,
                    _EXECUTION_PROFILE_SCHEMA_VERSION_V2,
                    _EXECUTION_PROFILE_SCHEMA_VERSION_V3,
                }
                else None
            ),
        )

    @property
    def is_legacy_unresolved(self) -> bool:
        return self.kind == "legacy_unresolved"

    @property
    def has_media_preflight_policy(self) -> bool:
        """Whether this profile can execute the media-preflight stage."""

        return self.schema_version == _EXECUTION_PROFILE_SCHEMA_VERSION_V4

    def to_mapping(self) -> dict[str, object]:
        if self.is_legacy_unresolved:
            return {
                "kind": "legacy_unresolved",
                "schema_version": _EXECUTION_PROFILE_SCHEMA_VERSION_V1,
            }
        result: dict[str, object] = {
            "adapter_strategy_version": self.adapter_strategy_version,
            "kind": "doubao_vlm",
            "kernel_parser_strategy_version": self.kernel_parser_strategy_version,
            "model_id": self.model_id,
            "parse_policy": _decode_canonical_json(self.parse_policy_json, "parse_policy_json"),
            "prompt_version": self.prompt_version,
            "provider_id": self.provider_id,
            "request_parameters": _decode_canonical_json(
                self.request_parameters_json,
                "request_parameters_json",
            ),
            "response_schema": _decode_canonical_json(
                self.response_schema_json,
                "response_schema_json",
            ),
            "schema_version": self.schema_version,
            "vlm_stage_strategy_version": self.vlm_stage_strategy_version,
        }
        if self.schema_version in {
            _EXECUTION_PROFILE_SCHEMA_VERSION_V2,
            _EXECUTION_PROFILE_SCHEMA_VERSION_V3,
            _EXECUTION_PROFILE_SCHEMA_VERSION_V4,
        }:
            result["generation_retry_policy"] = _decode_canonical_json(
                self.generation_retry_policy_json,
                "generation_retry_policy_json",
            )
        if self.schema_version in {
            _EXECUTION_PROFILE_SCHEMA_VERSION_V3,
            _EXECUTION_PROFILE_SCHEMA_VERSION_V4,
        }:
            result["media_preflight_policy"] = _decode_canonical_json(
                self.media_preflight_policy_json,
                "media_preflight_policy_json",
            )
            result["media_preflight_policy_hash"] = self.media_preflight_policy_hash
        return result

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    def to_doubao_policy(self) -> DoubaoVlmRequestPolicy:
        """Rebuild the exact registered policy without consulting process defaults."""

        return _build_registered_doubao_policy(self)

    def to_media_preflight_policy(self) -> LocalMediaPreflightPolicy:
        """Rebuild the frozen physical-evidence policy without environment defaults."""

        return self._decode_media_preflight_policy()

    def _decode_media_preflight_policy(self) -> LocalMediaPreflightPolicy:
        from auto_cut_bot.pipeline.media_preflight import (
            LocalMediaPolicyError,
            LocalMediaPreflightPolicy,
        )

        if self.schema_version not in {
            _EXECUTION_PROFILE_SCHEMA_VERSION_V3,
            _EXECUTION_PROFILE_SCHEMA_VERSION_V4,
        }:
            raise PipelineRunValidationError(
                "execution profile has no frozen media-preflight policy"
            )
        value = _decode_canonical_json(
            self.media_preflight_policy_json,
            "media_preflight_policy_json",
        )
        try:
            policy = LocalMediaPreflightPolicy.from_mapping(value)
        except LocalMediaPolicyError as error:
            raise PipelineRunValidationError("media_preflight_policy_json is invalid") from error
        if policy.word_timing_capability != "required":
            raise PipelineRunValidationError(
                "pipeline execution profile requires exact word timing"
            )
        if policy.canonical_hash != self.media_preflight_policy_hash:
            raise PipelineRunValidationError(
                "media-preflight policy hash does not bind its canonical JSON"
            )
        if _canonical_json(policy.to_mapping()) != self.media_preflight_policy_json:
            raise PipelineRunValidationError("media-preflight policy is not canonical")
        return policy

    def _decode_generation_retry_policy(self) -> GenerationRetryPolicy:
        from autocut_kernel.vlm import GenerationRetryPolicy

        value = _decode_canonical_json(
            self.generation_retry_policy_json,
            "generation_retry_policy_json",
        )
        if frozenset(value) != _RETRY_POLICY_FIELDS:
            raise PipelineRunValidationError(
                "generation_retry_policy_json must match the closed retry contract"
            )
        backoff = value["backoff_seconds"]
        if type(backoff) is not list:  # noqa: E721
            raise PipelineRunValidationError(
                "generation retry backoff_seconds must be an integer array"
            )
        backoff_items = cast(list[object], backoff)
        if any(type(item) is not int for item in backoff_items):  # noqa: E721
            raise PipelineRunValidationError(
                "generation retry backoff_seconds must be an integer array"
            )
        try:
            return GenerationRetryPolicy(
                strategy_version=_profile_text(
                    value["strategy_version"],
                    "generation_retry_policy.strategy_version",
                ),
                max_attempts=cast(int, value["max_attempts"]),
                backoff_seconds=tuple(cast(int, item) for item in backoff_items),
            )
        except (TypeError, ValueError) as error:
            raise PipelineRunValidationError("generation retry policy is invalid") from error

    def to_generation_retry_policy(self) -> GenerationRetryPolicy:
        from autocut_kernel.vlm import (
            GENERATION_RETRY_STRATEGY_VERSION,
            GenerationRetryPolicy,
        )

        if self.is_legacy_unresolved:
            raise PipelineRunValidationError(
                "legacy-unresolved execution profile has no generation retry policy"
            )
        if self.schema_version == _EXECUTION_PROFILE_SCHEMA_VERSION_V1:
            return GenerationRetryPolicy(
                strategy_version=GENERATION_RETRY_STRATEGY_VERSION,
                max_attempts=1,
                backoff_seconds=(),
            )
        policy = self._decode_generation_retry_policy()
        if type(policy) is not GenerationRetryPolicy:  # noqa: E721
            raise PipelineRunValidationError("generation retry policy lost its exact type")
        return policy


def _build_registered_doubao_policy(
    profile: PipelineExecutionProfile,
) -> DoubaoVlmRequestPolicy:
    from autocut_kernel.vlm import VlmParsePolicy

    from auto_cut_bot.pipeline.vlm.request_factory import DoubaoVlmRequestPolicy

    if profile.is_legacy_unresolved:
        raise PipelineRunValidationError(
            "legacy-unresolved execution profile cannot reconstruct a Doubao policy"
        )
    if profile.schema_version != _EXECUTION_PROFILE_SCHEMA_VERSION_V4:
        raise PipelineRunValidationError(
            "historical execution profile is read-only and cannot map to v4 policy"
        )
    parameters = _decode_canonical_json(
        profile.request_parameters_json,
        "request_parameters_json",
    )
    parse_policy = _decode_canonical_json(profile.parse_policy_json, "parse_policy_json")
    if frozenset(parse_policy) != _PARSE_POLICY_FIELDS:
        raise PipelineRunValidationError(
            "historical execution profile is read-only and cannot map to v4 policy"
        )
    try:
        rebuilt = DoubaoVlmRequestPolicy(
            model_id=cast(str, profile.model_id),
            provider_id=cast(str, profile.provider_id),
            adapter_strategy_version=cast(str, profile.adapter_strategy_version),
            prompt_version=cast(str, profile.prompt_version),
            parser_strategy_version=cast(
                str,
                profile.kernel_parser_strategy_version,
            ),
            response_schema_json=cast(str, profile.response_schema_json),
            video_fps=cast(float, parameters["video_fps"]),
            max_output_tokens=cast(int, parameters["max_output_tokens"]),
            temperature=cast(int | float, parameters["temperature"]),
            parse_policy=VlmParsePolicy(
                max_response_bytes=cast(int, parse_policy["max_response_bytes"]),
                max_entities=cast(int, parse_policy["max_entities"]),
                max_facts=cast(int, parse_policy["max_facts"]),
                max_events=cast(int, parse_policy["max_events"]),
                max_candidate_hypotheses=cast(int, parse_policy["max_candidate_hypotheses"]),
                max_temporal_segments=cast(int, parse_policy["max_temporal_segments"]),
                max_measurements=cast(int, parse_policy["max_measurements"]),
                max_text_characters=cast(int, parse_policy["max_text_characters"]),
                max_total_text_characters=cast(int, parse_policy["max_total_text_characters"]),
            ),
            stage_strategy_version=cast(str, profile.vlm_stage_strategy_version),
        )
    except (TypeError, ValueError) as error:
        raise PipelineRunValidationError(
            "execution profile does not match the registered Doubao policy"
        ) from error
    registered_mapping: dict[str, object] = {
        "adapter_strategy_version": rebuilt.adapter_strategy_version,
        "kind": "doubao_vlm",
        "kernel_parser_strategy_version": rebuilt.parser_strategy_version,
        "model_id": rebuilt.model_id,
        "parse_policy": rebuilt.parse_policy.to_mapping(),
        "prompt_version": rebuilt.prompt_version,
        "provider_id": rebuilt.provider_id,
        "request_parameters": rebuilt.request_parameters,
        "response_schema": _decode_canonical_json(
            rebuilt.response_schema_json,
            "response_schema_json",
        ),
        "schema_version": profile.schema_version,
        "vlm_stage_strategy_version": rebuilt.stage_strategy_version,
    }
    registered_mapping["generation_retry_policy"] = (
        profile.to_generation_retry_policy().to_mapping()
    )
    registered_mapping["media_preflight_policy"] = profile.to_media_preflight_policy().to_mapping()
    registered_mapping["media_preflight_policy_hash"] = profile.media_preflight_policy_hash
    if _canonical_json(registered_mapping) != profile.canonical_json:
        raise PipelineRunValidationError(
            "execution profile cannot exactly reconstruct its registered Doubao policy"
        )
    return rebuilt


_LEGACY_UNRESOLVED_EXECUTION_PROFILE = PipelineExecutionProfile.legacy_unresolved()


@dataclass(frozen=True, slots=True)
class PipelineRunRequest:
    """The only HTTP intent accepted by the run service.

    Source authorization is intentionally delegated to the injected authority
    port. This value only closes shape, profile and source identity.
    """

    profile: PipelineProfile
    source_root: str | None = None
    source_reference: str | None = None

    def __post_init__(self) -> None:
        if self.profile not in ("test", "shadow"):
            raise PipelineRunValidationError("profile must be 'test' or 'shadow'")
        has_root = self.source_root is not None
        has_reference = self.source_reference is not None
        if has_root == has_reference:
            raise PipelineRunValidationError(
                "request must contain exactly one of source_root or source_reference"
            )
        if self.source_root is not None:
            _required_text(self.source_root, "source_root")
        if self.source_reference is not None:
            _required_text(self.source_reference, "source_reference")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PipelineRunRequest:
        allowed = {"profile", "source_root", "source_reference"}
        unsupported = set(value) - allowed
        if unsupported:
            raise PipelineRunValidationError(
                f"unsupported fields: {', '.join(sorted(unsupported))}"
            )
        profile_value = value.get("profile")
        if profile_value not in ("test", "shadow"):
            raise PipelineRunValidationError("profile must be 'test' or 'shadow'")
        source_root_value = value.get("source_root")
        source_reference_value = value.get("source_reference")
        source_root = (
            _required_text(source_root_value, "source_root")
            if source_root_value is not None
            else None
        )
        source_reference = (
            _required_text(source_reference_value, "source_reference")
            if source_reference_value is not None
            else None
        )
        return cls(profile_value, source_root, source_reference)

    def to_mapping(self) -> dict[str, str]:
        source_name = "source_root" if self.source_root is not None else "source_reference"
        source_value = self.source_root if self.source_root is not None else self.source_reference
        if source_value is None:  # pragma: no cover - guarded by __post_init__
            raise PipelineRunValidationError("request source is missing")
        return {"profile": self.profile, source_name: source_value}

    @property
    def request_hash(self) -> str:
        encoded = json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PipelineCommand:
    """Persisted command status and optional durable Receipt identity."""

    command_id: str
    stage: str
    status: PipelineCommandStatus
    receipt_id: UUID | None = None
    version: int = 0
    lease_id: str | None = None
    blocking_command_id: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.command_id, "command_id")
        _required_text(self.stage, "stage")
        if self.status not in (
            "pending",
            "running",
            "succeeded",
            "denied",
            "failed",
            "indeterminate",
            "blocked",
        ):
            raise PipelineRunValidationError("command status is unsupported")
        if self.receipt_id is not None and not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.receipt_id, UUID
        ):
            raise PipelineRunValidationError("receipt_id must be a UUID")
        if type(self.version) is not int or self.version < 0:  # noqa: E721
            raise PipelineRunValidationError("command version must be a non-negative integer")
        if self.lease_id is not None:
            _required_text(self.lease_id, "lease_id")
        if self.status in ("succeeded", "denied", "failed") and self.receipt_id is None:
            raise PipelineRunValidationError("terminal command requires a Receipt")
        if (
            self.status in ("pending", "running", "indeterminate", "blocked")
            and self.receipt_id is not None
        ):
            raise PipelineRunValidationError("nonterminal command cannot claim a Receipt")
        if self.status == "running" and self.lease_id is None:
            raise PipelineRunValidationError("running command requires a lease")
        if self.status != "running" and self.lease_id is not None:
            raise PipelineRunValidationError("only a running command may hold a lease")
        if self.status == "blocked":
            _required_text(self.blocking_command_id, "blocking_command_id")
            if self.blocking_command_id == self.command_id:
                raise PipelineRunValidationError("command cannot block itself")
        elif self.blocking_command_id is not None:
            raise PipelineRunValidationError("only a blocked command names its blocker")

    def to_mapping(self) -> dict[str, str | int | None]:
        return {
            "command_id": self.command_id,
            "stage": self.stage,
            "status": self.status,
            "receipt_id": str(self.receipt_id) if self.receipt_id is not None else None,
            "version": self.version,
            "lease_id": self.lease_id,
            "blocking_command_id": self.blocking_command_id,
        }


@dataclass(frozen=True, slots=True)
class PipelineRunSnapshot:
    """Durable run projection returned by the repository port."""

    run_id: str
    request: PipelineRunRequest
    request_hash: str
    status: PipelineRunStatus
    commands: tuple[PipelineCommand, ...]
    version: int
    execution_profile: PipelineExecutionProfile = _LEGACY_UNRESOLVED_EXECUTION_PROFILE

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if type(self.request) is not PipelineRunRequest:  # noqa: E721
            raise PipelineRunValidationError("request must be a PipelineRunRequest")
        if type(self.execution_profile) is not PipelineExecutionProfile:  # noqa: E721
            raise PipelineRunValidationError("execution_profile must be a PipelineExecutionProfile")
        if self.request_hash != self.request.request_hash:
            raise PipelineRunValidationError("request_hash does not bind the canonical request")
        if self.status not in ("accepted", "running", "succeeded", "denied", "failed"):
            raise PipelineRunValidationError("run status is unsupported")
        if type(self.commands) is not tuple or any(  # noqa: E721
            type(command) is not PipelineCommand
            for command in self.commands  # noqa: E721
        ):
            raise PipelineRunValidationError("commands must be a tuple of PipelineCommand values")
        if not self.commands:
            raise PipelineRunValidationError("commands must not be empty")
        if type(self.version) is not int or self.version < 0:  # noqa: E721
            raise PipelineRunValidationError("version must be a non-negative integer")
        terminal_statuses = {"succeeded", "denied", "failed", "blocked"}
        if self.status in terminal_statuses:
            if any(command.status not in terminal_statuses for command in self.commands):
                raise PipelineRunValidationError("terminal run requires every command terminal")
            if self.status == "succeeded" and any(
                command.status != "succeeded" for command in self.commands
            ):
                raise PipelineRunValidationError("succeeded run requires every command succeeded")
            if self.status == "denied" and not any(
                command.status == "denied" for command in self.commands
            ):
                raise PipelineRunValidationError("denied run must contain a denied command")
            if self.status == "failed" and not (
                any(command.status == "failed" for command in self.commands)
                or (
                    tuple(command.stage for command in self.commands)
                    == _FAIL_CLOSED_BOOTSTRAP_STAGES
                    and all(command.status == "succeeded" for command in self.commands)
                )
            ):
                raise PipelineRunValidationError(
                    "failed run must contain a failed command or a fail-closed incomplete plan"
                )
        elif not any(
            command.status in ("pending", "running", "indeterminate") for command in self.commands
        ):
            raise PipelineRunValidationError(
                "nonterminal run requires at least one nonterminal command"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "profile": self.request.profile,
            "request_hash": self.request_hash,
            "execution_profile_hash": self.execution_profile_hash,
            "status": self.status,
            "commands": [command.to_mapping() for command in self.commands],
            "version": self.version,
        }

    @property
    def execution_profile_hash(self) -> str:
        return self.execution_profile.canonical_hash


@dataclass(frozen=True, slots=True)
class RunClaim:
    snapshot: PipelineRunSnapshot
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.snapshot) is not PipelineRunSnapshot:  # noqa: E721
            raise PipelineRunValidationError("claim snapshot must be a PipelineRunSnapshot")
        if type(self.replayed) is not bool:  # noqa: E721
            raise PipelineRunValidationError("replayed must be a bool")


@dataclass(frozen=True, slots=True)
class PipelineStageResult:
    """A stage port outcome; only its repository may turn it into run state."""

    command_id: str
    outcome: PipelineStageOutcome
    receipt_id: UUID | None = None

    def __post_init__(self) -> None:
        _required_text(self.command_id, "command_id")
        if self.outcome not in ("succeeded", "denied", "failed", "indeterminate"):
            raise PipelineRunValidationError("stage outcome is unsupported")
        if self.receipt_id is not None and not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.receipt_id, UUID
        ):
            raise PipelineRunValidationError("receipt_id must be a UUID")
        if self.outcome in ("succeeded", "denied", "failed") and self.receipt_id is None:
            raise PipelineRunValidationError("terminal stage result requires a Receipt")
        if self.outcome == "indeterminate" and self.receipt_id is not None:
            raise PipelineRunValidationError("indeterminate stage cannot claim a Receipt")


@dataclass(frozen=True, slots=True)
class PipelineStageContext:
    """Exact persisted run/request/command identity passed to a stage port."""

    run_id: str
    request: PipelineRunRequest
    command: PipelineCommand
    execution_profile: PipelineExecutionProfile = _LEGACY_UNRESOLVED_EXECUTION_PROFILE

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if type(self.request) is not PipelineRunRequest:  # noqa: E721
            raise PipelineRunValidationError("stage context request must be canonical")
        if type(self.command) is not PipelineCommand:  # noqa: E721
            raise PipelineRunValidationError("stage context command must be persisted")
        if type(self.execution_profile) is not PipelineExecutionProfile:  # noqa: E721
            raise PipelineRunValidationError("stage context execution_profile must be persisted")
        if self.command.stage == "vlm" and self.execution_profile.is_legacy_unresolved:
            raise PipelineRunValidationError(
                "legacy-unresolved execution profile cannot execute VLM"
            )
        if (
            self.command.stage == "vlm"
            and self.execution_profile.schema_version != _EXECUTION_PROFILE_SCHEMA_VERSION_V4
        ):
            raise PipelineRunValidationError(
                "VLM execution requires a persisted execution profile v4"
            )
        if (
            self.command.stage == "media_preflight"
            and self.execution_profile.schema_version != _EXECUTION_PROFILE_SCHEMA_VERSION_V4
        ):
            raise PipelineRunValidationError(
                "media-preflight execution requires a persisted execution profile v4"
            )

    @property
    def execution_profile_hash(self) -> str:
        return self.execution_profile.canonical_hash


@dataclass(frozen=True, slots=True)
class OutboxLease:
    """One exact durable outbox ownership token."""

    outbox_id: UUID
    run_id: str
    version: int
    lease_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.outbox_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise PipelineRunValidationError("outbox_id must be a UUID")
        validate_run_id(self.run_id)
        if type(self.version) is not int or self.version < 1:  # noqa: E721
            raise PipelineRunValidationError("leased outbox version must be positive")
        _required_text(self.lease_id, "lease_id")
