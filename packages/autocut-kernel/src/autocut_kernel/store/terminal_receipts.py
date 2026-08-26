"""Exact terminal Receipt values, not retry authorization or HTTP preimages.

``failure_detail_json`` preserves the database's logical JSON text. JSONB has
already normalized the original input; consumers must independently reconstruct
and verify any domain-specific canonical response proof.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import Literal, cast
from uuid import UUID

from .errors import StoreValidationError
from .models import CommandExecutionKind, Job


def _text(value: object, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise StoreValidationError(f"{name} must be non-empty text")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as error:
        raise StoreValidationError(f"{name} must be UTF-8 text") from error


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_value: str) -> object:
    raise ValueError("nonfinite JSON number")


def _validate_detail(raw: str) -> None:
    _text(raw, "failure_detail_json")
    try:
        # Validate numbers without any binary-float conversion or reserialization.
        # The parsed tree is discarded; the exact logical JSONB text is retained.
        decoded: object = json.loads(
            raw, object_pairs_hook=_object, parse_constant=_nonfinite,
            parse_float=Decimal, parse_int=Decimal,
        )
        if type(decoded) is not dict:
            raise ValueError("failure detail must be an object")
        pending: list[object] = [decoded]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                mapping = cast(dict[str, object], value)
                for key, child in mapping.items():
                    key.encode("utf-8", "strict")
                    pending.append(child)
            elif isinstance(value, list):
                pending.extend(cast(list[object], value))
            elif isinstance(value, str):
                value.encode("utf-8", "strict")
            elif isinstance(value, Decimal) and not value.is_finite():
                raise ValueError("nonfinite JSON number")
    except (ValueError, DecimalException, RecursionError) as error:
        raise StoreValidationError("failure_detail_json must be strict finite object JSON") from error


@dataclass(frozen=True, slots=True)
class PersistedTerminalCommandReceipt:
    """A content projection; direct construction proves no durable ownership."""

    job: Job
    job_id: UUID
    command_slot_id: UUID
    receipt_id: UUID
    request_hash: str
    command_name: str
    execution_kind: CommandExecutionKind
    outcome: Literal["failed", "denied"]
    failure_code: str
    failure_detail_json: str

    def __post_init__(self) -> None:
        if type(self.job) is not Job or type(self.job.profile) is not str or self.job.profile not in (
            "test", "shadow", "production", "authority",
        ):
            raise StoreValidationError("terminal Receipt requires an exact Job and profile")
        _text(self.job.job_key, "job_key")
        for name, value in (
            ("job_id", self.job_id), ("command_slot_id", self.command_slot_id),
            ("receipt_id", self.receipt_id),
        ):
            if type(value) is not UUID:
                raise StoreValidationError(f"{name} must be an exact UUID")
        if (
            type(self.request_hash) is not str or len(self.request_hash) != 71
            or not self.request_hash.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in self.request_hash[7:])
        ):
            raise StoreValidationError("request_hash must be a lowercase sha256 digest")
        _text(self.command_name, "command_name")
        if type(self.execution_kind) is not str or self.execution_kind not in ("deterministic", "generation"):
            raise StoreValidationError("terminal Receipt execution kind is unsupported")
        if type(self.outcome) is not str or self.outcome not in ("failed", "denied"):
            raise StoreValidationError("terminal Receipt outcome must be failed or denied")
        _text(self.failure_code, "failure_code")
        _validate_detail(self.failure_detail_json)
