"""Durable VLM generation command over the current Kernel Store.

The command owns orchestration only.  Provider adapters perform one external
invocation or reconciliation; the strict Kernel parser remains the sole
producer of semantic observations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from ..media.types import canonical_sha256, sha256_prefixed
from ..store import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    GenerationAttempt,
    Job,
)
from ..store.models import canonical_payload_hash, canonical_recipe_scope
from ..vlm import (
    ProviderCompleted,
    ProviderDispatchRequest,
    ProviderFailed,
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
    VlmObservationSet,
    VlmParsePolicy,
    VlmProviderPort,
    VlmRequestIdentity,
    VlmResponseIndeterminate,
    VlmResponseRejected,
    WindowManifest,
    WindowManifestSet,
    parse_vlm_response,
)

_COMMAND_NAME = "GenerateVlmEvidenceCommand"
VLM_PARSER_STRATEGY_VERSION = "strict-v1"


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_object(value: str, field_name: str) -> dict[str, object]:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ValueError(f"{field_name} must contain a JSON object")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must contain finite JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must contain a JSON object")
    return cast(dict[str, object], parsed)


def _blob_mapping(value: BlobRef) -> dict[str, object]:
    return {
        "object_id": str(value.object_id),
        "content_hash": value.content_hash,
        "byte_length": value.byte_length,
        "media_type": value.media_type,
    }


class GenerationStore(Protocol):
    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def put_immutable_blob(
        self,
        job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef: ...

    def read_immutable_blob(self, job: Job, reference: BlobRef) -> bytes: ...

    def reserve_generation_attempt(
        self,
        command_slot_id: UUID,
        request_hash: str,
        *,
        provider_id: str,
        provider_idempotency_key: str,
        request_payload: BlobRef,
    ) -> GenerationAttempt: ...

    def dispatch_generation_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt: ...

    def record_generation_provider_request_id(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        provider_request_id: str,
    ) -> GenerationAttempt: ...

    def record_generation_response(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        raw_response: BlobRef,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt: ...

    def mark_generation_indeterminate(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt: ...

    def reconcile_generation_response(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        raw_response: BlobRef,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt: ...

    def fail_generation_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        failure_code: str,
        failure_detail_json: str,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt: ...

    def commit_generation_success(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        success: CommandSuccess,
    ) -> GenerationAttempt: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...

    def read_generation_attempt_for_slot(
        self,
        job: Job,
        command_slot_id: UUID,
    ) -> GenerationAttempt | None: ...


@dataclass(frozen=True, slots=True)
class GenerateVlmEvidenceRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    manifest: WindowManifest
    manifest_set: WindowManifestSet
    proxy_blob: BlobRef
    prompt_template: str
    prompt_version: str
    response_schema_json: str
    request_parameters_json: str
    model_id: str
    provider_id: str
    parse_policy: VlmParsePolicy
    episode_index: int = 0
    parser_strategy_version: str = VLM_PARSER_STRATEGY_VERSION
    source_provenance_sha256: str | None = None
    source_manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.job) is not Job:  # noqa: E721
            raise ValueError("job must be a Job")
        if type(self.idempotency_key) is not str or not self.idempotency_key.strip():  # noqa: E721
            raise ValueError("idempotency_key must be non-empty")
        if self.artifact_scope != canonical_recipe_scope(self.job):
            raise ValueError("artifact_scope must be the canonical Job scope")
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:  # noqa: E721
            raise ValueError("artifact_revision must be positive")
        if type(self.manifest) is not WindowManifest:  # noqa: E721
            raise ValueError("manifest must be a WindowManifest")
        if type(self.manifest_set) is not WindowManifestSet:  # noqa: E721
            raise ValueError("manifest_set must be a WindowManifestSet")
        if self.manifest.canonical_hash not in {
            item.canonical_hash for item in self.manifest_set.manifests
        }:
            raise ValueError("manifest must belong to the exact WindowManifestSet")
        if type(self.proxy_blob) is not BlobRef:  # noqa: E721
            raise ValueError("proxy_blob must be a Store BlobRef")
        manifest_blob = self.manifest.proxy_blob_ref
        if (
            str(self.proxy_blob.object_id) != manifest_blob.object_id
            or self.proxy_blob.content_hash != manifest_blob.content_hash
            or self.proxy_blob.byte_length != manifest_blob.byte_length
            or self.proxy_blob.media_type != manifest_blob.media_type
        ):
            raise ValueError("proxy_blob must match the exact WindowManifest proxy identity")
        for value, name in (
            (self.prompt_template, "prompt_template"),
            (self.prompt_version, "prompt_version"),
            (self.model_id, "model_id"),
            (self.provider_id, "provider_id"),
            (self.parser_strategy_version, "parser_strategy_version"),
        ):
            if type(value) is not str or not value.strip():  # noqa: E721
                raise ValueError(f"{name} must be non-empty")
        if self.parser_strategy_version != VLM_PARSER_STRATEGY_VERSION:
            raise ValueError("parser_strategy_version is not registered")
        if self.source_provenance_sha256 is not None:
            sha256_prefixed(
                self.source_provenance_sha256,
                "source_provenance_sha256",
            )
        if self.source_manifest_sha256 is not None:
            sha256_prefixed(self.source_manifest_sha256, "source_manifest_sha256")
        _json_object(self.response_schema_json, "response_schema_json")
        _json_object(self.request_parameters_json, "request_parameters_json")
        if type(self.parse_policy) is not VlmParsePolicy:  # noqa: E721
            raise ValueError("parse_policy must be a VlmParsePolicy")
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise ValueError("episode_index must be non-negative")

    @property
    def request_payload(self) -> bytes:
        return _json_bytes(
            {
                "model_id": self.model_id,
                "parser_strategy_version": self.parser_strategy_version,
                "prompt": self.prompt_template,
                "prompt_version": self.prompt_version,
                "provider_id": self.provider_id,
                "proxy_blob": self.manifest.proxy_blob_ref.to_mapping(),
                "request_parameters": _json_object(
                    self.request_parameters_json,
                    "request_parameters_json",
                ),
                "response_schema": _json_object(
                    self.response_schema_json,
                    "response_schema_json",
                ),
                "window_manifest_sha256": self.manifest.canonical_hash,
                "window_manifest_set_sha256": self.manifest_set.canonical_hash,
            }
        )

    @property
    def request_identity(self) -> VlmRequestIdentity:
        response_schema = _json_bytes(
            _json_object(self.response_schema_json, "response_schema_json")
        )
        parameters = _json_bytes(
            _json_object(self.request_parameters_json, "request_parameters_json")
        )
        return VlmRequestIdentity.from_manifest(
            self.manifest,
            self.manifest_set,
            prompt_template_sha256=_sha256_bytes(self.prompt_template.encode("utf-8")),
            prompt_version=self.prompt_version,
            response_schema_sha256=_sha256_bytes(response_schema),
            model_id=self.model_id,
            provider_id=self.provider_id,
            request_parameters_sha256=_sha256_bytes(parameters),
            request_payload_sha256=_sha256_bytes(self.request_payload),
            parse_policy=self.parse_policy,
        )

    @property
    def request_hash(self) -> str:
        return canonical_sha256(
            {
                "artifact_revision": self.artifact_revision,
                "episode_index": self.episode_index,
                "artifact_scope": {
                    "namespace": self.artifact_scope.namespace,
                    "kind": self.artifact_scope.kind,
                    "key": self.artifact_scope.key,
                },
                "identity_sha256": self.request_identity.canonical_hash,
                "job": {"job_key": self.job.job_key, "profile": self.job.profile},
                "parser_strategy_version": self.parser_strategy_version,
                "proxy_blob": _blob_mapping(self.proxy_blob),
                "source_provenance_sha256": self.source_provenance_sha256,
                "source_manifest_sha256": self.source_manifest_sha256,
            }
        )

    @property
    def provider_idempotency_key(self) -> str:
        return canonical_sha256(
            {
                "command": _COMMAND_NAME,
                "idempotency_key": self.idempotency_key,
                "job_key": self.job.job_key,
                "request_hash": self.request_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class GenerateVlmEvidenceResult:
    outcome: CommandOutcome
    attempt: GenerationAttempt | None = None
    observation_set: VlmObservationSet | None = None
    artifacts: tuple[ArtifactMember, ...] = ()


class GenerateVlmEvidenceCommand:
    def __init__(self, store: GenerationStore, provider: VlmProviderPort) -> None:
        self._store = store
        self._provider = provider

    def execute(self, request: GenerateVlmEvidenceRequest) -> GenerateVlmEvidenceResult:
        outcome = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                _COMMAND_NAME,
                request.request_hash,
            )
        )
        if outcome.state in ("denied", "failed"):
            return GenerateVlmEvidenceResult(outcome)
        if outcome.state == "succeeded":
            attempt = self._store.read_generation_attempt_for_slot(
                request.job,
                outcome.command_slot_id,
            )
            return self._replay_committed(request, outcome, attempt)

        attempt = self._store.read_generation_attempt_for_slot(
            request.job,
            outcome.command_slot_id,
        )
        if attempt is None:
            payload = request.request_payload
            payload_blob = self._store.put_immutable_blob(
                request.job,
                content=payload,
                content_hash=_sha256_bytes(payload),
                media_type="application/json",
            )
            attempt = self._store.reserve_generation_attempt(
                outcome.command_slot_id,
                request.request_hash,
                provider_id=request.provider_id,
                provider_idempotency_key=request.provider_idempotency_key,
                request_payload=payload_blob,
            )
        self._assert_attempt_identity(request, outcome, attempt)

        if attempt.state == "reserved":
            proxy_content = self._store.read_immutable_blob(
                request.job,
                request.proxy_blob,
            )
            attempt = self._store.dispatch_generation_attempt(
                attempt.attempt_id,
                expected_version=attempt.version,
            )
            attempt_box = [attempt]

            def persist_provider_request_id(provider_request_id: str) -> None:
                current = attempt_box[0]
                attempt_box[0] = self._store.record_generation_provider_request_id(
                    current.attempt_id,
                    expected_version=current.version,
                    provider_request_id=provider_request_id,
                )

            try:
                provider_result = self._provider.dispatch(
                    ProviderDispatchRequest(
                        request.provider_id,
                        request.model_id,
                        request.provider_idempotency_key,
                        request.request_payload,
                        request.request_identity.request_payload_sha256,
                        request.manifest.proxy_blob_ref,
                        proxy_content,
                        persist_provider_request_id,
                    )
                )
            except Exception:
                attempt = attempt_box[0]
                attempt = self._store.mark_generation_indeterminate(
                    attempt.attempt_id,
                    expected_version=attempt.version,
                )
                return GenerateVlmEvidenceResult(outcome, attempt)
            attempt = attempt_box[0]
            return self._handle_provider_result(request, outcome, attempt, provider_result)

        if attempt.state in ("dispatched", "indeterminate"):
            try:
                provider_result = self._provider.reconcile(
                    ProviderReconcileQuery(
                        attempt.provider_id,
                        request.model_id,
                        attempt.provider_idempotency_key,
                        attempt.provider_request_id,
                    )
                )
            except Exception:
                if attempt.state == "dispatched":
                    attempt = self._store.mark_generation_indeterminate(
                        attempt.attempt_id,
                        expected_version=attempt.version,
                        provider_request_id=attempt.provider_request_id,
                    )
                return GenerateVlmEvidenceResult(outcome, attempt)
            return self._handle_provider_result(request, outcome, attempt, provider_result)

        if attempt.state in ("responded", "reconciled"):
            return self._parse_and_commit(request, outcome, attempt)
        if attempt.state == "failed":
            rejection = self._store.commit_command_rejection(
                CommandRejection(
                    outcome.command_slot_id,
                    attempt.failure_code or "GENERATION_FAILED",
                    attempt.failure_detail_json or '{"reason":"generation failed"}',
                    outcome="failed",
                )
            )
            return GenerateVlmEvidenceResult(rejection, attempt)
        if attempt.state == "committed":
            return self._replay_committed(request, outcome, attempt)
        return GenerateVlmEvidenceResult(outcome, attempt)

    def _handle_provider_result(
        self,
        request: GenerateVlmEvidenceRequest,
        outcome: CommandOutcome,
        attempt: GenerationAttempt,
        provider_result: object,
    ) -> GenerateVlmEvidenceResult:
        if isinstance(provider_result, ProviderCompleted):
            response_blob = self._store.put_immutable_blob(
                request.job,
                content=provider_result.raw_response,
                content_hash=_sha256_bytes(provider_result.raw_response),
                media_type="application/json",
            )
            if attempt.state == "dispatched":
                attempt = self._store.record_generation_response(
                    attempt.attempt_id,
                    expected_version=attempt.version,
                    raw_response=response_blob,
                    provider_request_id=provider_result.provider_request_id,
                )
            elif attempt.state == "indeterminate":
                attempt = self._store.reconcile_generation_response(
                    attempt.attempt_id,
                    expected_version=attempt.version,
                    raw_response=response_blob,
                    provider_request_id=provider_result.provider_request_id,
                )
            return self._parse_and_commit(request, outcome, attempt)

        if isinstance(provider_result, ProviderFailed):
            attempt = self._store.fail_generation_attempt(
                attempt.attempt_id,
                expected_version=attempt.version,
                failure_code=provider_result.failure_code,
                failure_detail_json=provider_result.failure_detail_json,
                provider_request_id=provider_result.provider_request_id,
            )
            rejection = self._store.commit_command_rejection(
                CommandRejection(
                    outcome.command_slot_id,
                    provider_result.failure_code,
                    provider_result.failure_detail_json,
                    outcome="failed",
                )
            )
            return GenerateVlmEvidenceResult(rejection, attempt)

        if isinstance(provider_result, (ProviderPending, ProviderIndeterminate)):
            provider_request_id = provider_result.provider_request_id
            if attempt.state == "dispatched":
                attempt = self._store.mark_generation_indeterminate(
                    attempt.attempt_id,
                    expected_version=attempt.version,
                    provider_request_id=provider_request_id,
                )
            return GenerateVlmEvidenceResult(outcome, attempt)

        if attempt.state == "dispatched":
            attempt = self._store.mark_generation_indeterminate(
                attempt.attempt_id,
                expected_version=attempt.version,
            )
        return GenerateVlmEvidenceResult(outcome, attempt)

    def _parse_and_commit(
        self,
        request: GenerateVlmEvidenceRequest,
        outcome: CommandOutcome,
        attempt: GenerationAttempt,
    ) -> GenerateVlmEvidenceResult:
        if attempt.raw_response is None:
            return GenerateVlmEvidenceResult(outcome, attempt)
        raw_response = self._store.read_immutable_blob(request.job, attempt.raw_response)
        try:
            observation_set = parse_vlm_response(
                raw_response,
                manifest=request.manifest,
                manifest_set=request.manifest_set,
                request_identity=request.request_identity,
                policy=request.parse_policy,
            )
        except (VlmResponseRejected, VlmResponseIndeterminate) as error:
            code = getattr(error, "code", "VLM_RESPONSE_DENIED")
            detail = _json_bytes({"reason_code": code}).decode("utf-8")
            failed = self._store.fail_generation_attempt(
                attempt.attempt_id,
                expected_version=attempt.version,
                failure_code=code,
                failure_detail_json=detail,
            )
            rejection = self._store.commit_command_rejection(
                CommandRejection(
                    outcome.command_slot_id,
                    code,
                    detail,
                    outcome="denied",
                )
            )
            return GenerateVlmEvidenceResult(rejection, failed)

        artifacts = _artifacts(request, attempt, observation_set)
        success = CommandSuccess(
            outcome.command_slot_id,
            _artifact_set_hash(artifacts),
            artifacts,
        )
        committed = self._store.commit_generation_success(
            attempt.attempt_id,
            expected_version=attempt.version,
            success=success,
        )
        committed_outcome = CommandOutcome(
            command_slot_id=outcome.command_slot_id,
            state="succeeded",
            receipt_id=committed.receipt_id,
            artifact_set_id=committed.artifact_set_id,
            job_id=committed.job_id,
        )
        return GenerateVlmEvidenceResult(
            committed_outcome,
            committed,
            observation_set,
            artifacts,
        )

    def _replay_committed(
        self,
        request: GenerateVlmEvidenceRequest,
        outcome: CommandOutcome,
        attempt: GenerationAttempt | None,
    ) -> GenerateVlmEvidenceResult:
        if attempt is None or attempt.state != "committed" or attempt.raw_response is None:
            return GenerateVlmEvidenceResult(outcome, attempt)
        self._assert_attempt_identity(request, outcome, attempt)
        raw_response = self._store.read_immutable_blob(request.job, attempt.raw_response)
        observation_set = parse_vlm_response(
            raw_response,
            manifest=request.manifest,
            manifest_set=request.manifest_set,
            request_identity=request.request_identity,
            policy=request.parse_policy,
        )
        return GenerateVlmEvidenceResult(
            outcome,
            attempt,
            observation_set,
            _artifacts(request, attempt, observation_set),
        )

    @staticmethod
    def _assert_attempt_identity(
        request: GenerateVlmEvidenceRequest,
        outcome: CommandOutcome,
        attempt: GenerationAttempt,
    ) -> None:
        if (
            attempt.command_slot_id != outcome.command_slot_id
            or attempt.request_hash != request.request_hash
            or attempt.provider_id != request.provider_id
            or attempt.provider_idempotency_key != request.provider_idempotency_key
            or attempt.request_payload.content_hash
            != request.request_identity.request_payload_sha256
        ):
            raise ValueError("durable generation attempt does not match the exact command request")


def _artifact(
    request: GenerateVlmEvidenceRequest,
    *,
    artifact_type: str,
    logical_id: str,
    payload: object,
) -> ArtifactMember:
    payload_json = _json_bytes(payload).decode("utf-8")
    return ArtifactMember(
        artifact_type=artifact_type,
        logical_id=logical_id,
        revision=request.artifact_revision,
        scope=request.artifact_scope,
        content_hash=canonical_payload_hash(payload_json),
        payload_json=payload_json,
    )


def _artifacts(
    request: GenerateVlmEvidenceRequest,
    attempt: GenerationAttempt,
    observation_set: VlmObservationSet,
) -> tuple[ArtifactMember, ...]:
    if attempt.raw_response is None:
        raise ValueError("generation artifacts require an exact raw-response BlobRef")
    suffix = request.manifest.canonical_hash[7:31]
    request_record = {
        "attempt_id": str(attempt.attempt_id),
        "episode_index": request.episode_index,
        "idempotency_key": request.idempotency_key,
        "provider_idempotency_key": attempt.provider_idempotency_key,
        "proxy_blob": _blob_mapping(request.proxy_blob),
        "request_identity": request.request_identity.to_mapping(),
        "request_identity_sha256": request.request_identity.canonical_hash,
        "request_hash": request.request_hash,
        "request_payload_blob": _blob_mapping(attempt.request_payload),
        "source_provenance_sha256": request.source_provenance_sha256,
        "source_manifest_sha256": request.source_manifest_sha256,
        "window_manifest_set_sha256": request.manifest_set.canonical_hash,
        "window_manifest_sha256": request.manifest.canonical_hash,
    }
    response_record = {
        "attempt_id": str(attempt.attempt_id),
        "provider_request_id": attempt.provider_request_id,
        "raw_response_blob": _blob_mapping(attempt.raw_response),
        "raw_response_sha256": observation_set.raw_response_sha256,
    }
    return (
        _artifact(
            request,
            artifact_type="vlm_request_record",
            logical_id=f"vlm_request_{suffix}",
            payload=request_record,
        ),
        _artifact(
            request,
            artifact_type="vlm_response_record",
            logical_id=f"vlm_response_{suffix}",
            payload=response_record,
        ),
        _artifact(
            request,
            artifact_type="vlm_observation_set",
            logical_id=f"evidence_{request.manifest.canonical_hash[7:39]}",
            payload=observation_set.to_mapping(),
        ),
    )


def _artifact_set_hash(artifacts: tuple[ArtifactMember, ...]) -> str:
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
        canonical_members,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


__all__ = [
    "GenerateVlmEvidenceCommand",
    "GenerateVlmEvidenceRequest",
    "GenerateVlmEvidenceResult",
    "GenerationStore",
    "VLM_PARSER_STRATEGY_VERSION",
]
