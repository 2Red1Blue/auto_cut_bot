"""Persist one untrusted shadow-bootstrap observation behind a generic Store claim.

This command is deliberately a reporting boundary.  It binds a single HTTP
response to an already-succeeded source manifest, but neither evaluates nor
publishes the response as calibration or timed-media evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from ..media.root_evidence import EvidenceContext
from ..media.shadow_bootstrap_observation import (
    ShadowBootstrapObservationError,
    ShadowBootstrapObservationRequest,
    ShadowBootstrapObservationResult,
    decode_shadow_bootstrap_observation_response,
    project_shadow_bootstrap_observation,
)
from ..media.types import TickRange
from ..source_manifest import SourceManifestDecodeError, decode_source_manifest
from ..store.models import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    MaterializationError,
    MaterializationLimits,
    PersistedWholeSeriesSourceManifest,
    VerifiedMaterializedBlob,
    WholeSeriesSourceManifestReference,
    artifact_set_hash,
    canonical_payload_hash,
)

COLLECT_SHADOW_BOOTSTRAP_OBSERVATION_COMMAND = "CollectShadowBootstrapObservationCommand@1"
SHADOW_BOOTSTRAP_RAW_RESPONSE_MEDIA_TYPE = (
    "application/vnd.autocut.shadow-bootstrap-observation.raw+json"
)
SHADOW_BOOTSTRAP_PROJECTION_MEDIA_TYPE = (
    "application/vnd.autocut.shadow-bootstrap-observation.projection+json"
)


class CollectShadowBootstrapObservationError(ValueError):
    """The closed collection request or its committed source cannot be verified."""


class ShadowBootstrapObservationDispatchUnknownError(RuntimeError):
    """The HTTP invocation may have reached the provider, so it is not terminal."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _blob_mapping(blob: BlobRef) -> dict[str, object]:
    return {
        "object_id": str(blob.object_id),
        "content_hash": blob.content_hash,
        "byte_length": blob.byte_length,
        "media_type": blob.media_type,
    }


def _manifest_mapping(
    reference: WholeSeriesSourceManifestReference,
    receipt_id: UUID,
    artifact_set_id: UUID,
    command_slot_id: UUID,
) -> dict[str, object]:
    return {
        "artifact_reference": {
            "artifact_type": reference.artifact_type,
            "content_hash": reference.content_hash,
            "logical_id": reference.logical_id,
            "revision": reference.revision,
            "scope": {
                "namespace": reference.scope.namespace,
                "kind": reference.scope.kind,
                "key": reference.scope.key,
            },
        },
        "receipt_id": str(receipt_id),
        "artifact_set_id": str(artifact_set_id),
        "command_slot_id": str(command_slot_id),
    }


def _limits_mapping(limits: MaterializationLimits) -> dict[str, int]:
    return {
        "max_source_bytes": limits.max_source_bytes,
        "timed_speech_max_request_bytes": limits.timed_speech_max_request_bytes,
        "copy_chunk_bytes": limits.copy_chunk_bytes,
        "staging_quota_bytes": limits.staging_quota_bytes,
    }


def _same_blob(left: object, right: BlobRef) -> bool:
    return (
        getattr(left, "object_id", None) == right.object_id
        and getattr(left, "content_hash", None) == right.content_hash
        and getattr(left, "byte_length", None) == right.byte_length
        and getattr(left, "media_type", None) == right.media_type
    )


def _context_full_range(clock: EvidenceContext) -> TickRange:
    """Return the committed episode clock's complete range.

    ``EvidenceContext`` exposes the origin and the derived end tick; it has no
    ``full_range`` accessor.  The shadow-bootstrap entry builds the request with
    exactly this pair, so the command must compare against the same identity.
    """
    return TickRange(clock.origin_tick, clock.end_tick)


@dataclass(frozen=True, slots=True)
class CollectShadowBootstrapObservationRequest:
    """One exact source episode and one bounded, untrusted HTTP observation."""

    job: Job
    source_job: Job
    episode_index: int
    source_blob: BlobRef
    source_manifest_reference: WholeSeriesSourceManifestReference
    source_manifest_receipt_id: UUID
    source_manifest_artifact_set_id: UUID
    source_manifest_command_slot_id: UUID
    observation: ShadowBootstrapObservationRequest
    materialization_limits: MaterializationLimits

    def __post_init__(self) -> None:
        if type(self.job) is not Job or type(self.source_job) is not Job:  # noqa: E721
            raise CollectShadowBootstrapObservationError("jobs must be exact Job values")
        if self.job.profile != "shadow" or self.source_job.profile != "shadow":
            raise CollectShadowBootstrapObservationError(
                "shadow bootstrap requires shadow source and destination jobs"
            )
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise CollectShadowBootstrapObservationError("episode_index must be a non-negative integer")
        if type(self.source_blob) is not BlobRef:  # noqa: E721
            raise CollectShadowBootstrapObservationError("source_blob must be an exact BlobRef")
        if type(self.source_manifest_reference) is not WholeSeriesSourceManifestReference:  # noqa: E721
            raise CollectShadowBootstrapObservationError("source manifest reference must be exact")
        if any(
            not isinstance(value, UUID)
            for value in (
                self.source_manifest_receipt_id,
                self.source_manifest_artifact_set_id,
                self.source_manifest_command_slot_id,
            )
        ):
            raise CollectShadowBootstrapObservationError("source manifest provenance IDs must be UUIDs")
        if type(self.observation) is not ShadowBootstrapObservationRequest:  # noqa: E721
            raise CollectShadowBootstrapObservationError("observation must be an exact bootstrap request")
        if type(self.materialization_limits) is not MaterializationLimits:  # noqa: E721
            raise CollectShadowBootstrapObservationError("materialization_limits must be exact")
        if (
            self.observation.kernel_max_source_bytes != self.materialization_limits.max_source_bytes
            or self.observation.service_max_request_bytes
            != self.materialization_limits.timed_speech_max_request_bytes
        ):
            raise CollectShadowBootstrapObservationError(
                "bootstrap source limits must equal the materialization limits"
            )
        if self.source_blob.byte_length > self.materialization_limits.effective_max_source_bytes:
            raise CollectShadowBootstrapObservationError("source blob exceeds the frozen source byte limit")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "command": COLLECT_SHADOW_BOOTSTRAP_OBSERVATION_COMMAND,
            "job": {"job_key": self.job.job_key, "profile": self.job.profile},
            "source_job": {"job_key": self.source_job.job_key, "profile": self.source_job.profile},
            "episode_index": self.episode_index,
            "source_manifest": _manifest_mapping(
                self.source_manifest_reference,
                self.source_manifest_receipt_id,
                self.source_manifest_artifact_set_id,
                self.source_manifest_command_slot_id,
            ),
            "source_blob": _blob_mapping(self.source_blob),
            "observation": self.observation.to_mapping(),
            "materialization_limits": _limits_mapping(self.materialization_limits),
        }

    @property
    def request_hash(self) -> str:
        return canonical_payload_hash(_json(self.canonical_payload()))

    @property
    def idempotency_key(self) -> str:
        return "shadow-bootstrap-observation:" + self.request_hash.removeprefix("sha256:")


class ShadowBootstrapObservationPort(Protocol):
    """HTTP adapter invoked after claim with a private verified source lease.

    The adapter reports both the raw response and its untrusted projection in
    ``ShadowBootstrapObservationResult``.  The command independently rebuilds
    that projection before it persists either immutable blob.
    """

    def observe(
        self, request: ShadowBootstrapObservationRequest, source: VerifiedMaterializedBlob
    ) -> ShadowBootstrapObservationResult: ...


class ShadowBootstrapObservationStore(Protocol):
    def read_whole_series_source_manifest(
        self, job: Job, artifact_set_id: UUID
    ) -> PersistedWholeSeriesSourceManifest: ...

    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def materialize_immutable_blob(
        self, job: Job, reference: BlobRef, limits: MaterializationLimits
    ) -> VerifiedMaterializedBlob: ...

    def put_immutable_blob(
        self, job: Job, *, content: bytes, content_hash: str, media_type: str
    ) -> BlobRef: ...

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...


class CollectShadowBootstrapObservationCommand:
    """Claim, dispatch once, and atomically persist raw and untrusted projection blobs."""

    def __init__(self, store: ShadowBootstrapObservationStore, port: ShadowBootstrapObservationPort) -> None:
        self._store = store
        self._port = port

    def execute(self, request: CollectShadowBootstrapObservationRequest) -> CommandOutcome:
        if type(request) is not CollectShadowBootstrapObservationRequest:  # noqa: E721
            raise CollectShadowBootstrapObservationError("request must be exact")
        self._resolve_source(request)
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                COLLECT_SHADOW_BOOTSTRAP_OBSERVATION_COMMAND,
                request.request_hash,
                execution_kind="deterministic",
            )
        )
        if not claimed.is_fresh_claim:
            return claimed

        source: VerifiedMaterializedBlob | None = None
        failure: tuple[str, str, Literal["denied", "failed"]] | None = None
        success: CommandSuccess | None = None
        try:
            source = self._store.materialize_immutable_blob(
                request.job, request.source_blob, request.materialization_limits
            )
            if not _same_blob(source.reference, request.source_blob):
                raise CollectShadowBootstrapObservationError("materialized source reference differs")
            reported = self._port.observe(request.observation, source)
            if type(reported) is not ShadowBootstrapObservationResult:  # noqa: E721
                raise CollectShadowBootstrapObservationError(
                    "bootstrap port must return an exact observation result"
                )
            raw = reported.decoded.raw_response
            decoded = decode_shadow_bootstrap_observation_response(raw, request.observation)
            projection = project_shadow_bootstrap_observation(decoded)
            if projection != reported:
                raise CollectShadowBootstrapObservationError(
                    "bootstrap port projection differs from its raw response"
                )
            artifacts = self._persist_artifacts(request, raw, projection.to_mapping())
            success = CommandSuccess(claimed.command_slot_id, artifact_set_hash(artifacts), artifacts)
        except MaterializationError as error:
            failure = (error.code, error.detail, error.outcome)
        except ShadowBootstrapObservationDispatchUnknownError:
            # The remote model may have completed after the caller lost its
            # response. Do not fabricate a terminal Receipt; a dedicated
            # recovery policy must decide whether a successor attempt is safe.
            return claimed
        except (
            CollectShadowBootstrapObservationError,
            ShadowBootstrapObservationError,
            SourceManifestDecodeError,
            ValueError,
        ) as error:
            failure = ("SHADOW_BOOTSTRAP_OBSERVATION_INVALID", str(error), "denied")
        except Exception:
            failure = (
                "SHADOW_BOOTSTRAP_OBSERVATION_INFRASTRUCTURE_FAILED",
                "shadow bootstrap observation infrastructure failed",
                "failed",
            )
        finally:
            if source is not None:
                source.close()

        if failure is not None:
            code, detail, outcome = failure
            return self._reject(claimed, code, detail, outcome=outcome)
        assert success is not None
        # A commit error is ambiguous.  Never overwrite it with a rejection.
        return self._store.commit_command_success(success)

    def _resolve_source(self, request: CollectShadowBootstrapObservationRequest) -> None:
        persisted = self._store.read_whole_series_source_manifest(
            request.source_job, request.source_manifest_artifact_set_id
        )
        if (
            persisted.reference != request.source_manifest_reference
            or persisted.receipt_id != request.source_manifest_receipt_id
            or persisted.artifact_set_id != request.source_manifest_artifact_set_id
            or persisted.command_slot_id != request.source_manifest_command_slot_id
            or persisted.source_job != request.source_job
        ):
            raise CollectShadowBootstrapObservationError("committed source manifest provenance differs")
        decoded = decode_source_manifest(persisted.payload_json, persisted.proxy_blobs)
        try:
            episode = decoded.episodes[request.episode_index]
        except IndexError as error:
            raise CollectShadowBootstrapObservationError("source episode index is unavailable") from error
        if not _same_blob(episode.proxy_blob, request.source_blob):
            raise CollectShadowBootstrapObservationError("source episode BlobRef differs")
        source = episode.media_probe.source
        clock = episode.media_probe.audio_sample_boundaries.context
        observation_source = request.observation.source
        if (
            observation_source.source_id != source.source_id
            or observation_source.source_sha256 != source.content_sha256
            or observation_source.clock_id != clock.clock_id
            or observation_source.time_base != clock.time_base
            or observation_source.source_range != _context_full_range(clock)
            or request.source_blob.media_type != "video/mp4"
        ):
            raise CollectShadowBootstrapObservationError(
                "bootstrap observation source or audio clock differs from the committed episode"
            )

    def _persist_artifacts(
        self,
        request: CollectShadowBootstrapObservationRequest,
        raw: bytes,
        projection: dict[str, object],
    ) -> tuple[ArtifactMember, ArtifactMember]:
        raw_blob = self._put_blob(request.job, raw, SHADOW_BOOTSTRAP_RAW_RESPONSE_MEDIA_TYPE)
        projection_bytes = _json(projection).encode("utf-8")
        projection_blob = self._put_blob(
            request.job, projection_bytes, SHADOW_BOOTSTRAP_PROJECTION_MEDIA_TYPE
        )
        source_manifest = _manifest_mapping(
            request.source_manifest_reference,
            request.source_manifest_receipt_id,
            request.source_manifest_artifact_set_id,
            request.source_manifest_command_slot_id,
        )
        common = {
            "command": COLLECT_SHADOW_BOOTSTRAP_OBSERVATION_COMMAND,
            "request_hash": request.request_hash,
            "source_job": {"job_key": request.source_job.job_key, "profile": request.source_job.profile},
            "episode_index": request.episode_index,
            "source_manifest": source_manifest,
            "source_blob": _blob_mapping(request.source_blob),
            "trust_status": "untrusted",
            "authority_eligible": False,
            "independent_anchor_count": 0,
        }
        raw_payload = {
            **common,
            "schema_version": "shadow-bootstrap-observation-raw-artifact-v1",
            "raw_response_blob": _blob_mapping(raw_blob),
            "raw_response_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "raw_response_byte_length": len(raw),
        }
        projection_payload = {
            **common,
            "schema_version": "shadow-bootstrap-observation-projection-artifact-v1",
            "raw_response_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "projection_blob": _blob_mapping(projection_blob),
            "projection": projection,
        }
        suffix = request.request_hash.removeprefix("sha256:")
        return (
            self._artifact(
                request,
                "shadow_bootstrap_observation_raw_response",
                f"shadow_bootstrap_raw_episode_{request.episode_index:04d}_{suffix}",
                raw_payload,
            ),
            self._artifact(
                request,
                "shadow_bootstrap_observation_projection",
                f"shadow_bootstrap_projection_episode_{request.episode_index:04d}_{suffix}",
                projection_payload,
            ),
        )

    def _put_blob(self, job: Job, content: bytes, media_type: str) -> BlobRef:
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        blob = self._store.put_immutable_blob(
            job, content=content, content_hash=digest, media_type=media_type
        )
        if (
            type(blob) is not BlobRef
            or blob.content_hash != digest
            or blob.byte_length != len(content)
            or blob.media_type != media_type
        ):
            raise CollectShadowBootstrapObservationError("stored observation blob reference differs")
        return blob

    @staticmethod
    def _artifact(
        request: CollectShadowBootstrapObservationRequest,
        artifact_type: str,
        logical_id: str,
        payload: dict[str, object],
    ) -> ArtifactMember:
        payload_json = _json(payload)
        return ArtifactMember(
            artifact_type,
            logical_id,
            1,
            ArtifactScope(
                "autocut_observation",
                "shadow_bootstrap",
                request.request_hash.removeprefix("sha256:"),
            ),
            canonical_payload_hash(payload_json),
            payload_json,
        )

    def _reject(
        self,
        claimed: CommandOutcome,
        code: str,
        detail: str,
        *,
        outcome: Literal["denied", "failed"],
    ) -> CommandOutcome:
        return self._store.commit_command_rejection(
            CommandRejection(
                claimed.command_slot_id,
                code,
                _json({"code": code, "detail": detail}),
                outcome=outcome,
            )
        )


__all__ = [
    "COLLECT_SHADOW_BOOTSTRAP_OBSERVATION_COMMAND",
    "SHADOW_BOOTSTRAP_PROJECTION_MEDIA_TYPE",
    "SHADOW_BOOTSTRAP_RAW_RESPONSE_MEDIA_TYPE",
    "CollectShadowBootstrapObservationCommand",
    "CollectShadowBootstrapObservationError",
    "CollectShadowBootstrapObservationRequest",
    "ShadowBootstrapObservationDispatchUnknownError",
    "ShadowBootstrapObservationPort",
    "ShadowBootstrapObservationStore",
]
