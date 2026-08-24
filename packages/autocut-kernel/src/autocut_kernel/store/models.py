"""Closed, semantic records for the local Pipeline persistence core."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, cast
from uuid import UUID

from ..vlm.models import VlmObservationSet
from .errors import StoreValidationError

CommandOutcomeKind = Literal["pending", "running", "succeeded", "denied", "failed"]
JobProfile = Literal["test", "shadow", "production"]
GenerationAttemptState = Literal[
    "reserved",
    "dispatched",
    "responded",
    "indeterminate",
    "reconciled",
    "committed",
    "failed",
]
GenerationFailureDisposition = Literal["retryable", "nonretryable", "repairable"]

_LEGACY_GENERATION_RETRY_POLICY_HASH = (
    "sha256:70f279a4b886d1aaf1498b432af937495e431113db3f38728a635ed24a6fbe39"
)


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
class WholeSeriesSourceManifestReference:
    """Exact identity of one committed whole-series source manifest."""

    scope: ArtifactScope
    logical_id: str
    revision: int
    content_hash: str
    artifact_type: Literal["whole_series_source_manifest"] = (
        "whole_series_source_manifest"
    )

    def __post_init__(self) -> None:
        _text(self.logical_id, "logical_id")
        if type(self.revision) is not int or self.revision < 1:  # noqa: E721
            raise StoreValidationError("revision must be a positive integer")
        _sha256(self.content_hash, "content_hash")
        if self.artifact_type != "whole_series_source_manifest":
            raise StoreValidationError(
                "source manifest reference has an invalid artifact_type"
            )


@dataclass(frozen=True, slots=True)
class PersistedWholeSeriesSourceManifest:
    """Verified source manifest payload, blobs, and success provenance."""

    reference: WholeSeriesSourceManifestReference
    payload_json: str
    proxy_blobs: tuple[BlobRef, ...]
    job_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID
    command_slot_id: UUID

    def __post_init__(self) -> None:
        proxy_blobs = tuple(self.proxy_blobs)
        if not proxy_blobs or any(type(item) is not BlobRef for item in proxy_blobs):  # noqa: E721
            raise StoreValidationError("source manifest proxy_blobs must contain BlobRefs")
        for field_name, value in (
            ("job_id", self.job_id),
            ("receipt_id", self.receipt_id),
            ("artifact_set_id", self.artifact_set_id),
            ("command_slot_id", self.command_slot_id),
        ):
            if not isinstance(value, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise StoreValidationError(f"{field_name} must be a UUID")
        if canonical_payload_hash(self.payload_json) != self.reference.content_hash:
            raise StoreValidationError(
                "payload_json does not match source manifest content_hash"
            )
        object.__setattr__(self, "proxy_blobs", proxy_blobs)


@dataclass(frozen=True, slots=True)
class VlmRequestRecordReference:
    """Exact immutable identity of one committed VLM request record."""

    scope: ArtifactScope
    logical_id: str
    revision: int
    content_hash: str
    artifact_type: Literal["vlm_request_record"] = "vlm_request_record"

    def __post_init__(self) -> None:
        _text(self.logical_id, "logical_id")
        if type(self.revision) is not int or self.revision < 1:  # noqa: E721
            raise StoreValidationError("revision must be a positive integer")
        _sha256(self.content_hash, "content_hash")
        if self.artifact_type != "vlm_request_record":
            raise StoreValidationError(
                "VLM request record reference has an invalid artifact_type"
            )


@dataclass(frozen=True, slots=True)
class VlmObservationSetReference:
    """Exact immutable identity of one committed VLM observation set."""

    scope: ArtifactScope
    logical_id: str
    revision: int
    content_hash: str
    artifact_type: Literal["vlm_observation_set"] = "vlm_observation_set"

    def __post_init__(self) -> None:
        _text(self.logical_id, "logical_id")
        if type(self.revision) is not int or self.revision < 1:  # noqa: E721
            raise StoreValidationError("revision must be a positive integer")
        _sha256(self.content_hash, "content_hash")
        if self.artifact_type != "vlm_observation_set":
            raise StoreValidationError(
                "VLM observation-set reference has an invalid artifact_type"
            )


_VLM_REQUEST_IDENTITY_FIELDS = frozenset(
    {
        "frame_pts_index_set_sha256",
        "frame_samples_sha256",
        "model_id",
        "parse_policy_sha256",
        "preprocess_policy_sha256",
        "prompt_template_sha256",
        "prompt_version",
        "provider_id",
        "proxy_blob_ref_sha256",
        "request_parameters_sha256",
        "request_payload_sha256",
        "response_schema_sha256",
        "source_clock_id",
        "source_id",
        "source_sha256",
        "window_manifest_set_sha256",
        "window_manifest_sha256",
        "window_sampling_policy_sha256",
    }
)
_VLM_REQUEST_RECORD_FIELDS = frozenset(
    {
        "attempt_id",
        "episode_index",
        "idempotency_key",
        "provider_idempotency_key",
        "proxy_blob",
        "request_hash",
        "request_identity",
        "request_identity_sha256",
        "request_payload_blob",
        "source_manifest_sha256",
        "source_provenance_sha256",
        "window_manifest_set_sha256",
        "window_manifest_sha256",
    }
)
_BLOB_REF_FIELDS = frozenset(
    {"object_id", "content_hash", "byte_length", "media_type"}
)


def _strict_json_mapping(value: str, field_name: str) -> dict[str, object]:
    if type(value) is not str or not value:  # noqa: E721
        raise StoreValidationError(f"{field_name} must contain canonical JSON")

    def closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, member in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = member
        return result

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=closed_pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {constant}")
            ),
        )
    except (TypeError, ValueError) as error:
        raise StoreValidationError(f"{field_name} must contain strict JSON") from error
    if type(parsed) is not dict:  # noqa: E721
        raise StoreValidationError(f"{field_name} must contain a JSON object")
    return cast(dict[str, object], parsed)


def _mapping_blob_ref(value: object, field_name: str) -> BlobRef:
    if type(value) is not dict:  # noqa: E721
        raise StoreValidationError(f"{field_name} must be a BlobRef object")
    mapping = cast(dict[str, object], value)
    if frozenset(mapping) != _BLOB_REF_FIELDS:
        raise StoreValidationError(f"{field_name} must match the closed BlobRef schema")
    try:
        object_id = UUID(str(mapping["object_id"]))
        content_hash = mapping["content_hash"]
        byte_length = mapping["byte_length"]
        media_type = mapping["media_type"]
        if type(content_hash) is not str or type(byte_length) is not int or type(media_type) is not str:  # noqa: E721
            raise ValueError("BlobRef member type mismatch")
        return BlobRef(object_id, content_hash, byte_length, media_type)
    except (KeyError, TypeError, ValueError, StoreValidationError) as error:
        raise StoreValidationError(f"{field_name} is invalid") from error


@dataclass(frozen=True, slots=True)
class PersistedVlmGenerationChild:
    """Independently verified committed VLM child and request provenance."""

    reference: VlmRequestRecordReference
    payload_json: str
    source_job: Job
    kernel_job_id: UUID
    command_slot_id: UUID
    idempotency_key: str
    request_hash: str
    attempt_id: UUID
    provider_idempotency_key: str
    request_payload: BlobRef
    receipt_id: UUID
    artifact_set_id: UUID
    episode_index: int
    window_manifest_sha256: str
    window_manifest_set_sha256: str
    source_manifest_sha256: str
    source_provenance_sha256: str
    request_identity_sha256: str

    def __post_init__(self) -> None:
        if type(self.reference) is not VlmRequestRecordReference:  # noqa: E721
            raise StoreValidationError("VLM request reference is invalid")
        if type(self.source_job) is not Job:  # noqa: E721
            raise StoreValidationError("VLM request source Job is invalid")
        for field_name in (
            "kernel_job_id",
            "command_slot_id",
            "attempt_id",
            "receipt_id",
            "artifact_set_id",
        ):
            if not isinstance(getattr(self, field_name), UUID):
                raise StoreValidationError(f"VLM request {field_name} must be a UUID")
        _text(self.idempotency_key, "VLM request idempotency_key")
        _text(self.provider_idempotency_key, "VLM request provider_idempotency_key")
        for value, field_name in (
            (self.request_hash, "request_hash"),
            (self.window_manifest_sha256, "window_manifest_sha256"),
            (self.window_manifest_set_sha256, "window_manifest_set_sha256"),
            (self.source_manifest_sha256, "source_manifest_sha256"),
            (self.source_provenance_sha256, "source_provenance_sha256"),
            (self.request_identity_sha256, "request_identity_sha256"),
        ):
            _sha256(value, field_name)
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise StoreValidationError("VLM request episode_index must be non-negative")
        if type(self.request_payload) is not BlobRef:  # noqa: E721
            raise StoreValidationError("VLM request payload must be a BlobRef")
        if self.reference.scope != canonical_recipe_scope(self.source_job):
            raise StoreValidationError("VLM request record has a non-canonical Job scope")
        if self.reference.logical_id != f"vlm_request_{self.window_manifest_sha256[7:31]}":
            raise StoreValidationError("VLM request logical identity is invalid")
        if canonical_payload_hash(self.payload_json) != self.reference.content_hash:
            raise StoreValidationError("VLM request payload does not match its artifact hash")
        payload = _strict_json_mapping(self.payload_json, "VLM request record")
        if frozenset(payload) != _VLM_REQUEST_RECORD_FIELDS:
            raise StoreValidationError("VLM request record does not match its closed schema")
        identity_value = payload["request_identity"]
        if type(identity_value) is not dict:  # noqa: E721
            raise StoreValidationError("VLM request identity does not match its closed schema")
        identity = cast(dict[str, object], identity_value)
        if frozenset(identity) != _VLM_REQUEST_IDENTITY_FIELDS:
            raise StoreValidationError("VLM request identity does not match its closed schema")
        identity_json = json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        identity_hash = "sha256:" + hashlib.sha256(identity_json).hexdigest()
        if identity_hash != self.request_identity_sha256:
            raise StoreValidationError("VLM request identity hash is invalid")
        for field_name, value in identity.items():
            if field_name.endswith("_sha256"):
                if type(value) is not str:  # noqa: E721
                    raise StoreValidationError(
                        f"VLM request identity {field_name} must be a SHA-256 string"
                    )
                _sha256(value, f"request_identity.{field_name}")
            elif type(value) is not str or not value.strip():  # noqa: E721
                raise StoreValidationError(
                    f"VLM request identity {field_name} must be non-empty text"
                )
        _mapping_blob_ref(payload["proxy_blob"], "proxy_blob")
        if (
            payload["attempt_id"] != str(self.attempt_id)
            or payload["episode_index"] != self.episode_index
            or payload["idempotency_key"] != self.idempotency_key
            or payload["provider_idempotency_key"] != self.provider_idempotency_key
            or payload["request_hash"] != self.request_hash
            or payload["request_identity_sha256"] != self.request_identity_sha256
            or payload["source_manifest_sha256"] != self.source_manifest_sha256
            or payload["source_provenance_sha256"] != self.source_provenance_sha256
            or payload["window_manifest_sha256"] != self.window_manifest_sha256
            or payload["window_manifest_set_sha256"] != self.window_manifest_set_sha256
            or identity.get("window_manifest_sha256") != self.window_manifest_sha256
            or identity.get("window_manifest_set_sha256")
            != self.window_manifest_set_sha256
            or identity.get("request_payload_sha256") != self.request_payload.content_hash
            or _mapping_blob_ref(payload["request_payload_blob"], "request_payload_blob")
            != self.request_payload
        ):
            raise StoreValidationError(
                "VLM request record does not match its committed generation identity"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_set_id": str(self.artifact_set_id),
            "episode_index": self.episode_index,
            "generation_attempt_id": str(self.attempt_id),
            "idempotency_key": self.idempotency_key,
            "receipt_id": str(self.receipt_id),
            "request_hash": self.request_hash,
            "request_identity_sha256": self.request_identity_sha256,
            "request_payload_blob": {
                "byte_length": self.request_payload.byte_length,
                "content_hash": self.request_payload.content_hash,
                "media_type": self.request_payload.media_type,
                "object_id": str(self.request_payload.object_id),
            },
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_provenance_sha256": self.source_provenance_sha256,
            "state": "succeeded",
            "window_manifest_set_sha256": self.window_manifest_set_sha256,
            "window_manifest_sha256": self.window_manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class PersistedVlmObservationSet:
    """Strictly decoded observation evidence bound to one committed child."""

    reference: VlmObservationSetReference
    payload_json: str
    observation_set: VlmObservationSet
    source_child: PersistedVlmGenerationChild

    def __post_init__(self) -> None:
        if type(self.reference) is not VlmObservationSetReference:  # noqa: E721
            raise StoreValidationError("VLM observation-set reference is invalid")
        if type(self.observation_set) is not VlmObservationSet:  # noqa: E721
            raise StoreValidationError("VLM observation-set value is invalid")
        if type(self.source_child) is not PersistedVlmGenerationChild:  # noqa: E721
            raise StoreValidationError("VLM observation source child is invalid")
        if self.reference.scope != canonical_recipe_scope(self.source_child.source_job):
            raise StoreValidationError("VLM observation set has a non-canonical Job scope")
        expected_logical_id = (
            f"evidence_{self.source_child.window_manifest_sha256[7:39]}"
        )
        if self.reference.logical_id != expected_logical_id:
            raise StoreValidationError("VLM observation logical identity is invalid")
        if canonical_payload_hash(self.payload_json) != self.reference.content_hash:
            raise StoreValidationError(
                "VLM observation payload does not match its artifact hash"
            )
        decoded_payload_json = json.dumps(
            self.observation_set.to_mapping(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical_payload_hash(decoded_payload_json) != self.reference.content_hash:
            raise StoreValidationError(
                "decoded VLM observation set does not match its artifact hash"
            )
        if (
            self.observation_set.request_identity_sha256
            != self.source_child.request_identity_sha256
            or self.observation_set.window_manifest_sha256
            != self.source_child.window_manifest_sha256
        ):
            raise StoreValidationError(
                "VLM observation set does not match its committed request provenance"
            )


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
class BlobRef:
    """Kernel-visible identity for immutable bytes, without a storage locator."""

    object_id: UUID
    content_hash: str
    byte_length: int
    media_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.object_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("blob object_id must be a UUID")
        _sha256(self.content_hash, "blob.content_hash")
        if type(self.byte_length) is not int or self.byte_length < 0:  # noqa: E721
            raise StoreValidationError("blob.byte_length must be a non-negative integer")
        _text(self.media_type, "blob.media_type")


@dataclass(frozen=True, slots=True)
class GenerationAttempt:
    """One durable, versioned provider invocation owned by a command slot."""

    attempt_id: UUID
    job_id: UUID
    command_slot_id: UUID
    request_hash: str
    provider_id: str
    provider_idempotency_key: str
    request_payload: BlobRef
    state: GenerationAttemptState
    version: int
    provider_request_id: str | None = None
    raw_response: BlobRef | None = None
    receipt_id: UUID | None = None
    artifact_set_id: UUID | None = None
    failure_code: str | None = None
    failure_detail_json: str | None = None
    attempt_ordinal: int = 1
    previous_attempt_id: UUID | None = None
    retry_policy_hash: str = _LEGACY_GENERATION_RETRY_POLICY_HASH
    max_attempts: int = 1
    failure_disposition: GenerationFailureDisposition | None = None
    dispatch_lease_token: str | None = None
    dispatch_lease_expires_at: datetime | None = None
    not_before_at: datetime | None = None
    retry_backoff_seconds: int = 0
    is_fresh_reservation: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        for field_name, value in (
            ("attempt_id", self.attempt_id),
            ("job_id", self.job_id),
            ("command_slot_id", self.command_slot_id),
        ):
            if not isinstance(value, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise StoreValidationError(f"{field_name} must be a UUID")
        _sha256(self.request_hash, "generation.request_hash")
        _text(self.provider_id, "generation.provider_id")
        _text(
            self.provider_idempotency_key,
            "generation.provider_idempotency_key",
        )
        if type(self.request_payload) is not BlobRef:  # noqa: E721
            raise StoreValidationError(
                "generation.request_payload must be an exact BlobRef"
            )
        if self.state not in (
            "reserved",
            "dispatched",
            "responded",
            "indeterminate",
            "reconciled",
            "committed",
            "failed",
        ):
            raise StoreValidationError("generation attempt has an unsupported state")
        if type(self.version) is not int or self.version < 0:  # noqa: E721
            raise StoreValidationError("generation version must be a non-negative integer")
        if type(self.attempt_ordinal) is not int or self.attempt_ordinal < 1:  # noqa: E721
            raise StoreValidationError("generation attempt_ordinal must be positive")
        if type(self.max_attempts) is not int or self.max_attempts < self.attempt_ordinal:  # noqa: E721
            raise StoreValidationError(
                "generation max_attempts must cover the attempt ordinal"
            )
        _sha256(self.retry_policy_hash, "generation.retry_policy_hash")
        if self.attempt_ordinal == 1:
            if self.previous_attempt_id is not None:
                raise StoreValidationError("first generation attempt cannot have a predecessor")
        elif not isinstance(self.previous_attempt_id, UUID):
            raise StoreValidationError("later generation attempt requires a predecessor UUID")
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "generation.provider_request_id")
        if self.state in ("responded", "reconciled", "committed") and self.raw_response is None:
            raise StoreValidationError(f"{self.state} generation requires an exact raw-response BlobRef")
        if self.state in ("reserved", "dispatched", "indeterminate") and self.raw_response is not None:
            raise StoreValidationError(f"{self.state} generation cannot claim a raw response")
        if self.state == "committed":
            if self.receipt_id is None or self.artifact_set_id is None:
                raise StoreValidationError("committed generation requires receipt and artifact set bindings")
        elif self.receipt_id is not None or self.artifact_set_id is not None:
            raise StoreValidationError("only committed generation may bind a receipt or artifact set")
        if self.state == "failed":
            _text(self.failure_code or "", "generation.failure_code")
            _text(self.failure_detail_json or "", "generation.failure_detail_json")
            try:
                detail = json.loads(self.failure_detail_json or "")
            except (TypeError, ValueError) as error:
                raise StoreValidationError("generation.failure_detail_json must contain JSON") from error
            if not isinstance(detail, dict):
                raise StoreValidationError("generation.failure_detail_json must contain a JSON object")
            if self.failure_disposition not in (
                "retryable",
                "nonretryable",
                "repairable",
            ):
                raise StoreValidationError(
                    "failed generation requires a closed failure disposition"
                )
        elif self.failure_code is not None or self.failure_detail_json is not None:
            raise StoreValidationError("only failed generation may contain failure diagnostics")
        elif self.failure_disposition is not None:
            raise StoreValidationError(
                "only failed generation may contain a failure disposition"
            )
        if (self.dispatch_lease_token is None) != (
            self.dispatch_lease_expires_at is None
        ):
            raise StoreValidationError(
                "generation dispatch lease token and expiry must be paired"
            )
        if self.dispatch_lease_token is not None:
            _text(self.dispatch_lease_token, "generation.dispatch_lease_token")
            if (
                not isinstance(self.dispatch_lease_expires_at, datetime)
                or self.dispatch_lease_expires_at.tzinfo is None
            ):
                raise StoreValidationError(
                    "generation dispatch lease expiry must be timezone-aware"
                )
            if self.state not in ("dispatched", "indeterminate"):
                raise StoreValidationError(
                    "only dispatched or reconciling generation may hold a lease"
                )
        if self.state == "dispatched" and self.dispatch_lease_token is None:
            raise StoreValidationError("dispatched generation requires an active lease identity")
        if self.not_before_at is not None and (
            self.not_before_at.tzinfo is None
        ):
            raise StoreValidationError(
                "generation not_before_at must be timezone-aware when present"
            )
        if (
            type(self.retry_backoff_seconds) is not int  # noqa: E721
            or self.retry_backoff_seconds < 0
        ):
            raise StoreValidationError(
                "generation retry_backoff_seconds must be non-negative"
            )
        if self.attempt_ordinal == 1 and self.retry_backoff_seconds != 0:
            raise StoreValidationError("first generation attempt cannot have retry backoff")

    def retry_delay_is_active(self, now: datetime | None = None) -> bool:
        if self.not_before_at is None:
            return False
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            raise StoreValidationError("retry delay comparison time must be timezone-aware")
        return self.not_before_at > reference

    def dispatch_lease_is_active(self, now: datetime | None = None) -> bool:
        if self.dispatch_lease_expires_at is None:
            return False
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            raise StoreValidationError("dispatch lease comparison time must be timezone-aware")
        return self.dispatch_lease_expires_at > reference


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
