"""Closed, semantic records for the local Pipeline persistence core."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from .errors import StoreValidationError

CommandOutcomeKind = Literal["pending", "running", "succeeded", "denied", "failed"]
JobProfile = Literal["test", "shadow", "production"]


def _text(value: str, field_name: str) -> None:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise StoreValidationError(f"{field_name} must be a non-empty string")


def _sha256(value: str, field_name: str) -> None:
    _text(value, field_name)
    if (
        len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise StoreValidationError(f"{field_name} must be a lowercase sha256 digest")


@dataclass(frozen=True, slots=True)
class Job:
    job_key: str
    profile: JobProfile

    def __post_init__(self) -> None:
        _text(self.job_key, "job_key")


@dataclass(frozen=True, slots=True)
class ArtifactScope:
    namespace: str
    kind: str
    key: str

    def __post_init__(self) -> None:
        _text(self.namespace, "scope.namespace")
        _text(self.kind, "scope.kind")
        _text(self.key, "scope.key")


def canonical_recipe_scope(job: Job) -> ArtifactScope:
    """Return the only artifact scope a local Job recipe may occupy.

    The generic Store deliberately permits other scopes for other artifact
    producers. Local-media production and persisted rendering use this helper
    as their shared policy boundary.
    """
    return ArtifactScope("pipeline", "job", job.job_key)


@dataclass(frozen=True, slots=True)
class ArtifactMember:
    artifact_type: str
    logical_id: str
    revision: int
    scope: ArtifactScope
    content_hash: str
    payload_json: str

    def __post_init__(self) -> None:
        _text(self.artifact_type, "artifact_type")
        _text(self.logical_id, "logical_id")
        if type(self.revision) is not int or self.revision < 1:  # noqa: E721
            raise StoreValidationError("revision must be a positive integer")
        _sha256(self.content_hash, "content_hash")
        _text(self.payload_json, "payload_json")
        try:
            json.loads(self.payload_json)
        except (TypeError, ValueError) as error:
            raise StoreValidationError("payload_json must contain JSON") from error


def canonical_payload_hash(payload_json: str) -> str:
    """Hash parsed JSON in the canonical form used for immutable store members."""
    try:
        payload = json.loads(
            payload_json,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
    except (TypeError, ValueError) as error:
        raise StoreValidationError("payload_json must contain finite JSON") from error
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RecipeReference:
    """The complete immutable identity of one persisted ``recipe`` artifact."""

    scope: ArtifactScope
    logical_id: str
    revision: int
    content_hash: str
    artifact_type: Literal["recipe"] = "recipe"

    def __post_init__(self) -> None:
        _text(self.logical_id, "logical_id")
        if type(self.revision) is not int or self.revision < 1:  # noqa: E721
            raise StoreValidationError("revision must be a positive integer")
        _sha256(self.content_hash, "content_hash")
        if self.artifact_type != "recipe":
            raise StoreValidationError("recipe reference artifact_type must be 'recipe'")


@dataclass(frozen=True, slots=True)
class PersistedRecipe:
    """One verified Recipe payload plus the succeeded command provenance that owns it."""

    reference: RecipeReference
    payload_json: str
    job_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID
    command_slot_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("job_id must be a UUID")
        if not isinstance(self.receipt_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("receipt_id must be a UUID")
        if not isinstance(self.artifact_set_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("artifact_set_id must be a UUID")
        if not isinstance(self.command_slot_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("command_slot_id must be a UUID")
        if canonical_payload_hash(self.payload_json) != self.reference.content_hash:
            raise StoreValidationError("payload_json does not match recipe content_hash")


@dataclass(frozen=True, slots=True)
class MediaEvidenceReference:
    """The complete immutable identity of one persisted ``media_evidence`` artifact.

    Its payload is deliberately retained as JSON by :class:`PersistedMediaEvidence`.
    A media adapter can therefore parse the exact source evidence fields (including
    ``source.sha256`` and ``source.byte_size``) without a lossy store projection.
    """

    scope: ArtifactScope
    logical_id: str
    revision: int
    content_hash: str
    artifact_type: Literal["media_evidence"] = "media_evidence"

    def __post_init__(self) -> None:
        _text(self.logical_id, "logical_id")
        if type(self.revision) is not int or self.revision < 1:  # noqa: E721
            raise StoreValidationError("revision must be a positive integer")
        _sha256(self.content_hash, "content_hash")
        if self.artifact_type != "media_evidence":
            raise StoreValidationError(
                "media evidence reference artifact_type must be 'media_evidence'"
            )


@dataclass(frozen=True, slots=True)
class PersistedMediaEvidence:
    """Verified MediaEvidence JSON and the succeeded command provenance that owns it."""

    reference: MediaEvidenceReference
    payload_json: str
    job_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID
    command_slot_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("job_id must be a UUID")
        if not isinstance(self.receipt_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("receipt_id must be a UUID")
        if not isinstance(self.artifact_set_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("artifact_set_id must be a UUID")
        if not isinstance(self.command_slot_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("command_slot_id must be a UUID")
        if canonical_payload_hash(self.payload_json) != self.reference.content_hash:
            raise StoreValidationError("payload_json does not match media evidence content_hash")


@dataclass(frozen=True, slots=True)
class PersistedMediaOutputs:
    """The exact evidence/recipe pair produced by one succeeded media command.

    References are intentionally returned with their shared success provenance,
    rather than independently resolving logical heads or unrelated artifacts.
    """

    media_evidence: MediaEvidenceReference
    recipe: RecipeReference
    job_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID
    command_slot_id: UUID

    def __post_init__(self) -> None:
        if type(self.media_evidence) is not MediaEvidenceReference:  # noqa: E721
            raise StoreValidationError("media_evidence must be a MediaEvidenceReference")
        if type(self.recipe) is not RecipeReference:  # noqa: E721
            raise StoreValidationError("recipe must be a RecipeReference")
        if self.media_evidence.scope != self.recipe.scope:
            raise StoreValidationError("media output references must share one artifact scope")
        for field_name, value in (
            ("job_id", self.job_id),
            ("receipt_id", self.receipt_id),
            ("artifact_set_id", self.artifact_set_id),
            ("command_slot_id", self.command_slot_id),
        ):
            if not isinstance(value, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise StoreValidationError(f"{field_name} must be a UUID")


@dataclass(frozen=True, slots=True)
class SemanticResolutionProofReference:
    """The exact immutable proof member from a semantic command ArtifactSet."""

    scope: ArtifactScope
    logical_id: str
    revision: int
    content_hash: str
    artifact_type: Literal["semantic_resolution_proof"] = "semantic_resolution_proof"

    def __post_init__(self) -> None:
        _text(self.logical_id, "logical_id")
        if type(self.revision) is not int or self.revision < 1:  # noqa: E721
            raise StoreValidationError("revision must be a positive integer")
        _sha256(self.content_hash, "content_hash")
        if self.artifact_type != "semantic_resolution_proof":
            raise StoreValidationError(
                "semantic resolution proof artifact_type must be 'semantic_resolution_proof'"
            )


@dataclass(frozen=True, slots=True)
class PersistedSemanticResolutionProof:
    """Verified proof payload and shared success provenance for replay recovery."""

    reference: SemanticResolutionProofReference
    payload_json: str
    job_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID
    command_slot_id: UUID

    def __post_init__(self) -> None:
        if type(self.reference) is not SemanticResolutionProofReference:  # noqa: E721
            raise StoreValidationError("reference must be a SemanticResolutionProofReference")
        for field_name, value in (
            ("job_id", self.job_id),
            ("receipt_id", self.receipt_id),
            ("artifact_set_id", self.artifact_set_id),
            ("command_slot_id", self.command_slot_id),
        ):
            if not isinstance(value, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise StoreValidationError(f"{field_name} must be a UUID")
        if canonical_payload_hash(self.payload_json) != self.reference.content_hash:
            raise StoreValidationError("payload_json does not match semantic resolution proof content_hash")


@dataclass(frozen=True, slots=True)
class CommandClaim:
    job: Job
    idempotency_key: str
    command_name: str
    request_hash: str

    def __post_init__(self) -> None:
        _text(self.idempotency_key, "idempotency_key")
        _text(self.command_name, "command_name")
        _sha256(self.request_hash, "request_hash")


@dataclass(frozen=True, slots=True)
class CommandSuccess:
    command_slot_id: UUID
    set_hash: str
    artifacts: tuple[ArtifactMember, ...]

    def __post_init__(self) -> None:
        _sha256(self.set_hash, "set_hash")
        if not self.artifacts:
            raise StoreValidationError("a successful command requires a non-empty artifact set")
        identities = {
            (
                item.scope.namespace,
                item.scope.kind,
                item.scope.key,
                item.artifact_type,
                item.logical_id,
            )
            for item in self.artifacts
        }
        if len(identities) != len(self.artifacts):
            raise StoreValidationError("one artifact set cannot advance a logical chain twice")
        if self.set_hash != self.expected_set_hash:
            raise StoreValidationError("set_hash must bind the exact artifact members")

    @property
    def expected_set_hash(self) -> str:
        canonical_members = [
            {
                "artifact_type": item.artifact_type,
                "content_hash": item.content_hash,
                "logical_id": item.logical_id,
                "payload_json": json.loads(item.payload_json),
                "revision": item.revision,
                "scope": {
                    "key": item.scope.key,
                    "kind": item.scope.kind,
                    "namespace": item.scope.namespace,
                },
            }
            for item in self.artifacts
        ]
        encoded = json.dumps(
            canonical_members, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CommandRejection:
    command_slot_id: UUID
    failure_code: str
    failure_detail_json: str
    outcome: Literal["denied", "failed"] = "denied"

    def __post_init__(self) -> None:
        if not isinstance(self.command_slot_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("command_slot_id must be a UUID")
        _text(self.failure_code, "failure_code")
        _text(self.failure_detail_json, "failure_detail_json")
        try:
            detail = json.loads(
                self.failure_detail_json,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant {value}")
                ),
            )
        except (TypeError, ValueError) as error:
            raise StoreValidationError("failure_detail_json must contain JSON") from error
        if not isinstance(detail, dict):
            raise StoreValidationError("failure_detail_json must contain a JSON object")
        if self.outcome not in ("denied", "failed"):
            raise StoreValidationError("outcome must be 'denied' or 'failed'")


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    command_slot_id: UUID
    state: CommandOutcomeKind
    is_fresh_claim: bool = False
    receipt_id: UUID | None = None
    artifact_set_id: UUID | None = None
    failure_code: str | None = None
    failure_detail_json: str | None = None
    job_id: UUID | None = field(default=None, compare=False)
