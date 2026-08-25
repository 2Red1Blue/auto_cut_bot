"""Closed, semantic records for the local Pipeline persistence core."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID

from ..source_manifest import SourceOperationGrant
from ..vlm.models import VlmRequestIdentity, VlmSemanticPack
from .errors import StoreValidationError

CommandOutcomeKind = Literal["pending", "running", "succeeded", "denied", "failed"]
JobProfile = Literal["test", "shadow", "production", "authority"]
SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME = "MeasureShadowCalibrationCommand@2.1.3"
SHADOW_CALIBRATION_MEASUREMENT_PROTOCOL = "shadow-calibration-measurement-v1"
ShadowMeasurementAttemptState = Literal[
    "prepared", "collecting", "ready", "indeterminate", "committed"
]
ShadowMeasurementMemberState = Literal["pending", "invoking", "staged", "indeterminate"]
VLM_BATCH_FINALIZER_COMMAND_NAME = "FinalizeVlmBatchCommand"
VLM_BATCH_IDEMPOTENCY_PREFIX = "vlm-batch:"
VLM_BATCH_FINALIZER_STRATEGY_VERSION = "vlm-batch-finalizer-v1"
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
    source_job: Job | None = field(default=None, compare=False)

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
        if self.source_job is not None:
            if type(self.source_job) is not Job:  # noqa: E721
                raise StoreValidationError("source manifest source_job must be a Job")
            if self.reference.scope != canonical_recipe_scope(self.source_job):
                raise StoreValidationError(
                    "source manifest source_job does not match its artifact scope"
                )

    def provenance_mapping(self) -> dict[str, object]:
        """Return the source-preparation owner identity used by VLM children."""

        reference = self.reference
        return {
            "artifact_reference": {
                "artifact_type": reference.artifact_type,
                "content_hash": reference.content_hash,
                "logical_id": reference.logical_id,
                "revision": reference.revision,
                "scope": {
                    "key": reference.scope.key,
                    "kind": reference.scope.kind,
                    "namespace": reference.scope.namespace,
                },
            },
            "artifact_set_id": str(self.artifact_set_id),
            "command_slot_id": str(self.command_slot_id),
            "kernel_job_id": str(self.job_id),
            "receipt_id": str(self.receipt_id),
            "source_job": {
                "job_key": self.reference.scope.key,
                "profile": self.source_profile,
            },
        }

    @property
    def source_profile(self) -> JobProfile:
        """The exact durable profile is filled by the committed reader result."""

        if self.source_job is None:
            raise StoreValidationError("source manifest profile is unavailable")
        return self.source_job.profile

    @property
    def canonical_hash(self) -> str:
        return canonical_payload_hash(json.dumps(self.provenance_mapping()))


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
class VlmSemanticPackReference:
    """Exact immutable identity of one committed VLM Semantic Pack."""

    scope: ArtifactScope
    logical_id: str
    revision: int
    content_hash: str
    artifact_type: Literal["vlm_semantic_pack"] = "vlm_semantic_pack"

    def __post_init__(self) -> None:
        _text(self.logical_id, "logical_id")
        if type(self.revision) is not int or self.revision < 1:  # noqa: E721
            raise StoreValidationError("revision must be a positive integer")
        _sha256(self.content_hash, "content_hash")
        if self.artifact_type != "vlm_semantic_pack":
            raise StoreValidationError(
                "VLM Semantic Pack reference has an invalid artifact_type"
            )


VLM_REQUEST_IDENTITY_FIELDS = frozenset(
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
        if frozenset(identity) != VLM_REQUEST_IDENTITY_FIELDS:
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

    @property
    def request_policy(self) -> VlmBatchRequestPolicy:
        payload = _strict_json_mapping(self.payload_json, "VLM request record")
        identity = cast(dict[str, object], payload["request_identity"])
        return VlmBatchRequestPolicy(
            prompt_template_sha256=cast(str, identity["prompt_template_sha256"]),
            prompt_version=cast(str, identity["prompt_version"]),
            response_schema_sha256=cast(str, identity["response_schema_sha256"]),
            preprocess_policy_sha256=cast(str, identity["preprocess_policy_sha256"]),
            window_sampling_policy_sha256=cast(
                str, identity["window_sampling_policy_sha256"]
            ),
            model_id=cast(str, identity["model_id"]),
            provider_id=cast(str, identity["provider_id"]),
            request_parameters_sha256=cast(str, identity["request_parameters_sha256"]),
            parse_policy_sha256=cast(str, identity["parse_policy_sha256"]),
        )


@dataclass(frozen=True, slots=True)
class PersistedVlmSemanticPack:
    """Strictly decoded Semantic Pack bound to one committed VLM child."""

    reference: VlmSemanticPackReference
    payload_json: str
    semantic_pack: VlmSemanticPack
    source_child: PersistedVlmGenerationChild

    def __post_init__(self) -> None:
        if type(self.reference) is not VlmSemanticPackReference:  # noqa: E721
            raise StoreValidationError("VLM Semantic Pack reference is invalid")
        if type(self.semantic_pack) is not VlmSemanticPack:  # noqa: E721
            raise StoreValidationError("VLM Semantic Pack value is invalid")
        if type(self.source_child) is not PersistedVlmGenerationChild:  # noqa: E721
            raise StoreValidationError("VLM Semantic Pack source child is invalid")
        if self.reference.scope != canonical_recipe_scope(self.source_child.source_job):
            raise StoreValidationError("VLM Semantic Pack has a non-canonical Job scope")
        expected_logical_id = (
            f"semantic_pack_{self.source_child.window_manifest_sha256[7:39]}"
        )
        if self.reference.logical_id != expected_logical_id:
            raise StoreValidationError("VLM Semantic Pack logical identity is invalid")
        if canonical_payload_hash(self.payload_json) != self.reference.content_hash:
            raise StoreValidationError(
                "VLM Semantic Pack payload does not match its artifact hash"
            )
        decoded_payload_json = json.dumps(
            self.semantic_pack.to_mapping(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical_payload_hash(decoded_payload_json) != self.reference.content_hash:
            raise StoreValidationError(
                "decoded VLM Semantic Pack does not match its artifact hash"
            )
        if (
            self.semantic_pack.request_identity_sha256
            != self.source_child.request_identity_sha256
            or self.semantic_pack.window_manifest_sha256
            != self.source_child.window_manifest_sha256
        ):
            raise StoreValidationError(
                "VLM Semantic Pack does not match its committed request provenance"
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
class MaterializationLimits:
    """Explicit bounded-transfer controls for one timed-media command."""

    max_source_bytes: int
    timed_speech_max_request_bytes: int
    copy_chunk_bytes: int
    staging_quota_bytes: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_source_bytes",
            "timed_speech_max_request_bytes",
            "copy_chunk_bytes",
            "staging_quota_bytes",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:  # noqa: E721
                raise StoreValidationError(f"{field_name} must be a positive integer")
        if self.copy_chunk_bytes > self.max_source_bytes:
            raise StoreValidationError("copy_chunk_bytes must not exceed max_source_bytes")

    @property
    def evidence_policy_sha256(self) -> str:
        """Hash the frozen kernel/service source ceilings, excluding host-only controls."""

        return canonical_payload_hash(
            json.dumps(
                {
                    "max_source_bytes": self.max_source_bytes,
                    "timed_speech_max_request_bytes": self.timed_speech_max_request_bytes,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    @property
    def policy_sha256(self) -> str:
        """Hash every frozen transfer limit used to construct a command."""

        return canonical_payload_hash(
            json.dumps(
                {
                    "copy_chunk_bytes": self.copy_chunk_bytes,
                    "max_source_bytes": self.max_source_bytes,
                    "staging_quota_bytes": self.staging_quota_bytes,
                    "timed_speech_max_request_bytes": self.timed_speech_max_request_bytes,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    @property
    def effective_max_source_bytes(self) -> int:
        """The one source ceiling enforced before private staging and dispatch."""

        return min(self.max_source_bytes, self.timed_speech_max_request_bytes)


class VerifiedMaterializedBlob(Protocol):
    """One command-private, verified immutable BlobRef lease."""

    reference: BlobRef
    path: Path

    def close(self) -> None:
        """Idempotently remove private bytes and release the quota lease."""


class MaterializationError(RuntimeError):
    """Closed failure from bounded private source materialization."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        outcome: Literal["denied", "failed"],
    ) -> None:
        if code not in {
            "COMMITTED_SOURCE_BLOB_INTEGRITY_FAILED",
            "MEDIA_SOURCE_BYTE_LIMIT_EXCEEDED",
            "MEDIA_MATERIALIZATION_CAPACITY_BUSY",
            "MEDIA_MATERIALIZATION_QUOTA_CONFIGURATION_MISMATCH",
            "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED",
        }:
            raise ValueError("materialization failure code is unsupported")
        self.code = code
        self.detail = detail
        self.outcome: Literal["denied", "failed"] = outcome
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class CommittedArtifactMemberReference:
    """Complete identity of one member in one succeeded committed ArtifactSet."""

    receipt_id: UUID
    artifact_set_id: UUID
    member_ordinal: int
    scope: ArtifactScope
    artifact_type: str
    logical_id: str
    revision: int
    content_hash: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("receipt_id", self.receipt_id),
            ("artifact_set_id", self.artifact_set_id),
        ):
            if not isinstance(value, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise StoreValidationError(f"{field_name} must be a UUID")
        if type(self.member_ordinal) is not int or self.member_ordinal < 0:  # noqa: E721
            raise StoreValidationError("member_ordinal must be a non-negative integer")
        if type(self.scope) is not ArtifactScope:  # noqa: E721
            raise StoreValidationError("committed member scope must be an ArtifactScope")
        _text(self.artifact_type, "artifact_type")
        _text(self.logical_id, "logical_id")
        if type(self.revision) is not int or self.revision < 1:  # noqa: E721
            raise StoreValidationError("revision must be a positive integer")
        _sha256(self.content_hash, "content_hash")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_set_id": str(self.artifact_set_id),
            "artifact_type": self.artifact_type,
            "content_hash": self.content_hash,
            "logical_id": self.logical_id,
            "member_ordinal": self.member_ordinal,
            "receipt_id": str(self.receipt_id),
            "revision": self.revision,
            "scope": {
                "key": self.scope.key,
                "kind": self.scope.kind,
                "namespace": self.scope.namespace,
            },
        }


@dataclass(frozen=True, slots=True)
class PersistedCommittedArtifactMember:
    """One exact member reread from a succeeded immutable ArtifactSet.

    Unlike logical-head readers, this is intentionally useful to predecessor
    contracts that must consume an authority member by its full Receipt/Set
    identity.  The payload hash is verified here before a media command parses
    its closed schema.
    """

    reference: CommittedArtifactMemberReference
    payload_json: str
    command_slot_id: UUID

    def __post_init__(self) -> None:
        if type(self.reference) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError("persisted committed member requires an exact reference")
        _text(self.payload_json, "persisted committed member payload_json")
        if canonical_payload_hash(self.payload_json) != self.reference.content_hash:
            raise StoreValidationError(
                "persisted committed member payload does not match its content hash"
            )
        if not isinstance(self.command_slot_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StoreValidationError("persisted committed member command_slot_id must be a UUID")


@dataclass(frozen=True, slots=True)
class VlmBatchRequestPolicy:
    """Frozen provider policy shared by every child in one semantic-pack set."""

    prompt_template_sha256: str
    prompt_version: str
    response_schema_sha256: str
    preprocess_policy_sha256: str
    window_sampling_policy_sha256: str
    model_id: str
    provider_id: str
    request_parameters_sha256: str
    parse_policy_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "prompt_template_sha256",
            "response_schema_sha256",
            "preprocess_policy_sha256",
            "window_sampling_policy_sha256",
            "request_parameters_sha256",
            "parse_policy_sha256",
        ):
            _sha256(getattr(self, field_name), f"VLM batch policy {field_name}")
        for field_name in ("prompt_version", "model_id", "provider_id"):
            _text(getattr(self, field_name), f"VLM batch policy {field_name}")

    def to_mapping(self) -> dict[str, str]:
        return {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class VlmSemanticPackSetChild:
    """Ordered exact member references for one committed VLM child."""

    episode_index: int
    idempotency_key: str
    request_hash: str
    request_record: CommittedArtifactMemberReference
    response_record: CommittedArtifactMemberReference
    semantic_pack: CommittedArtifactMemberReference

    def __post_init__(self) -> None:
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise StoreValidationError("VLM pack-set episode_index must be non-negative")
        _text(self.idempotency_key, "VLM pack-set idempotency_key")
        _sha256(self.request_hash, "VLM pack-set request_hash")
        members = (self.request_record, self.response_record, self.semantic_pack)
        if any(type(item) is not CommittedArtifactMemberReference for item in members):  # noqa: E721
            raise StoreValidationError("VLM pack-set children require exact member references")
        if tuple(item.member_ordinal for item in members) != (0, 1, 2):
            raise StoreValidationError("VLM pack-set child member order is invalid")
        if tuple(item.artifact_type for item in members) != (
            "vlm_request_record",
            "vlm_response_record",
            "vlm_semantic_pack",
        ):
            raise StoreValidationError("VLM pack-set child member types are invalid")
        if len({item.receipt_id for item in members}) != 1 or len(
            {item.artifact_set_id for item in members}
        ) != 1:
            raise StoreValidationError("VLM pack-set child members must share a Receipt/ArtifactSet")
        if len({item.scope for item in members}) != 1 or len(
            {item.revision for item in members}
        ) != 1:
            raise StoreValidationError(
                "VLM pack-set child members must share scope/revision"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "episode_index": self.episode_index,
            "idempotency_key": self.idempotency_key,
            "request_hash": self.request_hash,
            "request_record": self.request_record.to_mapping(),
            "response_record": self.response_record.to_mapping(),
            "semantic_pack": self.semantic_pack.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class CommittedVlmInputReference:
    """Exact three-member VLM set and every immutable BlobRef it owns."""

    request_record: CommittedArtifactMemberReference
    response_record: CommittedArtifactMemberReference
    semantic_pack: CommittedArtifactMemberReference
    proxy_blob: BlobRef
    request_payload: BlobRef
    raw_response: BlobRef

    def __post_init__(self) -> None:
        members = (self.request_record, self.response_record, self.semantic_pack)
        if any(type(item) is not CommittedArtifactMemberReference for item in members):  # noqa: E721
            raise StoreValidationError(
                "committed VLM members must be exact member references"
            )
        if any(type(item) is not BlobRef for item in (self.proxy_blob, self.request_payload, self.raw_response)):  # noqa: E721
            raise StoreValidationError("committed VLM blobs must be exact BlobRefs")


@dataclass(frozen=True, slots=True)
class CommittedSemanticInputsRequest:
    """Closed Stage 1-3 request; no path, dictionary, head, or hash-only input."""

    job: Job
    source_manifest: CommittedArtifactMemberReference
    vlm_semantic_pack_set: CommittedArtifactMemberReference

    def __post_init__(self) -> None:
        if type(self.job) is not Job:  # noqa: E721
            raise StoreValidationError("semantic input job must be a Job")
        if type(self.source_manifest) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError(
                "semantic source manifest must be an exact member reference"
            )
        if type(self.vlm_semantic_pack_set) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError(
                "semantic pack set must be an exact member reference"
            )
        if (
            self.vlm_semantic_pack_set.artifact_type != "vlm_semantic_pack_set"
            or self.vlm_semantic_pack_set.logical_id != "vlm_semantic_pack_set"
            or self.vlm_semantic_pack_set.member_ordinal != 0
        ):
            raise StoreValidationError("semantic pack set member identity is invalid")


@dataclass(frozen=True, slots=True)
class SourceWindowIdentity:
    """Owner-bound Source/Window projection safe for Stage 1-3 semantics."""

    episode_index: int
    stream_index: int
    core_start_pts: int
    core_end_pts: int
    window_manifest_sha256: str
    source_id: str
    source_sha256: str
    source_clock_id: str
    window_manifest_set_sha256: str
    proxy_blob: BlobRef

    def __post_init__(self) -> None:
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise StoreValidationError("source window episode_index must be non-negative")
        _text(self.source_id, "source window source_id")
        _text(self.source_clock_id, "source window source_clock_id")
        for field_name in (
            "source_sha256",
            "window_manifest_sha256",
            "window_manifest_set_sha256",
        ):
            _sha256(getattr(self, field_name), f"source window {field_name}")
        if type(self.proxy_blob) is not BlobRef:  # noqa: E721
            raise StoreValidationError("source window proxy_blob must be a BlobRef")
        if type(self.stream_index) is not int or self.stream_index < 0:  # noqa: E721
            raise StoreValidationError("source window stream_index must be non-negative")
        if (
            type(self.core_start_pts) is not int  # noqa: E721
            or type(self.core_end_pts) is not int  # noqa: E721
            or self.core_start_pts >= self.core_end_pts
        ):
            raise StoreValidationError("source window core range is invalid")

    @property
    def canonical_order_key(self) -> tuple[object, ...]:
        return (
            self.episode_index,
            self.stream_index,
            self.core_start_pts,
            self.core_end_pts,
            self.window_manifest_sha256,
        )


@dataclass(frozen=True, slots=True)
class CommittedVlmSemanticInput:
    """One exact VLM Semantic Pack joined to its committed Source window."""

    source_window: SourceWindowIdentity
    request_identity: VlmRequestIdentity
    semantic_pack: PersistedVlmSemanticPack
    response_record: CommittedArtifactMemberReference
    raw_response: BlobRef

    def __post_init__(self) -> None:
        if type(self.source_window) is not SourceWindowIdentity:  # noqa: E721
            raise StoreValidationError("VLM semantic source_window is invalid")
        if type(self.request_identity) is not VlmRequestIdentity:  # noqa: E721
            raise StoreValidationError("VLM semantic request_identity is invalid")
        if type(self.semantic_pack) is not PersistedVlmSemanticPack:  # noqa: E721
            raise StoreValidationError("VLM semantic pack is invalid")
        if type(self.response_record) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError("VLM semantic response_record is invalid")
        if type(self.raw_response) is not BlobRef:  # noqa: E721
            raise StoreValidationError("VLM semantic raw_response is invalid")


@dataclass(frozen=True, slots=True)
class CommittedSemanticInputs:
    """Exact committed Source/Window/Doubao inputs consumed by Stage 1-3."""

    source_manifest: PersistedWholeSeriesSourceManifest
    source_grant: SourceOperationGrant
    vlm_semantic_pack_set: CommittedArtifactMemberReference
    vlm_aggregate_policy: VlmBatchRequestPolicy
    inputs: tuple[CommittedVlmSemanticInput, ...]

    def __post_init__(self) -> None:
        if type(self.source_manifest) is not PersistedWholeSeriesSourceManifest:  # noqa: E721
            raise StoreValidationError("semantic source_manifest is invalid")
        if type(self.source_grant) is not SourceOperationGrant:  # noqa: E721
            raise StoreValidationError("semantic source_grant is invalid")
        if type(self.vlm_semantic_pack_set) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError("semantic VLM aggregate member is invalid")
        if type(self.vlm_aggregate_policy) is not VlmBatchRequestPolicy:  # noqa: E721
            raise StoreValidationError("semantic VLM aggregate policy is invalid")
        if (
            self.vlm_semantic_pack_set.artifact_type != "vlm_semantic_pack_set"
            or self.vlm_semantic_pack_set.logical_id != "vlm_semantic_pack_set"
            or self.vlm_semantic_pack_set.member_ordinal != 0
        ):
            raise StoreValidationError("semantic VLM aggregate member identity is invalid")
        inputs = tuple(self.inputs)
        if not inputs or any(type(item) is not CommittedVlmSemanticInput for item in inputs):  # noqa: E721
            raise StoreValidationError("semantic inputs must contain committed VLM inputs")
        order_keys = tuple(item.source_window.canonical_order_key for item in inputs)
        window_ids = tuple(
            item.source_window.window_manifest_sha256 for item in inputs
        )
        if order_keys != tuple(sorted(order_keys)) or len(window_ids) != len(
            set(window_ids)
        ):
            raise StoreValidationError(
                "semantic inputs must have unique canonical Source/Window order"
            )
        object.__setattr__(self, "inputs", inputs)


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


def _strict_canonical_json_object(value: str, field_name: str) -> dict[str, object]:
    """Read the small closed JSON values persisted by the shadow owner."""

    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, member in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = member
        return result

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {constant}")
            ),
        )
    except (TypeError, ValueError) as error:
        raise StoreValidationError(f"{field_name} must contain strict JSON") from error
    if type(parsed) is not dict:  # noqa: E721
        raise StoreValidationError(f"{field_name} must contain a JSON object")
    canonical = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if value != canonical:
        raise StoreValidationError(f"{field_name} must be canonical JSON")
    return cast(dict[str, object], parsed)


@dataclass(frozen=True, slots=True)
class ShadowMeasurementMemberPlan:
    """One immutable native invocation/context pair in a shadow attempt plan."""

    corpus_member_reference_sha256: str
    member_ordinal: int
    invocation_json: str
    context_json: str
    expected_anchor_reference_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.corpus_member_reference_sha256, "shadow member reference")
        _sha256(self.expected_anchor_reference_sha256, "shadow member anchor reference")
        if type(self.member_ordinal) is not int or self.member_ordinal < 0:  # noqa: E721
            raise StoreValidationError("shadow member ordinal must be non-negative")
        _strict_canonical_json_object(self.invocation_json, "shadow member invocation_json")
        _strict_canonical_json_object(self.context_json, "shadow member context_json")


@dataclass(frozen=True, slots=True)
class ShadowMeasurementPlan:
    """Closed, canonical recovery input for exactly one shadow command claim."""

    claim: CommandClaim
    canonical_plan_json: str
    members: tuple[ShadowMeasurementMemberPlan, ...]

    def __post_init__(self) -> None:
        if type(self.claim) is not CommandClaim:  # noqa: E721
            raise StoreValidationError("shadow measurement plan requires an exact CommandClaim")
        if self.claim.command_name != SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME:
            raise StoreValidationError("shadow measurement plan requires the exact measurement command")
        if self.claim.job.profile != "shadow":
            raise StoreValidationError("shadow measurement plan requires a shadow Job")
        payload = _strict_canonical_json_object(
            self.canonical_plan_json, "shadow measurement canonical_plan_json"
        )
        if canonical_payload_hash(self.canonical_plan_json) != self.claim.request_hash:
            raise StoreValidationError("shadow measurement plan does not match claim request_hash")
        if set(payload) != {
            "command",
            "corpus_members",
            "measurement_protocol",
            "shadow_inputs",
        }:
            raise StoreValidationError("shadow measurement plan shape is invalid")
        if payload.get("command") != SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME:
            raise StoreValidationError("shadow measurement plan command is invalid")
        if payload.get("measurement_protocol") != SHADOW_CALIBRATION_MEASUREMENT_PROTOCOL:
            raise StoreValidationError("shadow measurement plan protocol is invalid")
        expected_key = self.claim.request_hash.removeprefix("sha256:")
        if self.claim.job.job_key != expected_key or self.claim.idempotency_key != (
            f"shadow-calibration:{expected_key}"
        ):
            raise StoreValidationError("shadow measurement plan claim identity is invalid")
        members = tuple(self.members)
        if not members or any(type(member) is not ShadowMeasurementMemberPlan for member in members):  # noqa: E721
            raise StoreValidationError("shadow measurement plan requires exact member plans")
        if tuple(member.member_ordinal for member in members) != tuple(range(len(members))):
            raise StoreValidationError("shadow measurement member ordinals must be contiguous")
        if len({member.corpus_member_reference_sha256 for member in members}) != len(members):
            raise StoreValidationError("shadow measurement plan must not duplicate member references")
        plan_members = payload.get("corpus_members")
        if not isinstance(plan_members, list):
            raise StoreValidationError("shadow measurement plan member set is invalid")
        plan_member_values = cast(list[object], plan_members)
        if len(plan_member_values) != len(members):
            raise StoreValidationError("shadow measurement plan member set is invalid")
        for member, encoded in zip(members, plan_member_values, strict=True):
            if not isinstance(encoded, dict):
                raise StoreValidationError("shadow measurement plan member shape is invalid")
            encoded_member = cast(dict[str, object], encoded)
            if set(encoded_member) != {
                "corpus_member_reference_sha256",
                "expected_anchor_reference_sha256",
                "native_invocation",
                "raw_context",
            }:
                raise StoreValidationError("shadow measurement plan member shape is invalid")
            if (
                encoded_member["corpus_member_reference_sha256"] != member.corpus_member_reference_sha256
                or encoded_member["expected_anchor_reference_sha256"]
                != member.expected_anchor_reference_sha256
                or json.dumps(encoded_member["native_invocation"], ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                != member.invocation_json
                or json.dumps(encoded_member["raw_context"], ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                != member.context_json
            ):
                raise StoreValidationError("shadow measurement member plan drifts from canonical plan")
        object.__setattr__(self, "members", members)


@dataclass(frozen=True, slots=True)
class ShadowMeasurementMember:
    attempt_id: UUID
    corpus_member_reference_sha256: str
    member_ordinal: int
    invocation_json: str
    context_json: str
    expected_anchor_reference_sha256: str
    state: ShadowMeasurementMemberState
    version: int
    raw_blob: BlobRef | None = None
    projection_json: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _sha256(self.corpus_member_reference_sha256, "shadow member reference")
        _sha256(self.expected_anchor_reference_sha256, "shadow member anchor reference")
        if self.state not in ("pending", "invoking", "staged", "indeterminate"):
            raise StoreValidationError("shadow member state is unsupported")
        if type(self.version) is not int or self.version < 0:  # noqa: E721
            raise StoreValidationError("shadow member version must be non-negative")
        _strict_canonical_json_object(self.invocation_json, "shadow member invocation_json")
        _strict_canonical_json_object(self.context_json, "shadow member context_json")
        if self.state == "staged":
            if type(self.raw_blob) is not BlobRef or self.projection_json is None:  # noqa: E721
                raise StoreValidationError("staged shadow member requires BlobRef and projection")
            _strict_canonical_json_object(self.projection_json, "shadow member projection_json")
        elif self.raw_blob is not None or self.projection_json is not None:
            raise StoreValidationError("only staged shadow members may bind raw evidence")


@dataclass(frozen=True, slots=True)
class ShadowMeasurementAttempt:
    attempt_id: UUID
    command_slot_id: UUID
    job: Job
    plan_hash: str
    canonical_plan_json: str
    attempt_ordinal: int
    previous_attempt_id: UUID | None
    state: ShadowMeasurementAttemptState
    version: int
    members: tuple[ShadowMeasurementMember, ...]
    outcome: CommandOutcome
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in ("attempt_id", "command_slot_id"):
            if not isinstance(getattr(self, field_name), UUID):
                raise StoreValidationError(f"shadow attempt {field_name} must be a UUID")
        if type(self.job) is not Job:  # noqa: E721
            raise StoreValidationError("shadow attempt job must be exact")
        _sha256(self.plan_hash, "shadow attempt plan_hash")
        _strict_canonical_json_object(self.canonical_plan_json, "shadow attempt plan_json")
        if type(self.attempt_ordinal) is not int or self.attempt_ordinal < 1:  # noqa: E721
            raise StoreValidationError("shadow attempt ordinal must be positive")
        if self.state not in ("prepared", "collecting", "ready", "indeterminate", "committed"):
            raise StoreValidationError("shadow attempt state is unsupported")
        if type(self.version) is not int or self.version < 0:  # noqa: E721
            raise StoreValidationError("shadow attempt version must be non-negative")
        members = tuple(self.members)
        if not members or any(type(member) is not ShadowMeasurementMember for member in members):  # noqa: E721
            raise StoreValidationError("shadow attempt requires exact members")
        if tuple(member.member_ordinal for member in members) != tuple(range(len(members))):
            raise StoreValidationError("shadow attempt members must be ordered")
        if any(member.attempt_id != self.attempt_id for member in members):
            raise StoreValidationError("shadow attempt member identity drift")
        if type(self.outcome) is not CommandOutcome:  # noqa: E721
            raise StoreValidationError("shadow attempt outcome must be exact")
        if self.outcome.command_slot_id != self.command_slot_id:
            raise StoreValidationError("shadow attempt command outcome drift")
        object.__setattr__(self, "members", members)


@dataclass(frozen=True, slots=True)
class ShadowMeasurementMemberLease:
    member: ShadowMeasurementMember
    attempt_version: int
    lease_token: str

    def __post_init__(self) -> None:
        if type(self.member) is not ShadowMeasurementMember:  # noqa: E721
            raise StoreValidationError("shadow member lease requires a member snapshot")
        if self.member.state != "invoking":
            raise StoreValidationError("shadow member lease requires invoking state")
        if type(self.attempt_version) is not int or self.attempt_version < 0:  # noqa: E721
            raise StoreValidationError("shadow member lease attempt_version is invalid")
        _text(self.lease_token, "shadow member lease_token")


@dataclass(frozen=True, slots=True)
class ShadowMeasurementRecoveryLease:
    attempt: ShadowMeasurementAttempt
    lease_token: str

    def __post_init__(self) -> None:
        if type(self.attempt) is not ShadowMeasurementAttempt:  # noqa: E721
            raise StoreValidationError("shadow recovery lease requires an attempt snapshot")
        _text(self.lease_token, "shadow recovery lease_token")


@dataclass(frozen=True, slots=True)
class ShadowMeasurementStagedResponse:
    """Exact bytes and decoder-derived canonical projection accepted by the Store owner."""

    raw_bytes: bytes
    content_hash: str
    media_type: str
    projection_json: str

    def __post_init__(self) -> None:
        if type(self.raw_bytes) is not bytes:  # noqa: E721
            raise StoreValidationError("shadow staged raw_bytes must be immutable bytes")
        _sha256(self.content_hash, "shadow staged content_hash")
        _text(self.media_type, "shadow staged media_type")
        _strict_canonical_json_object(self.projection_json, "shadow staged projection_json")
        actual = "sha256:" + hashlib.sha256(self.raw_bytes).hexdigest()
        if actual != self.content_hash:
            raise StoreValidationError("shadow staged raw bytes do not match content_hash")


@dataclass(frozen=True, slots=True)
class ShadowMeasurementRetryAuthorization:
    """Bounded authority decision required before an unknown native call may be retried."""

    decision_reference_sha256: str
    predecessor_plan_hash: str
    reason_code: Literal["NATIVE_OUTCOME_UNKNOWN"] = "NATIVE_OUTCOME_UNKNOWN"

    def __post_init__(self) -> None:
        _sha256(self.decision_reference_sha256, "shadow retry decision reference")
        _sha256(self.predecessor_plan_hash, "shadow retry predecessor plan hash")
        if self.reason_code != "NATIVE_OUTCOME_UNKNOWN":
            raise StoreValidationError("shadow retry authorization reason is unsupported")
