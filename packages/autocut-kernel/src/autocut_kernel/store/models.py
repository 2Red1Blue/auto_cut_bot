"""Closed, semantic records for the local Pipeline persistence core."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import UUID

from ..media.calibration_record import (
    CALIBRATION_VALIDATOR_COMMAND,
    CalibrationRecordArtifactSet,
    calibration_profile_key,
    runtime_calibration_profile_key,
)
from ..media.runtime_measurement_identity import RuntimeMeasurementIdentity
from ..source_manifest import (
    SourceManifestDecodeError,
    SourceOperationGrant,
    decode_source_manifest,
)
from ..vlm.models import VlmRequestIdentity, VlmSemanticPack
from ..vlm.semantic_pack_v4 import VlmSemanticPackV4
from .errors import StoreValidationError

if TYPE_CHECKING:
    from ..rendering.production_ffmpeg_renderer import ProductionRenderAttemptFacts

CommandOutcomeKind = Literal["pending", "running", "succeeded", "denied", "failed"]
JobProfile = Literal["test", "shadow", "production", "authority"]
SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME = "MeasureShadowCalibrationCommand@2.1.3"
SHADOW_CALIBRATION_MEASUREMENT_PROTOCOL = "shadow-calibration-measurement-v1"
SHADOW_CALIBRATION_TERMINAL_DENIAL_CODES = frozenset(
    {"SHADOW_CALIBRATION_INVALID", "SHADOW_CALIBRATION_NATIVE_REJECTED"}
)
SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME = "MeasureShadowLocalCalibrationCommand@1"
SHADOW_LOCAL_CALIBRATION_MEASUREMENT_PROTOCOL = "shadow-local-calibration-measurement-v1"
SHADOW_LOCAL_CALIBRATION_TERMINAL_DENIAL_CODES = frozenset({"SHADOW_LOCAL_CALIBRATION_INVALID_RAW"})
ShadowMeasurementAttemptState = Literal[
    "prepared", "collecting", "ready", "indeterminate", "committed"
]
ShadowMeasurementMemberState = Literal["pending", "invoking", "staged", "indeterminate"]
ShadowLocalMeasurementAttemptState = Literal[
    "prepared", "collecting", "ready", "indeterminate", "committed", "denied"
]
ShadowLocalMeasurementMemberState = Literal[
    "pending", "invoking", "not_started", "staged", "indeterminate", "rejected"
]
VLM_BATCH_FINALIZER_COMMAND_NAME = "FinalizeVlmBatchCommand"
VLM_BATCH_IDEMPOTENCY_PREFIX = "vlm-batch:"
VLM_BATCH_FINALIZER_STRATEGY_VERSION = "vlm-batch-finalizer-v1"
VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4 = "vlm-batch-finalizer-v2-semantic-pack-v4"
PRODUCTION_RECIPE_COMMAND_NAME = "CompileProductionRecipeCommand@1"
PRODUCTION_RENDER_COMMAND_NAME = "RenderProductionRecipeCommand@1"
ProductionRenderAttemptState = Literal[
    "reserved", "rendering", "rendered", "committed", "denied", "failed"
]
ProductionRenderQcAttemptState = Literal["reserved", "scanning", "evidence_ready"]
ProductionRenderQcCollectionStatus = Literal[
    "completed", "incomplete", "not_run", "not_applicable"
]
ProductionRenderQcCoverage = Literal["full_file", "partial", "none", "not_applicable"]
ProductionRenderQcMeasurementKind = Literal[
    "integer", "decimal", "rational", "boolean", "text", "sha256"
]
ProductionRenderQcMeasurementUnit = Literal[
    "none",
    "count",
    "byte",
    "tick",
    "second",
    "frame",
    "sample",
    "packet",
    "stream",
    "channel",
    "hertz",
    "decibel",
    "lufs",
    "percent",
    "ratio",
]
PRODUCTION_RENDER_QC_EVIDENCE_SCHEMA_VERSION = "production-render-qc-evidence-v1"
PRODUCTION_RENDER_QC_CHECK_SET_VERSION = "production-av-qc-v1"
PRODUCTION_RENDER_QC_REQUIRED_CHECKS = (
    "exact_object_identity",
    "container_stream_topology",
    "packet_timeline_integrity",
    "decoded_frame_timeline",
    "full_video_decode",
    "full_audio_decode",
    "video_black_intervals",
    "video_freeze_intervals",
    "audio_silence_intervals",
    "audio_sample_health",
    "av_presentation_envelope",
    "edit_junction_continuity",
)
GenerationAttemptState = Literal[
    "reserved",
    "dispatched",
    "responded",
    "indeterminate",
    "reconciled",
    "committed",
    "failed",
]
CommandExecutionKind = Literal["deterministic", "generation"]
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


def _qc_safe_identifier(value: object, field_name: str) -> None:
    if (
        type(value) is not str  # noqa: E721
        or not 1 <= len(value.encode("utf-8")) <= 128
        or re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value) is None
    ):
        raise StoreValidationError(
            f"production render QC {field_name} must be a safe identifier"
        )


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
    artifact_type: Literal["whole_series_source_manifest"] = "whole_series_source_manifest"

    def __post_init__(self) -> None:
        _text(self.logical_id, "logical_id")
        if type(self.revision) is not int or self.revision < 1:  # noqa: E721
            raise StoreValidationError("revision must be a positive integer")
        _sha256(self.content_hash, "content_hash")
        if self.artifact_type != "whole_series_source_manifest":
            raise StoreValidationError("source manifest reference has an invalid artifact_type")


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
            raise StoreValidationError("payload_json does not match source manifest content_hash")
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
            raise StoreValidationError("VLM request record reference has an invalid artifact_type")


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
            raise StoreValidationError("VLM Semantic Pack reference has an invalid artifact_type")


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
_BLOB_REF_FIELDS = frozenset({"object_id", "content_hash", "byte_length", "media_type"})


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
        if (
            type(content_hash) is not str
            or type(byte_length) is not int
            or type(media_type) is not str
        ):  # noqa: E721
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
    # Readback metadata, not new fields in the immutable v3 request-record wire.
    parser_strategy_version: str = "strict-semantic-pack-v3"
    semantic_schema_version: int = 3

    def __post_init__(self) -> None:
        if type(self.semantic_schema_version) is not int or (  # noqa: E721
            self.parser_strategy_version,
            self.semantic_schema_version,
        ) not in (("strict-semantic-pack-v3", 3), ("strict-semantic-pack-v4", 4)):
            raise StoreValidationError("VLM child parser and schema versions disagree")
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
            or identity.get("window_manifest_set_sha256") != self.window_manifest_set_sha256
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
            window_sampling_policy_sha256=cast(str, identity["window_sampling_policy_sha256"]),
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
        expected_logical_id = f"semantic_pack_{self.source_child.window_manifest_sha256[7:39]}"
        if self.reference.logical_id != expected_logical_id:
            raise StoreValidationError("VLM Semantic Pack logical identity is invalid")
        if canonical_payload_hash(self.payload_json) != self.reference.content_hash:
            raise StoreValidationError("VLM Semantic Pack payload does not match its artifact hash")
        decoded_payload_json = json.dumps(
            self.semantic_pack.to_mapping(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical_payload_hash(decoded_payload_json) != self.reference.content_hash:
            raise StoreValidationError("decoded VLM Semantic Pack does not match its artifact hash")
        if (
            self.semantic_pack.request_identity_sha256 != self.source_child.request_identity_sha256
            or self.semantic_pack.window_manifest_sha256 != self.source_child.window_manifest_sha256
        ):
            raise StoreValidationError(
                "VLM Semantic Pack does not match its committed request provenance"
            )


@dataclass(frozen=True, slots=True)
class PersistedVlmSemanticPackV4:
    """A distinct V4 value, never accepted by the frozen V3 wrapper."""

    reference: VlmSemanticPackReference
    payload_json: str
    semantic_pack: VlmSemanticPackV4
    source_child: PersistedVlmGenerationChild

    def __post_init__(self) -> None:
        if (
            type(self.reference) is not VlmSemanticPackReference
            or type(self.semantic_pack) is not VlmSemanticPackV4
            or type(self.source_child) is not PersistedVlmGenerationChild
        ):
            raise StoreValidationError("V4 Semantic Pack requires exact typed values")
        if (
            self.source_child.parser_strategy_version,
            self.source_child.semantic_schema_version,
        ) != ("strict-semantic-pack-v4", 4):
            raise StoreValidationError("V4 Semantic Pack child version is invalid")
        if (
            self.reference.scope != canonical_recipe_scope(self.source_child.source_job)
            or self.reference.logical_id
            != f"semantic_pack_{self.source_child.window_manifest_sha256[7:39]}"
            or canonical_payload_hash(self.payload_json) != self.reference.content_hash
            or canonical_payload_hash(json.dumps(self.semantic_pack.to_mapping()))
            != self.reference.content_hash
            or self.semantic_pack.request_identity_sha256
            != self.source_child.request_identity_sha256
            or self.semantic_pack.window_manifest_sha256 != self.source_child.window_manifest_sha256
        ):
            raise StoreValidationError(
                "V4 Semantic Pack does not match its committed child identity"
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

    @classmethod
    def from_mapping(cls, value: object) -> CommittedArtifactMemberReference:
        """Decode a full reference value, without proving its persistence."""
        if type(value) is not dict:  # noqa: E721
            raise StoreValidationError("committed member reference must be an object")
        item = cast(dict[str, object], value)
        if (
            set(item)
            != {
                "receipt_id",
                "artifact_set_id",
                "member_ordinal",
                "scope",
                "artifact_type",
                "logical_id",
                "revision",
                "content_hash",
            }
            or type(item["scope"]) is not dict
        ):
            raise StoreValidationError("committed member reference has missing or unknown fields")
        scope = cast(dict[str, object], item["scope"])
        if set(scope) != {"namespace", "kind", "key"}:
            raise StoreValidationError("committed member reference scope is not closed")
        for name in ("receipt_id", "artifact_set_id"):
            _text(cast(str, item[name]), name)
        try:
            receipt_id = UUID(cast(str, item["receipt_id"]))
            artifact_set_id = UUID(cast(str, item["artifact_set_id"]))
        except ValueError as error:
            raise StoreValidationError("committed member reference UUID is invalid") from error
        return cls(
            receipt_id,
            artifact_set_id,
            cast(int, item["member_ordinal"]),
            ArtifactScope(
                cast(str, scope["namespace"]), cast(str, scope["kind"]), cast(str, scope["key"])
            ),
            cast(str, item["artifact_type"]),
            cast(str, item["logical_id"]),
            cast(int, item["revision"]),
            cast(str, item["content_hash"]),
        )


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
class PersistedCommittedArtifactSet:
    """Exact persisted set content; constructing this value grants no authority.

    The Store reader establishes the succeeded Receipt/slot/Job join. This value
    checks its internal member identities, order and hashes, not domain admission.
    """

    job: Job
    job_id: UUID
    command_slot_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID
    request_hash: str
    command_name: str
    execution_kind: CommandExecutionKind
    set_hash: str
    members: tuple[PersistedCommittedArtifactMember, ...]

    def __post_init__(self) -> None:
        if type(self.job) is not Job or self.job.profile not in (
            "test",
            "shadow",
            "production",
            "authority",
        ):  # noqa: E721
            raise StoreValidationError("committed set requires an exact Job and profile")
        for value in (self.job_id, self.command_slot_id, self.receipt_id, self.artifact_set_id):
            if type(value) is not UUID:  # noqa: E721
                raise StoreValidationError("committed set identifiers must be UUIDs")
        _sha256(self.request_hash, "committed set request_hash")
        _text(self.command_name, "committed set command_name")
        if type(self.execution_kind) is not str or self.execution_kind not in (
            "deterministic",
            "generation",
        ):  # noqa: E721
            raise StoreValidationError("committed set execution kind is unsupported")
        if (
            type(self.members) is not tuple
            or not self.members
            or any(  # noqa: E721
                type(item) is not PersistedCommittedArtifactMember for item in self.members
            )
        ):
            raise StoreValidationError("committed set requires ordered persisted members")
        for ordinal, member in enumerate(self.members):
            reference = member.reference
            if (
                member.command_slot_id != self.command_slot_id
                or reference.receipt_id != self.receipt_id
                or reference.artifact_set_id != self.artifact_set_id
                or reference.member_ordinal != ordinal
            ):
                raise StoreValidationError("committed set member ownership or order differs")
        CommandSuccess(self.command_slot_id, self.set_hash, self.artifacts)

    @property
    def references(self) -> tuple[CommittedArtifactMemberReference, ...]:
        return tuple(member.reference for member in self.members)

    @property
    def artifacts(self) -> tuple[ArtifactMember, ...]:
        return tuple(
            ArtifactMember(
                ref.artifact_type,
                ref.logical_id,
                ref.revision,
                ref.scope,
                ref.content_hash,
                member.payload_json,
            )
            for member in self.members
            for ref in (member.reference,)
        )


@dataclass(frozen=True, slots=True)
class CalibrationValidationBinding:
    """Immutable validator inputs; retry keys never change the evidence identity."""

    profile_version: str
    profile_source_sha256: str
    registry_snapshot_sha256: str
    manifest_reference: CommittedArtifactMemberReference
    results_reference: CommittedArtifactMemberReference
    attempt_idempotency_key: str
    runtime_measurement_identity: RuntimeMeasurementIdentity | None = None

    def __post_init__(self) -> None:
        calibration_profile_key(self.profile_version)
        for name in ("profile_source_sha256", "registry_snapshot_sha256"):
            value = getattr(self, name)
            _sha256(value, name)
            if value == "sha256:" + "0" * 64:
                raise StoreValidationError(f"{name} must be non-zero")
        _text(self.attempt_idempotency_key, "validator attempt idempotency key")
        if (
            self.runtime_measurement_identity is not None
            and type(self.runtime_measurement_identity) is not RuntimeMeasurementIdentity
        ):  # noqa: E721
            raise StoreValidationError("runtime measurement identity must be exact when present")
        manifest, results = self.manifest_reference, self.results_reference
        if any(type(ref) is not CommittedArtifactMemberReference for ref in (manifest, results)):  # noqa: E721
            raise StoreValidationError(
                "calibration validation requires exact measurement references"
            )
        if (
            manifest.receipt_id != results.receipt_id
            or manifest.artifact_set_id != results.artifact_set_id
            or manifest.scope != results.scope
            or manifest.scope.namespace != "autocut_calibration"
            or manifest.scope.kind != "shadow_run"
            or manifest.content_hash == results.content_hash
        ):
            raise StoreValidationError(
                "calibration measurement references must name one exact shadow set"
            )
        _sha256("sha256:" + manifest.scope.key, "measurement scope request hash")
        for ref, ordinal, artifact_type, logical_id in (
            (manifest, 0, "calibration_measurement_manifest", "measurement-manifest"),
            (results, 1, "calibration_measurement_results", "measurement-results"),
        ):
            if (ref.member_ordinal, ref.artifact_type, ref.logical_id, ref.revision) != (
                ordinal,
                artifact_type,
                logical_id,
                1,
            ) or ref.content_hash == "sha256:" + "0" * 64:
                raise StoreValidationError("calibration measurement reference identity is invalid")

    @property
    def profile_key(self) -> str:
        if self.runtime_measurement_identity is None:
            return calibration_profile_key(self.profile_version)
        return runtime_calibration_profile_key(
            self.profile_version,
            self.runtime_measurement_identity.runtime_capability_id,
        )

    @property
    def job(self) -> Job:
        return Job(f"autocut_calibration_validator:{self.profile_key}", "authority")

    @property
    def request_hash(self) -> str:
        payload: dict[str, object] = {
            "command": CALIBRATION_VALIDATOR_COMMAND,
            "profile_key": self.profile_key,
            "profile_source_sha256": self.profile_source_sha256,
            "registry_snapshot_sha256": self.registry_snapshot_sha256,
            "measurement_manifest": self.manifest_reference.to_mapping(),
            "measurement_results": self.results_reference.to_mapping(),
        }
        # Preserve v1 request identities exactly.  A v2 capability binding is
        # intentionally a different immutable validator command input.
        if self.runtime_measurement_identity is not None:
            payload["runtime_measurement_identity"] = self.runtime_measurement_identity.to_mapping()
        return canonical_payload_hash(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )

    @property
    def claim(self) -> CommandClaim:
        return CommandClaim(
            self.job,
            self.attempt_idempotency_key,
            CALIBRATION_VALIDATOR_COMMAND,
            self.request_hash,
            execution_kind="deterministic",
        )


@dataclass(frozen=True, slots=True)
class PersistedShadowCalibrationMeasurement:
    """The exact succeeded measurement pair and the shadow Job owning its blobs."""

    job: Job
    request_hash: str
    command_slot_id: UUID
    manifest: PersistedCommittedArtifactMember
    results: PersistedCommittedArtifactMember

    def __post_init__(self) -> None:
        _sha256(self.request_hash, "measurement request hash")
        if self.job != Job(self.request_hash.removeprefix("sha256:"), "shadow"):
            raise StoreValidationError("measurement Job does not bind its request hash")
        if (
            self.manifest.command_slot_id != self.command_slot_id
            or self.results.command_slot_id != self.command_slot_id
            or self.manifest.reference.receipt_id != self.results.reference.receipt_id
            or self.manifest.reference.artifact_set_id != self.results.reference.artifact_set_id
        ):
            raise StoreValidationError("measurement pair does not share one succeeded command")


@dataclass(frozen=True, slots=True)
class PersistedCalibrationRecordAnchor:
    """Exact accepted members reached through an immutable anchor, never a head."""

    record: CalibrationRecordArtifactSet
    aggregate: PersistedCommittedArtifactMember
    validation: PersistedCommittedArtifactMember

    @property
    def command_slot_id(self) -> UUID:
        return self.aggregate.command_slot_id

    @property
    def record_sha256(self) -> str:
        return self.aggregate.reference.content_hash

    @property
    def validation_receipt_sha256(self) -> str:
        return self.validation.reference.content_hash


@dataclass(frozen=True, slots=True)
class PersistedRuntimeCalibrationCapability:
    """One exact v2 runtime capability anchored to an accepted historical record.

    The enclosing v1 record remains immutable history.  This value is the
    separate v2 authority proof required before normal timed-speech admission.
    """

    measurement_identity: RuntimeMeasurementIdentity
    anchor: PersistedCalibrationRecordAnchor

    def __post_init__(self) -> None:
        if type(self.measurement_identity) is not RuntimeMeasurementIdentity:  # noqa: E721
            raise StoreValidationError("runtime capability requires an exact measurement identity")
        if type(self.anchor) is not PersistedCalibrationRecordAnchor:  # noqa: E721
            raise StoreValidationError(
                "runtime capability requires an exact accepted record anchor"
            )


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
        return {field_name: getattr(self, field_name) for field_name in self.__dataclass_fields__}


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
        if (
            len({item.receipt_id for item in members}) != 1
            or len({item.artifact_set_id for item in members}) != 1
        ):
            raise StoreValidationError(
                "VLM pack-set child members must share a Receipt/ArtifactSet"
            )
        if (
            len({item.scope for item in members}) != 1
            or len({item.revision for item in members}) != 1
        ):
            raise StoreValidationError("VLM pack-set child members must share scope/revision")

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
            raise StoreValidationError("committed VLM members must be exact member references")
        if any(
            type(item) is not BlobRef
            for item in (self.proxy_blob, self.request_payload, self.raw_response)
        ):  # noqa: E721
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
            raise StoreValidationError("semantic source manifest must be an exact member reference")
        if type(self.vlm_semantic_pack_set) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError("semantic pack set must be an exact member reference")
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
    semantic_pack: PersistedVlmSemanticPack | PersistedVlmSemanticPackV4
    response_record: CommittedArtifactMemberReference
    raw_response: BlobRef

    def __post_init__(self) -> None:
        if type(self.source_window) is not SourceWindowIdentity:  # noqa: E721
            raise StoreValidationError("VLM semantic source_window is invalid")
        if type(self.request_identity) is not VlmRequestIdentity:  # noqa: E721
            raise StoreValidationError("VLM semantic request_identity is invalid")
        if type(self.semantic_pack) not in (  # noqa: E721
            PersistedVlmSemanticPack,
            PersistedVlmSemanticPackV4,
        ):
            raise StoreValidationError("VLM semantic pack is invalid")
        expected_version = (
            ("strict-semantic-pack-v3", 3)
            if type(self.semantic_pack) is PersistedVlmSemanticPack
            else ("strict-semantic-pack-v4", 4)
        )
        child = self.semantic_pack.source_child
        child_version = (
            getattr(child, "parser_strategy_version", "strict-semantic-pack-v3"),
            getattr(child, "semantic_schema_version", 3),
        )
        if child_version != expected_version:
            raise StoreValidationError(
                "VLM semantic pack exact type disagrees with its child version"
            )
        if type(self.response_record) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError("VLM semantic response_record is invalid")
        if type(self.raw_response) is not BlobRef:  # noqa: E721
            raise StoreValidationError("VLM semantic raw_response is invalid")


def _require_inspection_binding(
    field_name: str,
    actual: object,
    expected: object,
) -> None:
    if actual != expected:
        raise StoreValidationError(f"V4 inspection {field_name} mismatch")


@dataclass(frozen=True, slots=True)
class CommittedV4InspectionInput:
    """One V4 child exposed only for inspection-scoped computation.

    This deliberately is not a subtype of ``CommittedVlmSemanticInput``.  A
    consumer that requires complete-batch semantic authority therefore cannot
    accidentally accept a selected child merely because it carries the same
    evidence fields.
    """

    source_window: SourceWindowIdentity
    request_identity: VlmRequestIdentity
    semantic_pack: PersistedVlmSemanticPackV4
    response_record: CommittedArtifactMemberReference
    response_payload_json: str
    provider_request_id: str | None
    raw_response: BlobRef

    def __post_init__(self) -> None:
        if type(self.source_window) is not SourceWindowIdentity:  # noqa: E721
            raise StoreValidationError("V4 inspection source_window is invalid")
        if type(self.request_identity) is not VlmRequestIdentity:  # noqa: E721
            raise StoreValidationError("V4 inspection request_identity is invalid")
        if type(self.semantic_pack) is not PersistedVlmSemanticPackV4:  # noqa: E721
            raise StoreValidationError("V4 inspection requires an exact V4 Semantic Pack")
        child = self.semantic_pack.source_child
        if (
            child.parser_strategy_version,
            child.semantic_schema_version,
        ) != ("strict-semantic-pack-v4", 4):
            raise StoreValidationError("V4 inspection pack disagrees with its child version")
        if type(self.response_record) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError("V4 inspection response_record is invalid")
        if type(self.raw_response) is not BlobRef:  # noqa: E721
            raise StoreValidationError("V4 inspection raw_response is invalid")
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "V4 inspection provider_request_id")
        identity = self.request_identity
        pack = self.semantic_pack.semantic_pack
        window = self.source_window
        for field_name, actual, expected in (
            (
                "request_identity.child_hash",
                identity.canonical_hash,
                child.request_identity_sha256,
            ),
            (
                "request_identity.pack_hash",
                identity.canonical_hash,
                pack.request_identity_sha256,
            ),
            (
                "request_identity.child_window",
                identity.window_manifest_sha256,
                child.window_manifest_sha256,
            ),
            (
                "request_identity.source_window",
                identity.window_manifest_sha256,
                window.window_manifest_sha256,
            ),
            (
                "request_identity.child_window_set",
                identity.window_manifest_set_sha256,
                child.window_manifest_set_sha256,
            ),
            (
                "request_identity.source_window_set",
                identity.window_manifest_set_sha256,
                window.window_manifest_set_sha256,
            ),
            ("request_identity.source_id", identity.source_id, window.source_id),
            (
                "request_identity.source_sha256",
                identity.source_sha256,
                window.source_sha256,
            ),
            (
                "request_identity.source_clock_id",
                identity.source_clock_id,
                window.source_clock_id,
            ),
            (
                "request_identity.request_payload",
                identity.request_payload_sha256,
                child.request_payload.content_hash,
            ),
        ):
            _require_inspection_binding(field_name, actual, expected)
        response = self.response_record
        response_payload = _strict_json_mapping(
            self.response_payload_json,
            "V4 inspection response payload",
        )
        expected_response_fields = frozenset(
            {
                "attempt_id",
                "provider_request_id",
                "raw_response_blob",
                "raw_response_sha256",
            }
        )
        if frozenset(response_payload) != expected_response_fields:
            raise StoreValidationError("V4 inspection response payload schema mismatch")
        response_blob = _mapping_blob_ref(
            response_payload["raw_response_blob"],
            "V4 inspection raw_response_blob",
        )
        for field_name, actual, expected in (
            ("response.receipt_id", response.receipt_id, child.receipt_id),
            (
                "response.artifact_set_id",
                response.artifact_set_id,
                child.artifact_set_id,
            ),
            ("response.member_ordinal", response.member_ordinal, 1),
            (
                "response.scope",
                response.scope,
                self.semantic_pack.reference.scope,
            ),
            ("response.artifact_type", response.artifact_type, "vlm_response_record"),
            (
                "response.logical_id",
                response.logical_id,
                f"vlm_response_{child.window_manifest_sha256[7:31]}",
            ),
            (
                "response.revision",
                response.revision,
                self.semantic_pack.reference.revision,
            ),
            (
                "response.content_hash",
                canonical_payload_hash(self.response_payload_json),
                response.content_hash,
            ),
            ("response.attempt_id", response_payload["attempt_id"], str(child.attempt_id)),
            (
                "response.provider_request_id",
                response_payload["provider_request_id"],
                self.provider_request_id,
            ),
            ("response.raw_response_blob", response_blob, self.raw_response),
            (
                "response.raw_response_sha256",
                response_payload["raw_response_sha256"],
                pack.raw_response_sha256,
            ),
            (
                "response.raw_blob_content_hash",
                self.raw_response.content_hash,
                pack.raw_response_sha256,
            ),
        ):
            _require_inspection_binding(field_name, actual, expected)


@dataclass(frozen=True, slots=True)
class CommittedV4SemanticChildInspection:
    """One exact V4 child with Source closure, without batch-completeness authority."""

    source_manifest: PersistedWholeSeriesSourceManifest
    source_grant: SourceOperationGrant
    semantic_input: CommittedV4InspectionInput
    child_idempotency_key: str
    result_scope: Literal["inspection"] = field(default="inspection", init=False)

    def __post_init__(self) -> None:
        if type(self.source_manifest) is not PersistedWholeSeriesSourceManifest:  # noqa: E721
            raise StoreValidationError("V4 inspection source_manifest is invalid")
        if type(self.source_grant) is not SourceOperationGrant:  # noqa: E721
            raise StoreValidationError("V4 inspection source_grant is invalid")
        if type(self.semantic_input) is not CommittedV4InspectionInput:  # noqa: E721
            raise StoreValidationError("V4 inspection semantic_input is invalid")
        if (
            type(self.child_idempotency_key) is not str  # noqa: E721
            or not self.child_idempotency_key
            or self.child_idempotency_key != self.child_idempotency_key.strip()
        ):
            raise StoreValidationError("V4 inspection child_idempotency_key is not canonical text")
        try:
            decoded_source = decode_source_manifest(
                self.source_manifest.payload_json,
                self.source_manifest.proxy_blobs,
            )
        except (SourceManifestDecodeError, TypeError, ValueError) as error:
            raise StoreValidationError(
                "V4 inspection SourceManifest cannot be decoded exactly"
            ) from error
        if decoded_source.census != self.source_grant:
            raise StoreValidationError("V4 inspection source_grant differs from its SourceManifest")
        decoded_windows = tuple(
            SourceWindowIdentity(
                episode_index=episode_index,
                stream_index=episode.manifest.stream_index,
                core_start_pts=episode.manifest.core_range.start_pts,
                core_end_pts=episode.manifest.core_range.end_pts,
                window_manifest_sha256=episode.manifest.canonical_hash,
                source_id=episode.manifest.source_id,
                source_sha256=episode.manifest.source_sha256,
                source_clock_id=episode.manifest.source_clock_id,
                window_manifest_set_sha256=episode.manifest_set.canonical_hash,
                proxy_blob=proxy_blob,
            )
            for episode_index, (episode, proxy_blob) in enumerate(
                zip(
                    decoded_source.episodes,
                    self.source_manifest.proxy_blobs,
                    strict=True,
                )
            )
        )
        if decoded_windows.count(self.semantic_input.source_window) != 1:
            raise StoreValidationError(
                "V4 inspection source_window is not a unique SourceManifest member"
            )
        persisted = self.semantic_input.semantic_pack
        if type(persisted) is not PersistedVlmSemanticPackV4:  # noqa: E721
            raise StoreValidationError("V4 inspection requires an exact V4 Semantic Pack")
        child = persisted.source_child
        source_job = self.source_manifest.source_job
        if (
            source_job is None
            or child.idempotency_key != self.child_idempotency_key
            or child.source_job != source_job
            or child.kernel_job_id != self.source_manifest.job_id
            or child.source_manifest_sha256 != self.source_manifest.reference.content_hash
            or child.source_provenance_sha256 != self.source_manifest.canonical_hash
            or child.episode_index != self.semantic_input.source_window.episode_index
            or child.window_manifest_sha256
            != self.semantic_input.source_window.window_manifest_sha256
        ):
            raise StoreValidationError(
                "V4 inspection child does not bind its exact Source/Window owner"
            )
        self.source_grant.require_purpose("semantic_analysis")


@dataclass(frozen=True, slots=True)
class CommittedSemanticInputs:
    """Exact committed Source/Window/Doubao inputs consumed by Stage 1-3."""

    source_manifest: PersistedWholeSeriesSourceManifest
    source_grant: SourceOperationGrant
    vlm_semantic_pack_set: CommittedArtifactMemberReference
    vlm_aggregate_policy: VlmBatchRequestPolicy
    inputs: tuple[CommittedVlmSemanticInput, ...]
    vlm_batch_strategy_version: str = VLM_BATCH_FINALIZER_STRATEGY_VERSION

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
        window_ids = tuple(item.source_window.window_manifest_sha256 for item in inputs)
        if order_keys != tuple(sorted(order_keys)) or len(window_ids) != len(set(window_ids)):
            raise StoreValidationError(
                "semantic inputs must have unique canonical Source/Window order"
            )
        expected_version = {
            VLM_BATCH_FINALIZER_STRATEGY_VERSION: ("strict-semantic-pack-v3", 3),
            VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4: ("strict-semantic-pack-v4", 4),
        }.get(self.vlm_batch_strategy_version)
        if expected_version is None:
            raise StoreValidationError("semantic VLM aggregate strategy is not registered")
        if any(
            (
                getattr(
                    item.semantic_pack.source_child,
                    "parser_strategy_version",
                    "strict-semantic-pack-v3",
                ),
                getattr(item.semantic_pack.source_child, "semantic_schema_version", 3),
            )
            != expected_version
            for item in inputs
        ):
            raise StoreValidationError(
                "semantic VLM aggregate cannot mix child parser/schema versions"
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
            raise StoreValidationError("generation.request_payload must be an exact BlobRef")
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
            raise StoreValidationError("generation max_attempts must cover the attempt ordinal")
        _sha256(self.retry_policy_hash, "generation.retry_policy_hash")
        if self.attempt_ordinal == 1:
            if self.previous_attempt_id is not None:
                raise StoreValidationError("first generation attempt cannot have a predecessor")
        elif not isinstance(self.previous_attempt_id, UUID):
            raise StoreValidationError("later generation attempt requires a predecessor UUID")
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "generation.provider_request_id")
        if self.state in ("responded", "reconciled", "committed") and self.raw_response is None:
            raise StoreValidationError(
                f"{self.state} generation requires an exact raw-response BlobRef"
            )
        if (
            self.state in ("reserved", "dispatched", "indeterminate")
            and self.raw_response is not None
        ):
            raise StoreValidationError(f"{self.state} generation cannot claim a raw response")
        if self.state == "committed":
            if self.receipt_id is None or self.artifact_set_id is None:
                raise StoreValidationError(
                    "committed generation requires receipt and artifact set bindings"
                )
        elif self.receipt_id is not None or self.artifact_set_id is not None:
            raise StoreValidationError(
                "only committed generation may bind a receipt or artifact set"
            )
        if self.state == "failed":
            _text(self.failure_code or "", "generation.failure_code")
            _text(self.failure_detail_json or "", "generation.failure_detail_json")
            try:
                detail = json.loads(self.failure_detail_json or "")
            except (TypeError, ValueError) as error:
                raise StoreValidationError(
                    "generation.failure_detail_json must contain JSON"
                ) from error
            if not isinstance(detail, dict):
                raise StoreValidationError(
                    "generation.failure_detail_json must contain a JSON object"
                )
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
            raise StoreValidationError("only failed generation may contain a failure disposition")
        if (self.dispatch_lease_token is None) != (self.dispatch_lease_expires_at is None):
            raise StoreValidationError("generation dispatch lease token and expiry must be paired")
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
        if self.not_before_at is not None and (self.not_before_at.tzinfo is None):
            raise StoreValidationError(
                "generation not_before_at must be timezone-aware when present"
            )
        if (
            type(self.retry_backoff_seconds) is not int  # noqa: E721
            or self.retry_backoff_seconds < 0
        ):
            raise StoreValidationError("generation retry_backoff_seconds must be non-negative")
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
class ProductionRenderAttempt:
    """Durable, fenced execution state for one admitted Stage 4 Recipe member."""

    attempt_id: UUID
    job_id: UUID
    command_slot_id: UUID
    request_hash: str
    recipe: CommittedArtifactMemberReference
    render_plan_sha256: str
    render_profile_sha256: str
    renderer_identity_sha256: str
    execution_limits_sha256: str
    max_output_bytes: int
    state: ProductionRenderAttemptState
    version: int
    reserved_at: datetime
    lease_expires_at: datetime | None = None
    output_blob: BlobRef | None = None
    render_facts: ProductionRenderAttemptFacts | None = None
    render_facts_sha256: str | None = None
    receipt_id: UUID | None = None
    artifact_set_id: UUID | None = None
    failure_code: str | None = None
    failure_detail_json: str | None = None
    rendered_at: datetime | None = None
    completed_at: datetime | None = None
    is_fresh_reservation: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in ("attempt_id", "job_id", "command_slot_id"):
            if type(getattr(self, field_name)) is not UUID:  # noqa: E721
                raise StoreValidationError(
                    f"production render attempt {field_name} must be an exact UUID"
                )
        if type(self.recipe) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError(
                "production render attempt recipe must be an exact committed member reference"
            )
        if (
            self.recipe.scope.namespace != "pipeline"
            or self.recipe.scope.kind != "job"
            or self.recipe.artifact_type != "recipe"
            or not self.recipe.logical_id.startswith("production_recipe@")
            or len(self.recipe.logical_id) <= len("production_recipe@")
        ):
            raise StoreValidationError(
                "production render attempt recipe is not an admitted Stage 4 Recipe identity"
            )
        _sha256(self.request_hash, "production render attempt request_hash")
        for field_name in (
            "render_plan_sha256",
            "render_profile_sha256",
            "renderer_identity_sha256",
            "execution_limits_sha256",
        ):
            _sha256(
                getattr(self, field_name),
                f"production render attempt {field_name}",
            )
        if type(self.max_output_bytes) is not int or self.max_output_bytes <= 0:  # noqa: E721
            raise StoreValidationError(
                "production render attempt max_output_bytes must be a positive integer"
            )
        if self.state not in (
            "reserved",
            "rendering",
            "rendered",
            "committed",
            "denied",
            "failed",
        ):
            raise StoreValidationError("production render attempt state is unsupported")
        if type(self.version) is not int or self.version < 0:  # noqa: E721
            raise StoreValidationError(
                "production render attempt version must be a non-negative integer"
            )
        if self.state == "reserved" and self.version != 0:
            raise StoreValidationError("reserved production render attempt must be version zero")
        minimum_version = {
            "reserved": 0,
            "rendering": 1,
            "rendered": 2,
            "committed": 3,
            "denied": 1,
            "failed": 1,
        }[self.state]
        if self.version < minimum_version:
            raise StoreValidationError(
                "production render attempt version is inconsistent with its state"
            )
        if self.state == "rendering":
            self._require_aware(self.lease_expires_at, "lease_expires_at")
        elif self.lease_expires_at is not None:
            raise StoreValidationError(
                "only a rendering production attempt may expose a lease expiry"
            )
        if (self.output_blob is None) != (self.rendered_at is None):
            raise StoreValidationError(
                "production render output BlobRef and rendered_at must be paired"
            )
        if (self.render_facts is None) != (self.render_facts_sha256 is None):
            raise StoreValidationError(
                "production render facts and their canonical hash must be paired"
            )
        if (self.output_blob is None) != (self.render_facts is None):
            raise StoreValidationError(
                "production render output and exact render facts must be paired"
            )
        if self.state in ("rendered", "committed") and self.output_blob is None:
            raise StoreValidationError(
                f"{self.state} production attempt requires an exact output BlobRef"
            )
        if self.output_blob is not None:
            if type(self.output_blob) is not BlobRef:  # noqa: E721
                raise StoreValidationError(
                    "production render attempt output_blob must be an exact BlobRef"
                )
            if self.output_blob.byte_length <= 0:
                raise StoreValidationError("production render output BlobRef must be non-empty")
            if self.output_blob.byte_length > self.max_output_bytes:
                raise StoreValidationError(
                    "production render output exceeds its immutable byte ceiling"
                )
            if self.output_blob.media_type != "video/mp4":
                raise StoreValidationError("production render output BlobRef must use video/mp4")
            self._require_aware(self.rendered_at, "rendered_at")
            from ..rendering.production_ffmpeg_renderer import (
                ProductionRenderAttemptFacts,
            )

            if type(self.render_facts) is not ProductionRenderAttemptFacts:  # noqa: E721
                raise StoreValidationError(
                    "production render attempt requires exact ProductionRenderAttemptFacts"
                )
            facts = self.render_facts
            _sha256(
                self.render_facts_sha256 or "",
                "production render attempt render_facts_sha256",
            )
            ffmpeg_json = json.dumps(
                facts.ffmpeg.to_mapping(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            renderer_identity = "sha256:" + hashlib.sha256(ffmpeg_json).hexdigest()
            if (
                facts.canonical_hash != self.render_facts_sha256
                or facts.attempt_id != self.attempt_id
                or facts.job.job_key != self.recipe.scope.key
                or facts.story_id
                != self.recipe.logical_id.removeprefix("production_recipe@")
                or facts.recipe_sha256 != self.recipe.content_hash
                or facts.plan_sha256 != self.render_plan_sha256
                or facts.profile_sha256 != self.render_profile_sha256
                or facts.execution_limits_sha256 != self.execution_limits_sha256
                or renderer_identity != self.renderer_identity_sha256
                or facts.output_sha256 != self.output_blob.content_hash
                or facts.output_byte_length != self.output_blob.byte_length
                or facts.output_media_type != self.output_blob.media_type
            ):
                raise StoreValidationError(
                    "production render facts disagree with the durable attempt authority"
                )
        elif self.rendered_at is not None:
            raise StoreValidationError(
                "production render attempt cannot bind rendered_at without output bytes"
            )
        if self.state in ("reserved", "rendering") and self.output_blob is not None:
            raise StoreValidationError(
                "an unfinished production render attempt cannot bind output bytes"
            )
        terminal = self.state in ("committed", "denied", "failed")
        if terminal:
            if type(self.receipt_id) is not UUID:  # noqa: E721
                raise StoreValidationError(
                    "terminal production render attempt requires an exact Receipt UUID"
                )
            self._require_aware(self.completed_at, "completed_at")
        elif self.receipt_id is not None or self.completed_at is not None:
            raise StoreValidationError(
                "only terminal production render attempts may bind a Receipt"
            )
        if self.state == "committed":
            if type(self.artifact_set_id) is not UUID:  # noqa: E721
                raise StoreValidationError(
                    "committed production render attempt requires an ArtifactSet UUID"
                )
        elif self.artifact_set_id is not None:
            raise StoreValidationError(
                "only committed production render attempt may bind an ArtifactSet"
            )
        if self.state in ("denied", "failed"):
            _text(
                self.failure_code or "",
                "production render attempt failure_code",
            )
            _strict_canonical_json_object(
                self.failure_detail_json or "",
                "production render attempt failure_detail_json",
            )
        elif self.failure_code is not None or self.failure_detail_json is not None:
            raise StoreValidationError(
                "only rejected production render attempts may contain failure diagnostics"
            )
        self._require_aware(self.reserved_at, "reserved_at")
        if self.rendered_at is not None and self.rendered_at < self.reserved_at:
            raise StoreValidationError("production render attempt rendered_at precedes reserved_at")
        if self.completed_at is not None and self.completed_at < self.reserved_at:
            raise StoreValidationError(
                "production render attempt completed_at precedes reserved_at"
            )

    @staticmethod
    def _require_aware(value: datetime | None, field_name: str) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise StoreValidationError(
                f"production render attempt {field_name} must be timezone-aware"
            )


@dataclass(frozen=True, slots=True)
class ProductionRenderLease:
    """Exact post-CAS lease capability returned to one production renderer."""

    attempt_id: UUID
    job_id: UUID
    command_slot_id: UUID
    token: UUID
    expires_at: datetime
    version: int

    def __post_init__(self) -> None:
        for field_name in ("attempt_id", "job_id", "command_slot_id", "token"):
            if type(getattr(self, field_name)) is not UUID:  # noqa: E721
                raise StoreValidationError(
                    f"production render lease {field_name} must be an exact UUID"
                )
        if type(self.expires_at) is not datetime or self.expires_at.tzinfo is None:  # noqa: E721
            raise StoreValidationError("production render lease expires_at must be timezone-aware")
        if type(self.version) is not int or self.version < 1:  # noqa: E721
            raise StoreValidationError("production render lease version must be a positive integer")


@dataclass(frozen=True, slots=True)
class ProductionRenderQcMeasurement:
    """One bounded scalar observation; it carries no QC policy decision."""

    name: str
    value_kind: ProductionRenderQcMeasurementKind
    value: str
    unit: ProductionRenderQcMeasurementUnit

    def __post_init__(self) -> None:
        _qc_safe_identifier(self.name, "measurement name")
        if self.value_kind not in (
            "integer",
            "decimal",
            "rational",
            "boolean",
            "text",
            "sha256",
        ):
            raise StoreValidationError("production render QC measurement kind is unsupported")
        if self.unit not in (
            "none",
            "count",
            "byte",
            "tick",
            "second",
            "frame",
            "sample",
            "packet",
            "stream",
            "channel",
            "hertz",
            "decibel",
            "lufs",
            "percent",
            "ratio",
        ):
            raise StoreValidationError("production render QC measurement unit is unsupported")
        if type(self.value) is not str or len(self.value.encode("utf-8")) > 512:  # noqa: E721
            raise StoreValidationError(
                "production render QC measurement value must be bounded text"
            )
        if self.value_kind == "integer":
            valid = re.fullmatch(r"(?:0|-?[1-9][0-9]*)", self.value) is not None
        elif self.value_kind == "decimal":
            valid = (
                re.fullmatch(
                    r"(?:0|-?[1-9][0-9]*|-?(?:0|[1-9][0-9]*)[.][0-9]*[1-9])",
                    self.value,
                )
                is not None
            )
        elif self.value_kind == "rational":
            match = re.fullmatch(r"(0|-?[1-9][0-9]*)/([1-9][0-9]*)", self.value)
            valid = match is not None and math.gcd(
                abs(int(match.group(1))), int(match.group(2))
            ) == 1
        elif self.value_kind == "boolean":
            valid = self.value in ("true", "false")
        elif self.value_kind == "sha256":
            valid = re.fullmatch(r"sha256:[0-9a-f]{64}", self.value) is not None
        else:
            valid = bool(self.value) and not any(
                segment in {"path", "locator", "uri", "url"}
                for segment in self.name.split("_")
            )
        if not valid:
            raise StoreValidationError(
                "production render QC measurement value is not canonical"
            )

    def to_mapping(self) -> dict[str, str]:
        return {
            "name": self.name,
            "unit": self.unit,
            "value": self.value,
            "value_kind": self.value_kind,
        }


@dataclass(frozen=True, slots=True)
class ProductionRenderQcCheckEvidence:
    """One complete or explicitly incomplete collector observation."""

    check_ordinal: int
    check_id: str
    collection_status: ProductionRenderQcCollectionStatus
    coverage: ProductionRenderQcCoverage
    parser_schema_version: str
    tool_identity_sha256: str
    argv_sha256: str
    measurements: tuple[ProductionRenderQcMeasurement, ...]
    evidence_blob: BlobRef
    diagnostic_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.check_ordinal) is not int or not 0 <= self.check_ordinal <= 63:  # noqa: E721
            raise StoreValidationError(
                "production render QC check ordinal must be between zero and 63"
            )
        _qc_safe_identifier(self.check_id, "check_id")
        _qc_safe_identifier(
            self.parser_schema_version,
            "parser schema version",
        )
        _sha256(self.tool_identity_sha256, "production render QC tool identity")
        _sha256(self.argv_sha256, "production render QC argv")
        valid_pairs = {
            "completed": frozenset({"full_file", "not_applicable"}),
            "incomplete": frozenset({"partial", "none"}),
            "not_run": frozenset({"none"}),
            "not_applicable": frozenset({"not_applicable"}),
        }
        if (
            self.collection_status not in valid_pairs
            or self.coverage not in valid_pairs[self.collection_status]
        ):
            raise StoreValidationError(
                "production render QC collection status and coverage disagree"
            )
        if type(self.measurements) is not tuple or len(self.measurements) > 256:  # noqa: E721
            raise StoreValidationError(
                "production render QC measurements must be a bounded tuple"
            )
        for measurement in self.measurements:
            if type(measurement) is not ProductionRenderQcMeasurement:  # noqa: E721
                raise StoreValidationError(
                    "production render QC check requires exact measurement values"
                )
        names = tuple(item.name for item in self.measurements)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise StoreValidationError(
                "production render QC measurement names must be unique and sorted"
            )
        if type(self.evidence_blob) is not BlobRef:  # noqa: E721
            raise StoreValidationError(
                "production render QC evidence_blob must be an exact BlobRef"
            )
        if (
            not 1 <= self.evidence_blob.byte_length <= 2 * 1024 * 1024
            or self.evidence_blob.media_type != "application/json"
        ):
            raise StoreValidationError(
                "production render QC evidence BlobRef is invalid or exceeds its cap"
            )
        if self.diagnostic_code is not None:
            _qc_safe_identifier(
                self.diagnostic_code,
                "diagnostic code",
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "argv_sha256": self.argv_sha256,
            "check_id": self.check_id,
            "check_ordinal": self.check_ordinal,
            "collection_status": self.collection_status,
            "coverage": self.coverage,
            "diagnostic_code": self.diagnostic_code,
            "evidence_blob": {
                "byte_length": self.evidence_blob.byte_length,
                "content_hash": self.evidence_blob.content_hash,
                "media_type": self.evidence_blob.media_type,
                "object_id": str(self.evidence_blob.object_id),
            },
            "measurements": [item.to_mapping() for item in self.measurements],
            "parser_schema_version": self.parser_schema_version,
            "tool_identity_sha256": self.tool_identity_sha256,
        }


@dataclass(frozen=True, slots=True)
class ProductionRenderQcEvidenceReport:
    """Canonical private observation journal for one exact rendered output."""

    qc_attempt_id: UUID
    render_attempt_id: UUID
    job_id: UUID
    command_slot_id: UUID
    output_blob: BlobRef
    render_facts_sha256: str
    qc_policy_sha256: str
    required_check_set_version: str
    qc_runner_identity_sha256: str
    checks: tuple[ProductionRenderQcCheckEvidence, ...]
    schema_version: Literal[
        "production-render-qc-evidence-v1"
    ] = PRODUCTION_RENDER_QC_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "qc_attempt_id",
            "render_attempt_id",
            "job_id",
            "command_slot_id",
        ):
            if type(getattr(self, field_name)) is not UUID:  # noqa: E721
                raise StoreValidationError(
                    f"production render QC report {field_name} must be an exact UUID"
                )
        if type(self.output_blob) is not BlobRef:  # noqa: E721
            raise StoreValidationError(
                "production render QC report output_blob must be an exact BlobRef"
            )
        if self.output_blob.byte_length <= 0 or self.output_blob.media_type != "video/mp4":
            raise StoreValidationError(
                "production render QC report output_blob must be a non-empty MP4"
            )
        for field_name in (
            "render_facts_sha256",
            "qc_policy_sha256",
            "qc_runner_identity_sha256",
        ):
            _sha256(getattr(self, field_name), f"production render QC report {field_name}")
        if self.required_check_set_version != PRODUCTION_RENDER_QC_CHECK_SET_VERSION:
            raise StoreValidationError(
                "production render QC report check-set version is unregistered"
            )
        if self.schema_version != PRODUCTION_RENDER_QC_EVIDENCE_SCHEMA_VERSION:
            raise StoreValidationError(
                "production render QC report schema version is unsupported"
            )
        if type(self.checks) is not tuple or not 1 <= len(self.checks) <= 64:  # noqa: E721
            raise StoreValidationError(
                "production render QC report checks must be a bounded tuple"
            )
        for check in self.checks:
            if type(check) is not ProductionRenderQcCheckEvidence:  # noqa: E721
                raise StoreValidationError(
                    "production render QC report requires exact check evidence"
                )
        identities = tuple(
            (check.check_ordinal, check.check_id) for check in self.checks
        )
        expected = tuple(enumerate(PRODUCTION_RENDER_QC_REQUIRED_CHECKS))
        if identities != expected:
            raise StoreValidationError(
                "production render QC report check set is incomplete or reordered"
            )
        if sum(check.evidence_blob.byte_length for check in self.checks) > 16 * 1024 * 1024:
            raise StoreValidationError(
                "production render QC report evidence exceeds the aggregate cap"
            )
        if len(self.canonical_json.encode("utf-8")) > 1024 * 1024:
            raise StoreValidationError(
                "production render QC report exceeds the canonical JSON cap"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "checks": [item.to_mapping() for item in self.checks],
            "command_slot_id": str(self.command_slot_id),
            "job_id": str(self.job_id),
            "output_blob": {
                "byte_length": self.output_blob.byte_length,
                "content_hash": self.output_blob.content_hash,
                "media_type": self.output_blob.media_type,
                "object_id": str(self.output_blob.object_id),
            },
            "qc_attempt_id": str(self.qc_attempt_id),
            "qc_policy_sha256": self.qc_policy_sha256,
            "qc_runner_identity_sha256": self.qc_runner_identity_sha256,
            "render_attempt_id": str(self.render_attempt_id),
            "render_facts_sha256": self.render_facts_sha256,
            "required_check_set_version": self.required_check_set_version,
            "schema_version": self.schema_version,
        }

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.to_mapping(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def canonical_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProductionRenderQcAttempt:
    """Public durable identity and fenced state for one full-file QC scan."""

    qc_attempt_id: UUID
    render_attempt_id: UUID
    job_id: UUID
    command_slot_id: UUID
    rendered_version: int
    output_blob: BlobRef
    render_facts_sha256: str
    qc_policy_sha256: str
    required_check_set_version: str
    qc_runner_identity_sha256: str
    state: ProductionRenderQcAttemptState
    version: int
    reserved_at: datetime
    lease_expires_at: datetime | None = None
    evidence_report: ProductionRenderQcEvidenceReport | None = None
    evidence_report_sha256: str | None = None
    evidence_ready_at: datetime | None = None
    is_fresh_reservation: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in (
            "qc_attempt_id",
            "render_attempt_id",
            "job_id",
            "command_slot_id",
        ):
            if type(getattr(self, field_name)) is not UUID:  # noqa: E721
                raise StoreValidationError(
                    f"production render QC attempt {field_name} must be an exact UUID"
                )
        if type(self.rendered_version) is not int or self.rendered_version < 2:  # noqa: E721
            raise StoreValidationError(
                "production render QC attempt rendered_version must be at least two"
            )
        if type(self.output_blob) is not BlobRef:  # noqa: E721
            raise StoreValidationError(
                "production render QC attempt output_blob must be an exact BlobRef"
            )
        if self.output_blob.byte_length <= 0:
            raise StoreValidationError(
                "production render QC output BlobRef must be non-empty"
            )
        if self.output_blob.media_type != "video/mp4":
            raise StoreValidationError(
                "production render QC output BlobRef must use video/mp4"
            )
        for field_name in (
            "render_facts_sha256",
            "qc_policy_sha256",
            "qc_runner_identity_sha256",
        ):
            _sha256(
                getattr(self, field_name),
                f"production render QC attempt {field_name}",
            )
        if type(self.required_check_set_version) is not str or re.fullmatch(  # noqa: E721
            r"[a-z0-9][a-z0-9._-]{0,127}",
            self.required_check_set_version,
        ) is None:
            raise StoreValidationError(
                "production render QC attempt required_check_set_version "
                "must be a safe lowercase version identifier"
            )
        if self.state not in ("reserved", "scanning", "evidence_ready"):
            raise StoreValidationError("production render QC attempt state is unsupported")
        if type(self.version) is not int or self.version < 0:  # noqa: E721
            raise StoreValidationError(
                "production render QC attempt version must be a non-negative integer"
            )
        if self.state == "reserved":
            if self.version != 0:
                raise StoreValidationError(
                    "reserved production render QC attempt must be version zero"
                )
            if self.lease_expires_at is not None:
                raise StoreValidationError(
                    "reserved production render QC attempt cannot expose a lease expiry"
                )
            self._require_no_evidence()
        elif self.state == "scanning":
            if self.version < 1:
                raise StoreValidationError(
                    "scanning production render QC attempt requires a positive version"
                )
            self._require_aware(self.lease_expires_at, "lease_expires_at")
            self._require_no_evidence()
        else:
            if self.version < 2 or self.lease_expires_at is not None:
                raise StoreValidationError(
                    "evidence-ready production render QC attempt has an invalid version or lease"
                )
            if type(self.evidence_report) is not ProductionRenderQcEvidenceReport:  # noqa: E721
                raise StoreValidationError(
                    "evidence-ready production render QC attempt requires its exact report"
                )
            if self.evidence_report_sha256 != self.evidence_report.canonical_hash:
                raise StoreValidationError(
                    "production render QC evidence hash does not bind its canonical report"
                )
            self._require_aware(self.evidence_ready_at, "evidence_ready_at")
            report = self.evidence_report
            if (
                report.qc_attempt_id != self.qc_attempt_id
                or report.render_attempt_id != self.render_attempt_id
                or report.job_id != self.job_id
                or report.command_slot_id != self.command_slot_id
                or report.output_blob != self.output_blob
                or report.render_facts_sha256 != self.render_facts_sha256
                or report.qc_policy_sha256 != self.qc_policy_sha256
                or report.required_check_set_version != self.required_check_set_version
                or report.qc_runner_identity_sha256 != self.qc_runner_identity_sha256
            ):
                raise StoreValidationError(
                    "production render QC evidence report disagrees with its attempt"
                )
        self._require_aware(self.reserved_at, "reserved_at")
        if type(self.is_fresh_reservation) is not bool:  # noqa: E721
            raise StoreValidationError(
                "production render QC attempt is_fresh_reservation must be a boolean"
            )

    @staticmethod
    def _require_aware(value: datetime | None, field_name: str) -> None:
        if (  # noqa: E721
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise StoreValidationError(
                f"production render QC attempt {field_name} must be timezone-aware"
            )

    def _require_no_evidence(self) -> None:
        if (
            self.evidence_report is not None
            or self.evidence_report_sha256 is not None
            or self.evidence_ready_at is not None
        ):
            raise StoreValidationError(
                "pre-evidence production render QC attempt cannot expose evidence"
            )


@dataclass(frozen=True, slots=True)
class ProductionRenderQcLease:
    """Exact private-token lease capability for one full-file QC scanner."""

    qc_attempt_id: UUID
    render_attempt_id: UUID
    job_id: UUID
    command_slot_id: UUID
    token: UUID
    expires_at: datetime
    version: int

    def __post_init__(self) -> None:
        for field_name in (
            "qc_attempt_id",
            "render_attempt_id",
            "job_id",
            "command_slot_id",
            "token",
        ):
            if type(getattr(self, field_name)) is not UUID:  # noqa: E721
                raise StoreValidationError(
                    f"production render QC lease {field_name} must be an exact UUID"
                )
        if (  # noqa: E721
            type(self.expires_at) is not datetime
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise StoreValidationError(
                "production render QC lease expires_at must be timezone-aware"
            )
        if type(self.version) is not int or self.version < 1:  # noqa: E721
            raise StoreValidationError(
                "production render QC lease version must be a positive integer"
            )


@dataclass(frozen=True, slots=True)
class CommandClaim:
    job: Job
    idempotency_key: str
    command_name: str
    request_hash: str
    execution_kind: CommandExecutionKind = field(kw_only=True)

    def __post_init__(self) -> None:
        _text(self.idempotency_key, "idempotency_key")
        _text(self.command_name, "command_name")
        _sha256(self.request_hash, "request_hash")
        if type(self.execution_kind) is not str or self.execution_kind not in (  # noqa: E721
            "deterministic",
            "generation",
        ):
            raise StoreValidationError("execution_kind must be deterministic or generation")


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
        return artifact_set_hash(self.artifacts)


def artifact_set_hash(artifacts: tuple[ArtifactMember, ...]) -> str:
    """Hash the actual ordered Store member representation, without committing it."""
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
        for item in artifacts
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
            raise StoreValidationError(
                "shadow measurement plan requires the exact measurement command"
            )
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
        if not members or any(
            type(member) is not ShadowMeasurementMemberPlan for member in members
        ):  # noqa: E721
            raise StoreValidationError("shadow measurement plan requires exact member plans")
        if tuple(member.member_ordinal for member in members) != tuple(range(len(members))):
            raise StoreValidationError("shadow measurement member ordinals must be contiguous")
        if len({member.corpus_member_reference_sha256 for member in members}) != len(members):
            raise StoreValidationError(
                "shadow measurement plan must not duplicate member references"
            )
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
                encoded_member["corpus_member_reference_sha256"]
                != member.corpus_member_reference_sha256
                or encoded_member["expected_anchor_reference_sha256"]
                != member.expected_anchor_reference_sha256
                or json.dumps(
                    encoded_member["native_invocation"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                != member.invocation_json
                or json.dumps(
                    encoded_member["raw_context"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                != member.context_json
            ):
                raise StoreValidationError(
                    "shadow measurement member plan drifts from canonical plan"
                )
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
class ShadowMeasurementTerminalDenialRequest:
    """A decoder-proven, no-evidence terminal denial for one invoking member.

    This intentionally does not represent native unavailability or an unknown
    invocation outcome.  Those states remain recoverable/indeterminate and
    must not terminalize the command slot.
    """

    attempt_id: UUID
    command_slot_id: UUID
    job: Job
    plan_hash: str
    member_reference_sha256: str
    expected_attempt_version: int
    expected_member_version: int
    member_lease_token: str
    failure_code: str
    failure_detail_json: str
    command_name: str = SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME

    def __post_init__(self) -> None:
        for field_name in ("attempt_id", "command_slot_id"):
            if not isinstance(getattr(self, field_name), UUID):
                raise StoreValidationError(f"shadow terminal denial {field_name} must be a UUID")
        if type(self.job) is not Job or self.job.profile != "shadow":  # noqa: E721
            raise StoreValidationError("shadow terminal denial requires an exact shadow Job")
        _sha256(self.plan_hash, "shadow terminal denial plan_hash")
        _sha256(self.member_reference_sha256, "shadow terminal denial member reference")
        for field_name in ("expected_attempt_version", "expected_member_version"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:  # noqa: E721
                raise StoreValidationError(
                    f"shadow terminal denial {field_name} must be a non-negative integer"
                )
        _text(self.member_lease_token, "shadow terminal denial member_lease_token")
        if self.command_name != SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME:
            raise StoreValidationError(
                "shadow terminal denial requires the exact measurement command"
            )
        if self.failure_code not in SHADOW_CALIBRATION_TERMINAL_DENIAL_CODES:
            raise StoreValidationError("shadow terminal denial failure_code is not allowlisted")
        _strict_canonical_json_object(
            self.failure_detail_json, "shadow terminal denial failure_detail_json"
        )


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


@dataclass(frozen=True, slots=True)
class ShadowMeasurementTerminalDenialResult:
    """The immutable denial Receipt and closed indeterminate attempt snapshot."""

    attempt: ShadowMeasurementAttempt
    outcome: CommandOutcome

    def __post_init__(self) -> None:
        if type(self.attempt) is not ShadowMeasurementAttempt:  # noqa: E721
            raise StoreValidationError("shadow terminal denial result requires an exact attempt")
        if type(self.outcome) is not CommandOutcome:  # noqa: E721
            raise StoreValidationError("shadow terminal denial result requires an exact outcome")
        if (
            self.attempt.state != "indeterminate"
            or self.outcome.state != "denied"
            or self.attempt.outcome != self.outcome
        ):
            raise StoreValidationError("shadow terminal denial result is not a closed denial")


def _strict_media_canonical_json_object(value: str, field_name: str) -> dict[str, object]:
    """Read a media-domain canonical object without crossing into Store hashing.

    The local corpus values deliberately use the media codec's ``ensure_ascii=True``
    representation.  Store plans and artifact payloads use their separate
    ``ensure_ascii=False`` hash domain, so this helper must not call
    :func:`_strict_canonical_json_object`.
    """

    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, member in pairs:
            if type(key) is not str or key in result:  # noqa: E721
                raise ValueError("duplicate or non-text JSON key")
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
        raise StoreValidationError(f"{field_name} must contain strict media JSON") from error
    if type(parsed) is not dict:  # noqa: E721
        raise StoreValidationError(f"{field_name} must contain a media JSON object")
    canonical = json.dumps(parsed, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if value != canonical:
        raise StoreValidationError(f"{field_name} must be media-canonical JSON")
    return cast(dict[str, object], parsed)


def _media_canonical_hash(value: str, field_name: str) -> tuple[dict[str, object], str]:
    parsed = _strict_media_canonical_json_object(value, field_name)
    return parsed, "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _store_canonical_value_bytes(value: object, field_name: str) -> bytes:
    """Compare plan fragments as JSON, never with Python's bool/int equality."""
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise StoreValidationError(f"{field_name} must contain finite JSON values") from error


def _blob_mapping(value: BlobRef) -> dict[str, object]:
    return {
        "object_id": str(value.object_id),
        "content_hash": value.content_hash,
        "byte_length": value.byte_length,
        "media_type": value.media_type,
    }


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementMemberPlan:
    """One immutable local case/request/source binding in a durable plan."""

    member_ordinal: int
    case_sha256: str
    request_sha256: str
    canonical_case_json: str
    canonical_request_json: str
    source_job_id: UUID
    source_blob: BlobRef
    source_blob_reference_sha256: str
    binding_sha256: str
    service_profile_sha256: str
    max_response_bytes: int

    def __post_init__(self) -> None:
        if type(self.member_ordinal) is not int or self.member_ordinal < 0:  # noqa: E721
            raise StoreValidationError("shadow-local member ordinal must be non-negative")
        _sha256(self.case_sha256, "shadow-local member case_sha256")
        _sha256(self.request_sha256, "shadow-local member request_sha256")
        case, case_hash = _media_canonical_hash(
            self.canonical_case_json, "shadow-local member canonical_case_json"
        )
        request, request_hash = _media_canonical_hash(
            self.canonical_request_json, "shadow-local member canonical_request_json"
        )
        if self.case_sha256 != case_hash or self.request_sha256 != request_hash:
            raise StoreValidationError(
                "shadow-local member media hashes do not match canonical values"
            )
        if type(self.source_job_id) is not UUID:  # noqa: E721
            raise StoreValidationError("shadow-local member source_job_id must be an exact UUID")
        if type(self.source_blob) is not BlobRef:  # noqa: E721
            raise StoreValidationError("shadow-local member source_blob must be an exact BlobRef")
        _sha256(
            self.source_blob_reference_sha256,
            "shadow-local member source_blob_reference_sha256",
        )
        _sha256(self.binding_sha256, "shadow-local member binding_sha256")
        _sha256(self.service_profile_sha256, "shadow-local member service_profile_sha256")
        if type(self.max_response_bytes) is not int or self.max_response_bytes <= 0:  # noqa: E721
            raise StoreValidationError("shadow-local member max_response_bytes must be positive")
        # Preserve the parsed values only to force complete strict parsing above.
        del case, request

    def to_plan_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.member_ordinal,
            "case_sha256": self.case_sha256,
            "request_sha256": self.request_sha256,
            "case": json.loads(self.canonical_case_json),
            "request": json.loads(self.canonical_request_json),
            "source_job_id": str(self.source_job_id),
            "source_blob": _blob_mapping(self.source_blob),
            "source_blob_reference_sha256": self.source_blob_reference_sha256,
            "binding_sha256": self.binding_sha256,
            "service_profile_sha256": self.service_profile_sha256,
            "max_response_bytes": self.max_response_bytes,
        }

    def source_binding_mapping(self) -> dict[str, object]:
        return {
            "source_job_id": str(self.source_job_id),
            "source_blob": _blob_mapping(self.source_blob),
            "source_blob_reference_sha256": self.source_blob_reference_sha256,
        }


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementPlan:
    """Closed Store-domain plan for one shadow-local recovery aggregate."""

    claim: CommandClaim
    canonical_plan_json: str
    members: tuple[ShadowLocalMeasurementMemberPlan, ...]

    def __post_init__(self) -> None:
        if type(self.claim) is not CommandClaim:  # noqa: E721
            raise StoreValidationError("shadow-local plan requires an exact CommandClaim")
        if (
            self.claim.command_name != SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME
            or self.claim.job.profile != "shadow"
            or self.claim.execution_kind != "deterministic"
        ):
            raise StoreValidationError(
                "shadow-local plan requires the exact deterministic shadow command"
            )
        payload = _strict_canonical_json_object(
            self.canonical_plan_json, "shadow-local canonical_plan_json"
        )
        if canonical_payload_hash(self.canonical_plan_json) != self.claim.request_hash:
            raise StoreValidationError("shadow-local plan does not match claim request_hash")
        if set(payload) != {
            "command",
            "corpus_members",
            "measurement_protocol",
            "shadow_local_inputs",
        }:
            raise StoreValidationError("shadow-local plan shape is invalid")
        if (
            payload.get("command") != SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME
            or payload.get("measurement_protocol") != SHADOW_LOCAL_CALIBRATION_MEASUREMENT_PROTOCOL
        ):
            raise StoreValidationError("shadow-local plan command or protocol is invalid")
        summary = self.claim.request_hash.removeprefix("sha256:")
        if (
            self.claim.job.job_key != f"shadow-local:{summary}"
            or self.claim.idempotency_key != f"shadow-local-measurement:{summary}"
        ):
            raise StoreValidationError("shadow-local plan claim identity is invalid")
        inputs = payload.get("shadow_local_inputs")
        if type(inputs) is not dict:  # noqa: E721
            raise StoreValidationError("shadow-local plan inputs must be an object")
        input_values = cast(dict[str, object], inputs)
        if set(input_values) != {
            "limits",
            "manifest",
            "max_attempt_count",
            "service_profile",
            "source_bindings",
        }:
            raise StoreValidationError("shadow-local plan inputs are not closed")
        source_bindings_value: object = input_values["source_bindings"]
        max_attempt_count: object = input_values["max_attempt_count"]
        if (
            type(input_values["service_profile"]) is not dict
            or type(input_values["manifest"]) is not dict
            or type(input_values["limits"]) is not dict
            or type(source_bindings_value) is not list
            or type(max_attempt_count) is not int
            or max_attempt_count <= 0
        ):
            raise StoreValidationError("shadow-local plan input value types are invalid")
        members = tuple(self.members)
        if not members or any(
            type(member) is not ShadowLocalMeasurementMemberPlan for member in members
        ):
            raise StoreValidationError("shadow-local plan requires exact member plans")
        if tuple(member.member_ordinal for member in members) != tuple(range(len(members))):
            raise StoreValidationError("shadow-local member ordinals must be contiguous")
        if len({member.case_sha256 for member in members}) != len(members):
            raise StoreValidationError("shadow-local plan must not duplicate case hashes")
        corpus_members = payload.get("corpus_members")
        source_bindings = cast(list[object], source_bindings_value)
        if (
            type(corpus_members) is not list
            or len(cast(list[object], corpus_members)) != len(members)
            or len(source_bindings) != len(members)
        ):
            raise StoreValidationError("shadow-local plan member coverage is invalid")
        for member, encoded, source in zip(
            members,
            cast(list[object], corpus_members),
            source_bindings,
            strict=True,
        ):
            if _store_canonical_value_bytes(
                encoded, "shadow-local encoded member"
            ) != _store_canonical_value_bytes(
                member.to_plan_mapping(), "shadow-local expected member"
            ) or _store_canonical_value_bytes(
                source, "shadow-local encoded source binding"
            ) != _store_canonical_value_bytes(
                member.source_binding_mapping(), "shadow-local expected source binding"
            ):
                raise StoreValidationError("shadow-local plan member drifts from canonical plan")
        object.__setattr__(self, "members", members)


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementMember:
    attempt_id: UUID
    case_sha256: str
    request_sha256: str
    member_ordinal: int
    canonical_case_json: str
    canonical_request_json: str
    source_job_id: UUID
    source_blob: BlobRef
    source_blob_reference_sha256: str
    binding_sha256: str
    service_profile_sha256: str
    max_response_bytes: int
    state: ShadowLocalMeasurementMemberState
    version: int
    raw_blob: BlobRef | None = None
    evidence_json: str | None = None
    busy_proof_blob: BlobRef | None = None
    busy_proof_json: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.attempt_id) is not UUID:  # noqa: E721
            raise StoreValidationError("shadow-local member attempt_id must be an exact UUID")
        plan = ShadowLocalMeasurementMemberPlan(
            self.member_ordinal,
            self.case_sha256,
            self.request_sha256,
            self.canonical_case_json,
            self.canonical_request_json,
            self.source_job_id,
            self.source_blob,
            self.source_blob_reference_sha256,
            self.binding_sha256,
            self.service_profile_sha256,
            self.max_response_bytes,
        )
        del plan
        if self.state not in (
            "pending",
            "invoking",
            "not_started",
            "staged",
            "indeterminate",
            "rejected",
        ):
            raise StoreValidationError("shadow-local member state is unsupported")
        if type(self.version) is not int or self.version < 0:  # noqa: E721
            raise StoreValidationError("shadow-local member version must be non-negative")
        if self.state == "staged":
            if (
                type(self.raw_blob) is not BlobRef
                or self.evidence_json is None
                or self.busy_proof_blob is not None
                or self.busy_proof_json is not None
            ):  # noqa: E721
                raise StoreValidationError(
                    "staged shadow-local member requires BlobRef and evidence"
                )
            _strict_media_canonical_json_object(
                self.evidence_json, "shadow-local member evidence_json"
            )
        elif self.state == "not_started":
            if (
                type(self.busy_proof_blob) is not BlobRef
                or self.busy_proof_json is None
                or self.raw_blob is not None
                or self.evidence_json is not None
            ):  # noqa: E721
                raise StoreValidationError(
                    "not-started shadow-local member requires only a BUSY proof BlobRef"
                )
            proof = _strict_media_canonical_json_object(
                self.busy_proof_json, "shadow-local member busy_proof_json"
            )
            if set(proof) != {
                "binding_sha256",
                "invocation_state",
                "reason",
                "request_sha256",
                "schema_version",
                "service_profile_sha256",
            } or (
                proof.get("schema_version") != "local-speech-window-busy-v1"
                or proof.get("invocation_state") != "not_started"
                or proof.get("reason") != "admission_busy"
                or proof.get("request_sha256") != self.request_sha256
                or proof.get("binding_sha256") != self.binding_sha256
                or proof.get("service_profile_sha256") != self.service_profile_sha256
            ):
                raise StoreValidationError(
                    "shadow-local BUSY proof does not bind the exact member request"
                )
            if self.busy_proof_blob.content_hash != "sha256:" + hashlib.sha256(
                self.busy_proof_json.encode("utf-8")
            ).hexdigest() or self.busy_proof_blob.byte_length != len(
                self.busy_proof_json.encode("utf-8")
            ):
                raise StoreValidationError(
                    "shadow-local BUSY proof BlobRef does not bind exact proof bytes"
                )
        elif (
            self.raw_blob is not None
            or self.evidence_json is not None
            or self.busy_proof_blob is not None
            or self.busy_proof_json is not None
        ):
            raise StoreValidationError(
                "only staged or not-started members may bind durable response bytes"
            )


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementAttempt:
    attempt_id: UUID
    command_slot_id: UUID
    job: Job
    plan_hash: str
    canonical_plan_json: str
    attempt_ordinal: int
    previous_attempt_id: UUID | None
    state: ShadowLocalMeasurementAttemptState
    version: int
    members: tuple[ShadowLocalMeasurementMember, ...]
    outcome: CommandOutcome
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.attempt_id) is not UUID or type(self.command_slot_id) is not UUID:  # noqa: E721
            raise StoreValidationError("shadow-local attempt IDs must be exact UUIDs")
        if type(self.job) is not Job or self.job.profile != "shadow":  # noqa: E721
            raise StoreValidationError("shadow-local attempt requires an exact shadow Job")
        _sha256(self.plan_hash, "shadow-local attempt plan_hash")
        _strict_canonical_json_object(self.canonical_plan_json, "shadow-local attempt plan_json")
        if type(self.attempt_ordinal) is not int or self.attempt_ordinal < 1:  # noqa: E721
            raise StoreValidationError("shadow-local attempt ordinal must be positive")
        if self.previous_attempt_id is not None and type(self.previous_attempt_id) is not UUID:  # noqa: E721
            raise StoreValidationError("shadow-local previous_attempt_id must be an exact UUID")
        if self.state not in (
            "prepared",
            "collecting",
            "ready",
            "indeterminate",
            "committed",
            "denied",
        ):
            raise StoreValidationError("shadow-local attempt state is unsupported")
        if type(self.version) is not int or self.version < 0:  # noqa: E721
            raise StoreValidationError("shadow-local attempt version must be non-negative")
        members = tuple(self.members)
        if not members or any(
            type(member) is not ShadowLocalMeasurementMember for member in members
        ):  # noqa: E721
            raise StoreValidationError("shadow-local attempt requires exact members")
        if tuple(member.member_ordinal for member in members) != tuple(range(len(members))):
            raise StoreValidationError("shadow-local attempt members must be ordered")
        if any(member.attempt_id != self.attempt_id for member in members):
            raise StoreValidationError("shadow-local attempt member identity drift")
        if (
            type(self.outcome) is not CommandOutcome
            or self.outcome.command_slot_id != self.command_slot_id
        ):  # noqa: E721
            raise StoreValidationError("shadow-local attempt outcome drift")
        object.__setattr__(self, "members", members)


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementMemberLease:
    member: ShadowLocalMeasurementMember
    attempt_version: int
    lease_token: str

    def __post_init__(self) -> None:
        if type(self.member) is not ShadowLocalMeasurementMember or self.member.state != "invoking":  # noqa: E721
            raise StoreValidationError("shadow-local member lease requires an invoking member")
        if type(self.attempt_version) is not int or self.attempt_version < 0:  # noqa: E721
            raise StoreValidationError("shadow-local member lease attempt_version is invalid")
        _text(self.lease_token, "shadow-local member lease_token")


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementRecoveryLease:
    attempt: ShadowLocalMeasurementAttempt
    lease_token: str

    def __post_init__(self) -> None:
        if type(self.attempt) is not ShadowLocalMeasurementAttempt:  # noqa: E721
            raise StoreValidationError("shadow-local recovery lease requires an exact attempt")
        _text(self.lease_token, "shadow-local recovery lease_token")


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementStagedResponse:
    """Exact original response bytes and independently replayed local evidence."""

    raw_bytes: bytes
    content_hash: str
    media_type: str
    evidence_json: str

    def __post_init__(self) -> None:
        if type(self.raw_bytes) is not bytes or not self.raw_bytes:  # noqa: E721
            raise StoreValidationError(
                "shadow-local staged raw_bytes must be nonempty immutable bytes"
            )
        _sha256(self.content_hash, "shadow-local staged content_hash")
        _text(self.media_type, "shadow-local staged media_type")
        _strict_media_canonical_json_object(self.evidence_json, "shadow-local staged evidence_json")
        actual = "sha256:" + hashlib.sha256(self.raw_bytes).hexdigest()
        if actual != self.content_hash:
            raise StoreValidationError("shadow-local staged raw bytes do not match content_hash")


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementNotStartedProof:
    """Bounded canonical BUSY bytes, distinct from a staged measurement response."""

    raw_bytes: bytes
    content_hash: str
    media_type: str
    proof_json: str

    def __post_init__(self) -> None:
        if type(self.raw_bytes) is not bytes or not self.raw_bytes:  # noqa: E721
            raise StoreValidationError(
                "shadow-local BUSY raw_bytes must be nonempty immutable bytes"
            )
        _sha256(self.content_hash, "shadow-local BUSY content_hash")
        _text(self.media_type, "shadow-local BUSY media_type")
        proof = _strict_media_canonical_json_object(self.proof_json, "shadow-local BUSY proof_json")
        if set(proof) != {
            "binding_sha256",
            "invocation_state",
            "reason",
            "request_sha256",
            "schema_version",
            "service_profile_sha256",
        } or (
            proof.get("schema_version") != "local-speech-window-busy-v1"
            or proof.get("invocation_state") != "not_started"
            or proof.get("reason") != "admission_busy"
        ):
            raise StoreValidationError("shadow-local BUSY proof is not a pre-dispatch refusal")
        if self.raw_bytes != self.proof_json.encode("utf-8"):
            raise StoreValidationError(
                "shadow-local BUSY raw bytes must equal canonical proof bytes"
            )
        if self.content_hash != "sha256:" + hashlib.sha256(self.raw_bytes).hexdigest():
            raise StoreValidationError("shadow-local BUSY raw bytes do not match content_hash")


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementTerminalDenialRequest:
    attempt_id: UUID
    command_slot_id: UUID
    job: Job
    plan_hash: str
    member_case_sha256: str
    expected_attempt_version: int
    expected_member_version: int
    member_lease_token: str
    failure_code: str
    failure_detail_json: str
    command_name: str = SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME

    def __post_init__(self) -> None:
        if type(self.attempt_id) is not UUID or type(self.command_slot_id) is not UUID:  # noqa: E721
            raise StoreValidationError("shadow-local terminal denial IDs must be exact UUIDs")
        if type(self.job) is not Job or self.job.profile != "shadow":  # noqa: E721
            raise StoreValidationError("shadow-local terminal denial requires an exact shadow Job")
        _sha256(self.plan_hash, "shadow-local terminal denial plan_hash")
        _sha256(self.member_case_sha256, "shadow-local terminal denial member case")
        for field_name in ("expected_attempt_version", "expected_member_version"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:  # noqa: E721
                raise StoreValidationError(
                    f"shadow-local terminal denial {field_name} must be a non-negative integer"
                )
        _text(self.member_lease_token, "shadow-local terminal denial member_lease_token")
        if self.command_name != SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME:
            raise StoreValidationError("shadow-local terminal denial command is invalid")
        if self.failure_code not in SHADOW_LOCAL_CALIBRATION_TERMINAL_DENIAL_CODES:
            raise StoreValidationError(
                "shadow-local terminal denial failure_code is not allowlisted"
            )
        _strict_canonical_json_object(
            self.failure_detail_json, "shadow-local terminal denial failure_detail_json"
        )


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementRetryAuthorization:
    """Explicit authorization for one successor of an unknown local invocation."""

    decision_reference_sha256: str
    predecessor_plan_hash: str
    predecessor_attempt_id: UUID
    predecessor_version: int
    member_case_sha256: str
    next_attempt_ordinal: int
    reason_code: Literal["NATIVE_OUTCOME_UNKNOWN", "REQUEST_NOT_STARTED"]

    def __post_init__(self) -> None:
        _sha256(self.decision_reference_sha256, "shadow-local retry decision reference")
        _sha256(self.predecessor_plan_hash, "shadow-local retry predecessor plan hash")
        if type(self.predecessor_attempt_id) is not UUID:  # noqa: E721
            raise StoreValidationError(
                "shadow-local retry predecessor_attempt_id must be an exact UUID"
            )
        if type(self.predecessor_version) is not int or self.predecessor_version < 0:  # noqa: E721
            raise StoreValidationError("shadow-local retry predecessor_version is invalid")
        _sha256(self.member_case_sha256, "shadow-local retry member case")
        if type(self.next_attempt_ordinal) is not int or self.next_attempt_ordinal < 2:  # noqa: E721
            raise StoreValidationError(
                "shadow-local retry next_attempt_ordinal must be at least two"
            )
        if self.reason_code not in ("NATIVE_OUTCOME_UNKNOWN", "REQUEST_NOT_STARTED"):
            raise StoreValidationError("shadow-local retry authorization reason is unsupported")


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementTerminalDenialResult:
    attempt: ShadowLocalMeasurementAttempt
    outcome: CommandOutcome

    def __post_init__(self) -> None:
        if type(self.attempt) is not ShadowLocalMeasurementAttempt:  # noqa: E721
            raise StoreValidationError(
                "shadow-local terminal denial result requires an exact attempt"
            )
        if type(self.outcome) is not CommandOutcome:  # noqa: E721
            raise StoreValidationError(
                "shadow-local terminal denial result requires an exact outcome"
            )
        if (
            self.attempt.state != "denied"
            or self.outcome.state != "denied"
            or self.attempt.outcome != self.outcome
        ):
            raise StoreValidationError("shadow-local terminal denial result is not a closed denial")
