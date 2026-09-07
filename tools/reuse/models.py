"""Closed DTOs, strict JSON codec, error codes and status enums for the reuse runner.

Contract source: docs/open-source-adoption-phases/00-r0-experiment-foundation.md
and docs/llm-stage-contracts/13-open-source-adoption-architecture.md §9.

Everything here is intentionally closed: unknown JSON keys, duplicate JSON keys
and missing required fields are hard errors, never warnings.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SPEC_SCHEMA = "ExperimentSpec/v1"
CALL_REQUEST_SCHEMA = "ProviderCallRequest/v1"
PROJECTION_SCHEMA = "Projection/v1"
METRICS_SCHEMA = "Metrics/v1"
MANIFEST_V2_SCHEMA = "FixtureManifest/v2"

# ---------------------------------------------------------------------------
# Error codes and attempt status (closed enums from the R0 contract)
# ---------------------------------------------------------------------------


class ErrorCode(StrEnum):
    INVALID_SPEC = "INVALID_SPEC"
    REPLAY_MISS = "REPLAY_MISS"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    FIXTURE_UNVERIFIED = "FIXTURE_UNVERIFIED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    PROVIDER_RESULT_UNKNOWN = "PROVIDER_RESULT_UNKNOWN"
    PROJECTION_REJECTED = "PROJECTION_REJECTED"
    METRIC_INPUT_INCOMPLETE = "METRIC_INPUT_INCOMPLETE"
    SPEC_HASH_MISMATCH = "SPEC_HASH_MISMATCH"
    ATTEMPT_CONFLICT = "ATTEMPT_CONFLICT"
    PATH_ESCAPE = "PATH_ESCAPE"
    REGISTRY_INVALID = "REGISTRY_INVALID"


class AttemptStatus(StrEnum):
    RESERVED = "reserved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOT_EVALUATED = "not_evaluated"


TERMINAL_STATUSES = {
    AttemptStatus.SUCCEEDED,
    AttemptStatus.FAILED,
    AttemptStatus.NOT_EVALUATED,
}


class ExperimentError(Exception):
    """Runner failure carrying a closed error code (recorded in metadata.json)."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Strict JSON codec
# ---------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ExperimentError(
                ErrorCode.INVALID_SPEC, f"duplicate JSON key: {key!r}"
            )
        seen[key] = value
    return seen


def load_json_strict(text: str, *, context: str = "json") -> Any:
    """Parse JSON refusing duplicate keys; wraps json.JSONDecodeError as INVALID_SPEC."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ExperimentError:
        raise
    except json.JSONDecodeError as exc:
        raise ExperimentError(
            ErrorCode.INVALID_SPEC, f"{context}: invalid JSON: {exc}"
        ) from exc


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization used for all identity hashes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_sha256(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def file_sha256(path: Any) -> str:
    import pathlib

    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _require_mapping(obj: Any, what: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ExperimentError(ErrorCode.INVALID_SPEC, f"{what}: expected object")
    return obj


def _require_str(obj: dict[str, Any], key: str, what: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ExperimentError(ErrorCode.INVALID_SPEC, f"{what}.{key}: required string")
    return value


def _reject_unknown_keys(obj: dict[str, Any], allowed: set[str], what: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ExperimentError(
            ErrorCode.INVALID_SPEC, f"{what}: unknown keys {unknown}"
        )


def _require_int(obj: dict[str, Any], key: str, what: str) -> int:
    value = obj.get(key)
    # bool is an int subclass; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExperimentError(ErrorCode.INVALID_SPEC, f"{what}.{key}: required integer")
    return value


# ---------------------------------------------------------------------------
# ExperimentSpec/v1
# ---------------------------------------------------------------------------

_MODEL_REQUEST_KEYS = {"provider", "model", "prompt_id", "schema_id", "max_output_tokens"}
_BUDGET_KEYS = {"max_calls", "max_input_tokens", "max_output_tokens", "max_concurrency"}
_SPEC_KEYS = {
    "schema",
    "experiment_id",
    "producer_id",
    "producer_commit",
    "adapter_version",
    "fixture_ref",
    "fixture_sha256",
    "variant",
    "mode",
    "model_request",
    "budget",
    "metric_policy",
    "seed",
}
_METRIC_POLICY_KEYS = {"policy_version", "revision"}

VARIANTS = ("baseline", "candidate")
MODES = ("replay", "live")


@dataclass(frozen=True)
class ModelRequest:
    """Non-secret provider/model/prompt identity. Replay requires this to be empty."""

    provider: str
    model: str
    prompt_id: str
    schema_id: str
    max_output_tokens: int | None

    @classmethod
    def from_dict(cls, obj: Any) -> ModelRequest:
        data = _require_mapping(obj, "model_request")
        _reject_unknown_keys(data, _MODEL_REQUEST_KEYS, "model_request")
        mr = cls(
            provider=_require_str(data, "provider", "model_request"),
            model=_require_str(data, "model", "model_request"),
            prompt_id=_require_str(data, "prompt_id", "model_request"),
            schema_id=_require_str(data, "schema_id", "model_request"),
            max_output_tokens=data.get("max_output_tokens"),
        )
        if mr.max_output_tokens is not None and (
            isinstance(mr.max_output_tokens, bool)
            or not isinstance(mr.max_output_tokens, int)
            or mr.max_output_tokens <= 0
        ):
            raise ExperimentError(
                ErrorCode.INVALID_SPEC, "model_request.max_output_tokens: positive integer"
            )
        return mr

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "prompt_id": self.prompt_id,
            "schema_id": self.schema_id,
        }
        if self.max_output_tokens is not None:
            out["max_output_tokens"] = self.max_output_tokens
        return out


@dataclass(frozen=True)
class Budget:
    """Hard call/token/concurrency ceilings checked before and charged after calls."""

    max_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_concurrency: int

    @classmethod
    def from_dict(cls, obj: Any) -> Budget:
        data = _require_mapping(obj, "budget")
        _reject_unknown_keys(data, _BUDGET_KEYS, "budget")
        b = cls(
            max_calls=_require_int(data, "max_calls", "budget"),
            max_input_tokens=_require_int(data, "max_input_tokens", "budget"),
            max_output_tokens=_require_int(data, "max_output_tokens", "budget"),
            max_concurrency=_require_int(data, "max_concurrency", "budget"),
        )
        for name in (
            "max_calls",
            "max_input_tokens",
            "max_output_tokens",
            "max_concurrency",
        ):
            if getattr(b, name) <= 0:
                raise ExperimentError(
                    ErrorCode.INVALID_SPEC, f"budget.{name}: must be positive"
                )
        return b

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_concurrency": self.max_concurrency,
        }


@dataclass(frozen=True)
class ExperimentSpec:
    """Frozen experiment plan. Identity hash covers the whole closed object."""

    experiment_id: str
    producer_id: str
    producer_commit: str
    adapter_version: str
    fixture_ref: str
    fixture_sha256: str
    variant: str
    mode: str
    model_request: ModelRequest | None
    budget: Budget | None
    metric_policy: dict[str, str]
    seed: int | None

    @classmethod
    def from_dict(cls, obj: Any) -> ExperimentSpec:
        data = _require_mapping(obj, "spec")
        _reject_unknown_keys(data, _SPEC_KEYS, "spec")
        if data.get("schema") != SPEC_SCHEMA:
            raise ExperimentError(
                ErrorCode.INVALID_SPEC,
                f"spec.schema: expected {SPEC_SCHEMA}, got {data.get('schema')!r}",
            )
        variant = _require_str(data, "variant", "spec")
        if variant not in VARIANTS:
            raise ExperimentError(
                ErrorCode.INVALID_SPEC, f"spec.variant: must be one of {VARIANTS}"
            )
        mode = _require_str(data, "mode", "spec")
        if mode not in MODES:
            raise ExperimentError(ErrorCode.INVALID_SPEC, f"spec.mode: must be one of {MODES}")

        model_request_raw = data.get("model_request")
        budget_raw = data.get("budget")
        if mode == "replay":
            if model_request_raw not in (None, {}):
                raise ExperimentError(
                    ErrorCode.INVALID_SPEC,
                    "spec.model_request: must be empty for replay mode",
                )
        else:  # live
            if model_request_raw is None:
                raise ExperimentError(
                    ErrorCode.INVALID_SPEC,
                    "spec.model_request: required closed object for live mode",
                )
            if budget_raw is None:
                raise ExperimentError(
                    ErrorCode.INVALID_SPEC, "spec.budget: required for live mode"
                )

        policy_raw = _require_mapping(data.get("metric_policy"), "spec.metric_policy")
        _reject_unknown_keys(policy_raw, _METRIC_POLICY_KEYS, "spec.metric_policy")
        policy = {
            "policy_version": _require_str(policy_raw, "policy_version", "spec.metric_policy"),
            "revision": _require_str(policy_raw, "revision", "spec.metric_policy"),
        }

        seed = data.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ExperimentError(ErrorCode.INVALID_SPEC, "spec.seed: integer or null")

        return cls(
            experiment_id=_require_str(data, "experiment_id", "spec"),
            producer_id=_require_str(data, "producer_id", "spec"),
            producer_commit=_require_str(data, "producer_commit", "spec"),
            adapter_version=_require_str(data, "adapter_version", "spec"),
            fixture_ref=_require_str(data, "fixture_ref", "spec"),
            fixture_sha256=_require_str(data, "fixture_sha256", "spec"),
            variant=variant,
            mode=mode,
            model_request=(
                ModelRequest.from_dict(model_request_raw) if model_request_raw else None
            ),
            budget=Budget.from_dict(budget_raw) if budget_raw else None,
            metric_policy=policy,
            seed=seed,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": SPEC_SCHEMA,
            "experiment_id": self.experiment_id,
            "producer_id": self.producer_id,
            "producer_commit": self.producer_commit,
            "adapter_version": self.adapter_version,
            "fixture_ref": self.fixture_ref,
            "fixture_sha256": self.fixture_sha256,
            "variant": self.variant,
            "mode": self.mode,
            "model_request": self.model_request.to_dict() if self.model_request else None,
            "budget": self.budget.to_dict() if self.budget else None,
            "metric_policy": dict(self.metric_policy),
            "seed": self.seed,
        }
        return out

    @property
    def spec_hash(self) -> str:
        return json_sha256(self.to_dict())


# ---------------------------------------------------------------------------
# ProviderCallRequest/v1 and provider results
# ---------------------------------------------------------------------------

_CALL_REQUEST_KEYS = {"schema", "call_id", "provider", "model", "payload"}


@dataclass(frozen=True)
class ProviderCallRequest:
    """One producer model request. Request identity excludes call_id so that
    identical request content across attempts replay-matches exactly."""

    call_id: str
    provider: str
    model: str
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, obj: Any) -> ProviderCallRequest:
        data = _require_mapping(obj, "call_request")
        _reject_unknown_keys(data, _CALL_REQUEST_KEYS, "call_request")
        if data.get("schema") != CALL_REQUEST_SCHEMA:
            raise ExperimentError(
                ErrorCode.INVALID_SPEC,
                f"call_request.schema: expected {CALL_REQUEST_SCHEMA}",
            )
        payload = _require_mapping(data.get("payload"), "call_request.payload")
        return cls(
            call_id=_require_str(data, "call_id", "call_request"),
            provider=_require_str(data, "provider", "call_request"),
            model=_require_str(data, "model", "call_request"),
            payload=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CALL_REQUEST_SCHEMA,
            "call_id": self.call_id,
            "provider": self.provider,
            "model": self.model,
            "payload": self.payload,
        }

    @property
    def request_hash(self) -> str:
        return json_sha256(
            {"provider": self.provider, "model": self.model, "payload": self.payload}
        )


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ProviderResult:
    """Provider outcome. status=unknown means the call may still finish server-side;
    the attempt must be resumed with the recorded provider_response_id, never re-called."""

    raw: str
    provider_response_id: str | None
    usage: Usage
    status: str  # "done" | "unknown"


# ---------------------------------------------------------------------------
# Projection and metrics DTOs
# ---------------------------------------------------------------------------


@dataclass
class Projection:
    """Deterministic projection of raw producer output onto project DTOs.

    raw hashes bind each projected item to its untouched source; unmapped
    fields are recorded, never silently dropped."""

    producer_id: str
    projection_version: str
    items: list[dict[str, Any]] = field(default_factory=list)
    unmapped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROJECTION_SCHEMA,
            "producer_id": self.producer_id,
            "projection_version": self.projection_version,
            "items": self.items,
            "unmapped": self.unmapped,
        }


@dataclass(frozen=True)
class MetricResult:
    """One metric value with its denominator, or a null value plus the reason.

    A metric without a denominator can never be reported as a bare number."""

    metric: str
    value: float | None
    num: float | None = None
    denom: float | None = None
    missing_reason: str | None = None
    group: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "value": self.value,
            "num": self.num,
            "denom": self.denom,
            "missing_reason": self.missing_reason,
            "group": self.group,
        }

    def validate(self) -> None:
        if self.value is None and not self.missing_reason:
            raise ExperimentError(
                ErrorCode.METRIC_INPUT_INCOMPLETE,
                f"metric {self.metric}: null value requires missing_reason",
            )
        if self.value is not None and self.denom in (None, 0):
            raise ExperimentError(
                ErrorCode.METRIC_INPUT_INCOMPLETE,
                f"metric {self.metric}: non-null value requires a non-zero denominator",
            )
        if self.value is not None and self.missing_reason:
            raise ExperimentError(
                ErrorCode.METRIC_INPUT_INCOMPLETE,
                f"metric {self.metric}: value and missing_reason are mutually exclusive",
            )
