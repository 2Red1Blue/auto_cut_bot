"""Closed request and status values for the pipeline run control plane."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Mapping
from uuid import UUID

from .errors import PipelineRunValidationError

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
        if self.status in ("pending", "running", "indeterminate", "blocked") and self.receipt_id is not None:
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

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if type(self.request) is not PipelineRunRequest:  # noqa: E721
            raise PipelineRunValidationError("request must be a PipelineRunRequest")
        if self.request_hash != self.request.request_hash:
            raise PipelineRunValidationError("request_hash does not bind the canonical request")
        if self.status not in ("accepted", "running", "succeeded", "denied", "failed"):
            raise PipelineRunValidationError("run status is unsupported")
        if type(self.commands) is not tuple or any(  # noqa: E721
            type(command) is not PipelineCommand for command in self.commands  # noqa: E721
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
            if self.status == "failed" and not any(
                command.status == "failed" for command in self.commands
            ):
                raise PipelineRunValidationError("failed run must contain a failed command")
        elif not any(
            command.status in ("pending", "running", "indeterminate")
            for command in self.commands
        ):
            raise PipelineRunValidationError(
                "nonterminal run requires at least one nonterminal command"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "profile": self.request.profile,
            "request_hash": self.request_hash,
            "status": self.status,
            "commands": [command.to_mapping() for command in self.commands],
            "version": self.version,
        }


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

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if type(self.request) is not PipelineRunRequest:  # noqa: E721
            raise PipelineRunValidationError("stage context request must be canonical")
        if type(self.command) is not PipelineCommand:  # noqa: E721
            raise PipelineRunValidationError("stage context command must be persisted")


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
