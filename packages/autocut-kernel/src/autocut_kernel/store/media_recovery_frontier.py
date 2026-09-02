"""Minimal durable closure for incremental Media Preflight recovery.

The frontier records which exact immutable episode command closures satisfy one
fixed media-input census.  It is deliberately not a generic workflow engine:
failed attempts remain in the command ledger, while a frontier slot may be
filled once by one succeeded closure and can never be replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, cast
from uuid import UUID

from ..media.types import canonical_sha256, sha256_prefixed
from .models import Job

MEDIA_RECOVERY_PLAN_SCHEMA = "media-preflight-recovery-plan-v1"
MEDIA_RECOVERY_ENTRY_SCHEMA = "media-preflight-recovery-entry-v1"
MediaRecoveryProducerKind = Literal["local_cpu", "pc_cuda"]
MediaRecoveryState = Literal["open", "complete", "finalized"]

_ZERO_SHA256 = "sha256:" + "0" * 64


class MediaRecoveryFrontierError(ValueError):
    """A recovery plan, selected episode closure or frontier is invalid."""


def _sha(value: object, field_name: str) -> str:
    try:
        digest = sha256_prefixed(value, field_name)
    except ValueError as error:
        raise MediaRecoveryFrontierError(
            f"{field_name} must be a lowercase SHA-256 identity"
        ) from error
    if digest == _ZERO_SHA256:
        raise MediaRecoveryFrontierError(f"{field_name} must not be zero")
    return digest


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():  # noqa: E721
        raise MediaRecoveryFrontierError(f"{field_name} must be canonical non-empty text")
    return value


def _job_mapping(job: Job) -> dict[str, object]:
    return {"job_key": job.job_key, "profile": job.profile}


def _job_from_mapping(value: object, field_name: str) -> Job:
    if type(value) is not dict:  # noqa: E721
        raise MediaRecoveryFrontierError(f"{field_name} must be an exact Job object")
    mapping = cast(dict[str, object], value)
    if set(mapping) != {"job_key", "profile"}:
        raise MediaRecoveryFrontierError(f"{field_name} must be an exact Job object")
    try:
        return Job(cast(str, mapping["job_key"]), cast(object, mapping["profile"]))  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise MediaRecoveryFrontierError(f"{field_name} is invalid") from error


@dataclass(frozen=True, slots=True)
class MediaRecoveryPlan:
    """Immutable identity of one complete episode evidence census."""

    base_job: Job
    execution_profile_sha256: str
    source_manifest_sha256: str
    source_provenance_sha256: str
    vlm_semantic_pack_set_sha256: str
    producer_kind: MediaRecoveryProducerKind
    producer_compatibility_sha256: str
    requirement_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.base_job) is not Job:  # noqa: E721
            raise MediaRecoveryFrontierError("base_job must be exact")
        for field_name in (
            "execution_profile_sha256",
            "source_manifest_sha256",
            "source_provenance_sha256",
            "vlm_semantic_pack_set_sha256",
            "producer_compatibility_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        if self.producer_kind not in ("local_cpu", "pc_cuda"):
            raise MediaRecoveryFrontierError("producer_kind is unsupported")
        if type(self.requirement_sha256s) is not tuple or not self.requirement_sha256s:  # noqa: E721
            raise MediaRecoveryFrontierError("recovery plan requires a non-empty episode census")
        for index, value in enumerate(self.requirement_sha256s):
            _sha(value, f"requirement_sha256s[{index}]")
        if len(set(self.requirement_sha256s)) != len(self.requirement_sha256s):
            raise MediaRecoveryFrontierError("episode requirement identities must be unique")

    def to_mapping(self) -> dict[str, object]:
        return {
            "base_job": _job_mapping(self.base_job),
            "execution_profile_sha256": self.execution_profile_sha256,
            "producer_compatibility_sha256": self.producer_compatibility_sha256,
            "producer_kind": self.producer_kind,
            "requirement_sha256s": list(self.requirement_sha256s),
            "schema_version": MEDIA_RECOVERY_PLAN_SCHEMA,
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_provenance_sha256": self.source_provenance_sha256,
            "vlm_semantic_pack_set_sha256": self.vlm_semantic_pack_set_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> MediaRecoveryPlan:
        expected = {
            "base_job",
            "execution_profile_sha256",
            "producer_compatibility_sha256",
            "producer_kind",
            "requirement_sha256s",
            "schema_version",
            "source_manifest_sha256",
            "source_provenance_sha256",
            "vlm_semantic_pack_set_sha256",
        }
        if set(value) != expected:
            raise MediaRecoveryFrontierError("recovery plan fields do not match the closed schema")
        if value["schema_version"] != MEDIA_RECOVERY_PLAN_SCHEMA:
            raise MediaRecoveryFrontierError("recovery plan schema is unsupported")
        raw_requirements = value["requirement_sha256s"]
        if type(raw_requirements) is not list:  # noqa: E721
            raise MediaRecoveryFrontierError("requirement_sha256s must be an array")
        return cls(
            _job_from_mapping(value["base_job"], "base_job"),
            cast(str, value["execution_profile_sha256"]),
            cast(str, value["source_manifest_sha256"]),
            cast(str, value["source_provenance_sha256"]),
            cast(str, value["vlm_semantic_pack_set_sha256"]),
            cast(MediaRecoveryProducerKind, value["producer_kind"]),
            cast(str, value["producer_compatibility_sha256"]),
            tuple(cast(list[str], raw_requirements)),
        )

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class MediaRecoveryEntry:
    """One exact succeeded episode command closure selected once."""

    episode_index: int
    requirement_sha256: str
    origin_job: Job
    idempotency_key: str
    request_hash: str
    transient_retry_budget: int
    command_slot_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID

    def __post_init__(self) -> None:
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise MediaRecoveryFrontierError("episode_index must be non-negative")
        _sha(self.requirement_sha256, "requirement_sha256")
        if type(self.origin_job) is not Job:  # noqa: E721
            raise MediaRecoveryFrontierError("origin_job must be exact")
        _text(self.idempotency_key, "idempotency_key")
        _sha(self.request_hash, "request_hash")
        if (
            type(self.transient_retry_budget) is not int  # noqa: E721
            or not 0 <= self.transient_retry_budget <= 3
        ):
            raise MediaRecoveryFrontierError("transient_retry_budget must be from zero through three")
        if any(
            type(value) is not UUID  # noqa: E721
            for value in (self.command_slot_id, self.receipt_id, self.artifact_set_id)
        ):
            raise MediaRecoveryFrontierError("entry command/Receipt/ArtifactSet handles must be UUIDs")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_set_id": str(self.artifact_set_id),
            "command_slot_id": str(self.command_slot_id),
            "episode_index": self.episode_index,
            "idempotency_key": self.idempotency_key,
            "origin_job": _job_mapping(self.origin_job),
            "receipt_id": str(self.receipt_id),
            "request_hash": self.request_hash,
            "requirement_sha256": self.requirement_sha256,
            "schema_version": MEDIA_RECOVERY_ENTRY_SCHEMA,
            "transient_retry_budget": self.transient_retry_budget,
        }


@dataclass(frozen=True, slots=True)
class MediaRecoveryFrontier:
    """Durable snapshot; only ``complete`` may be sent to a finalizer."""

    frontier_id: UUID
    plan: MediaRecoveryPlan
    state: MediaRecoveryState
    version: int
    entries: tuple[MediaRecoveryEntry, ...]
    finalizer_job: Job | None = None
    final_receipt_id: UUID | None = None
    final_artifact_set_id: UUID | None = None

    def __post_init__(self) -> None:
        if type(self.frontier_id) is not UUID or type(self.plan) is not MediaRecoveryPlan:  # noqa: E721
            raise MediaRecoveryFrontierError("frontier identity and plan must be exact")
        if self.state not in ("open", "complete", "finalized"):
            raise MediaRecoveryFrontierError("frontier state is unsupported")
        if type(self.version) is not int or self.version < 0:  # noqa: E721
            raise MediaRecoveryFrontierError("frontier version must be non-negative")
        if type(self.entries) is not tuple or any(  # noqa: E721
            type(entry) is not MediaRecoveryEntry for entry in self.entries
        ):
            raise MediaRecoveryFrontierError("frontier entries must be an exact tuple")
        indexes = tuple(entry.episode_index for entry in self.entries)
        if indexes != tuple(sorted(set(indexes))):
            raise MediaRecoveryFrontierError("frontier episode entries must be unique and ordered")
        if any(
            index >= len(self.plan.requirement_sha256s)
            or entry.requirement_sha256 != self.plan.requirement_sha256s[index]
            or entry.origin_job.profile != self.plan.base_job.profile
            for index, entry in zip(indexes, self.entries, strict=True)
        ):
            raise MediaRecoveryFrontierError("frontier entry does not satisfy its plan slot")
        complete = len(self.entries) == len(self.plan.requirement_sha256s)
        if self.state == "open" and (complete or self.finalizer_job is not None):
            raise MediaRecoveryFrontierError("open frontier cannot be complete or own finalization")
        if self.state in ("complete", "finalized") and (
            not complete or self.finalizer_job is None
        ):
            raise MediaRecoveryFrontierError("closed frontier requires complete coverage and an owner")
        if self.state == "finalized":
            if self.final_receipt_id is None or self.final_artifact_set_id is None:
                raise MediaRecoveryFrontierError("finalized frontier requires exact final handles")
        elif self.final_receipt_id is not None or self.final_artifact_set_id is not None:
            raise MediaRecoveryFrontierError("non-finalized frontier cannot expose final handles")


__all__ = (
    "MEDIA_RECOVERY_ENTRY_SCHEMA",
    "MEDIA_RECOVERY_PLAN_SCHEMA",
    "MediaRecoveryEntry",
    "MediaRecoveryFrontier",
    "MediaRecoveryFrontierError",
    "MediaRecoveryPlan",
    "MediaRecoveryProducerKind",
    "MediaRecoveryState",
)
