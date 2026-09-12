"""Durable rich-observation generation, deliberately isolated from V3/V4 packs."""

# NOTE: This module is being expanded from a compressed prototype.  Keep the
# orchestration semantics below; provider dispatch and reconciliation are
# deliberately distinguished by the action selected before a lease is taken.
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
from ..store.errors import StoreValidationError
from ..store.models import (
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)
from ..vlm import (
    GenerationRetryPolicy,
    ProviderCompleted,
    ProviderFailed,
    ProviderFailureDisposition,
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
    ProviderRequestIdCallback,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
)
from ..vlm.observation_aliases import ObservationAliasMap
from ..vlm.observation_contract import (
    OBSERVATION_DECODER_IMPLEMENTATION_SHA256,
    ObservationContractError,
    ObservationLimits,
    ObservationReport,
    decode_observation_report,
    observation_response_schema,
)
from .generate_vlm_evidence_command import GenerationStore

OBSERVATION_GENERATION_COMMAND = "GenerateVlmObservationCommand@1"


def _hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _blob(value: BlobRef) -> dict[str, object]:
    return {
        "object_id": str(value.object_id),
        "content_hash": value.content_hash,
        "byte_length": value.byte_length,
        "media_type": value.media_type,
    }


def _object(value: str, name: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be JSON object") from error
    if type(parsed) is not dict:
        raise ValueError(f"{name} must be JSON object")
    return cast(dict[str, object], parsed)


@dataclass(frozen=True, slots=True)
class ObservationSourceBinding:
    source_receipt_id: UUID
    source_artifact_set_id: UUID
    source_command_slot_id: UUID
    source_manifest_sha256: str
    source_provenance_sha256: str
    episode_index: int

    def __post_init__(self) -> None:
        for field in (
            "source_receipt_id",
            "source_artifact_set_id",
            "source_command_slot_id",
        ):
            if not isinstance(getattr(self, field), UUID):
                raise ValueError(f"{field} must be a UUID")
        for name in ("source_manifest_sha256", "source_provenance_sha256"):
            sha256_prefixed(getattr(self, name), name)
        if type(self.episode_index) is not int or self.episode_index < 0:
            raise ValueError("episode_index must be non-negative")

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_receipt_id": str(self.source_receipt_id),
            "source_artifact_set_id": str(self.source_artifact_set_id),
            "source_command_slot_id": str(self.source_command_slot_id),
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_provenance_sha256": self.source_provenance_sha256,
            "episode_index": self.episode_index,
        }


@dataclass(frozen=True, slots=True)
class ObservationDispatchRequest:
    provider_id: str
    model_id: str
    provider_idempotency_key: str
    request_payload: bytes
    request_payload_sha256: str
    proxy_blob_ref: WindowProxyBlobRef
    proxy_content: bytes
    on_provider_request_id: ProviderRequestIdCallback | None = None

    def __post_init__(self) -> None:
        if any(
            type(x) is not str or not x.strip()
            for x in (self.provider_id, self.model_id, self.provider_idempotency_key)
        ):
            raise ValueError("observation dispatch identifiers must be text")
        if (
            type(self.request_payload) is not bytes
            or _hash(self.request_payload) != self.request_payload_sha256
        ):
            raise ValueError("observation dispatch payload hash differs")
        if (
            type(self.proxy_blob_ref) is not WindowProxyBlobRef
            or type(self.proxy_content) is not bytes
            or len(self.proxy_content) != self.proxy_blob_ref.byte_length
            or _hash(self.proxy_content) != self.proxy_blob_ref.content_hash
        ):
            raise ValueError("observation dispatch media differs from claimed Blob")


ObservationProviderResult = (
    ProviderCompleted | ProviderFailed | ProviderPending | ProviderIndeterminate
)


class ObservationProviderPort(Protocol):
    def dispatch(
        self, request: ObservationDispatchRequest
    ) -> ObservationProviderResult: ...
    def reconcile(self, query: ProviderReconcileQuery) -> ObservationProviderResult: ...


class ObservationGenerationStore(GenerationStore, Protocol):
    def read_committed_observation_artifacts(
        self, request: "ObservationGenerationRequest", outcome: CommandOutcome,
    ) -> tuple[ArtifactMember, ...]: ...

    def read_committed_generation_attempt_chain(
        self, job: Job, *, command_slot_id: UUID, receipt_id: UUID,
        artifact_set_id: UUID, expected_request_hash: str,
    ) -> tuple[GenerationAttempt, ...]: ...

    def assert_observation_source_binding(
        self,
        job: Job,
        binding: ObservationSourceBinding,
        manifest: WindowManifest,
        manifest_set: WindowManifestSet,
        proxy_blob: BlobRef,
    ) -> None: ...
    def commit_observation_generation_success(
        self,
        request: "ObservationGenerationRequest",
        attempt: GenerationAttempt,
        success: CommandSuccess,
    ) -> GenerationAttempt: ...


@dataclass(frozen=True, slots=True)
class ObservationGenerationRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    source_binding: ObservationSourceBinding
    manifest: WindowManifest
    manifest_set: WindowManifestSet
    proxy_blob: BlobRef
    prompt_template: str
    prompt_version: str
    response_schema_json: str
    request_parameters_json: str
    model_id: str
    provider_id: str
    limits: ObservationLimits
    retry_policy: GenerationRetryPolicy
    alias_map: ObservationAliasMap | None = None

    def __post_init__(self) -> None:
        if (
            type(self.job) is not Job
            or self.artifact_scope != canonical_recipe_scope(self.job)
            or type(self.artifact_revision) is not int
            or self.artifact_revision < 1
        ):
            raise ValueError("observation request has invalid Job scope or revision")
        if any(
            type(x) is not str or not x.strip()
            for x in (
                self.idempotency_key,
                self.prompt_template,
                self.prompt_version,
                self.response_schema_json,
                self.request_parameters_json,
                self.model_id,
                self.provider_id,
            )
        ):
            raise ValueError("observation request text is required")
        if (
            type(self.source_binding) is not ObservationSourceBinding
            or type(self.manifest) is not WindowManifest
            or type(self.manifest_set) is not WindowManifestSet
            or type(self.proxy_blob) is not BlobRef
            or type(self.limits) is not ObservationLimits
            or type(self.retry_policy) is not GenerationRetryPolicy
        ):
            raise ValueError("observation request requires exact immutable values")
        if (
            self.manifest not in self.manifest_set.manifests
            or self.manifest.proxy_blob_ref.to_mapping() != _blob(self.proxy_blob)
        ):
            raise ValueError("observation request proxy must bind its manifest")
        if (
            self.alias_map is not None
            and type(self.alias_map) is not ObservationAliasMap
        ):
            raise ValueError("alias_map must be exact")
        if _object(self.response_schema_json, "response_schema_json") != observation_response_schema(self.alias_map):
            raise ValueError("response_schema_json must be the exact observation response schema")
        _object(self.request_parameters_json, "request_parameters_json")

    @property
    def request_payload(self) -> bytes:
        # This durable request envelope owns the complete frozen alias map.
        # Provider adapters must project only aliases/descriptors, never this
        # private alias-to-target mapping, into their external wire body.
        return _json(
            {
                "source_binding": self.source_binding.to_mapping(),
                "manifest_sha256": self.manifest.canonical_hash,
                "manifest_set_sha256": self.manifest_set.canonical_hash,
                "proxy_blob": _blob(self.proxy_blob),
                "prompt": self.prompt_template,
                "prompt_version": self.prompt_version,
                "response_schema": _object(
                    self.response_schema_json, "response_schema_json"
                ),
                "request_parameters": _object(
                    self.request_parameters_json, "request_parameters_json"
                ),
                "model_id": self.model_id,
                "provider_id": self.provider_id,
                "observation_decoder_sha256": OBSERVATION_DECODER_IMPLEMENTATION_SHA256,
                "limits": self.limits.to_mapping(),
                "limits_sha256": self.limits.canonical_hash,
                **(
                    {
                        "alias_envelope": self.alias_map.to_mapping(),
                        "alias_envelope_sha256": self.alias_map.canonical_hash,
                    }
                    if self.alias_map
                    else {}
                ),
            }
        )

    @property
    def request_hash(self) -> str:
        return canonical_sha256(
            {
                "command": OBSERVATION_GENERATION_COMMAND,
                "scope": {
                    "namespace": self.artifact_scope.namespace,
                    "kind": self.artifact_scope.kind,
                    "key": self.artifact_scope.key,
                },
                "revision": self.artifact_revision,
                "job": {"job_key": self.job.job_key, "profile": self.job.profile},
                "request_payload_sha256": _hash(self.request_payload),
                "retry_policy_sha256": self.retry_policy.canonical_hash,
            }
        )

    def provider_idempotency_key_for(self, ordinal: int) -> str:
        if (
            type(ordinal) is not int
            or not 1 <= ordinal <= self.retry_policy.max_attempts
        ):
            raise ValueError("attempt ordinal outside frozen retry budget")
        return canonical_sha256(
            {
                "command": OBSERVATION_GENERATION_COMMAND,
                "idempotency_key": self.idempotency_key,
                "job_key": self.job.job_key,
                "request_hash": self.request_hash,
                "attempt_ordinal": ordinal,
            }
        )


@dataclass(frozen=True, slots=True)
class ObservationGenerationResult:
    outcome: CommandOutcome
    attempt: GenerationAttempt | None = None
    report: ObservationReport | None = None
    artifacts: tuple[ArtifactMember, ...] = ()


def observation_artifacts(
    request: ObservationGenerationRequest,
    attempt: GenerationAttempt,
    report: ObservationReport,
) -> tuple[ArtifactMember, ...]:
    if attempt.raw_response is None:
        raise ValueError("observation artifacts require raw response")
    # Observations are immutable command snapshots, not revisions of one
    # source-window head.  Bind artifact names to the exact operation so a
    # distinct command over the same media cannot overwrite its predecessor.
    command_identity = canonical_sha256(
        {
            "job_key": request.job.job_key,
            "idempotency_key": request.idempotency_key,
            "request_hash": request.request_hash,
        }
    )
    suffix = command_identity[7:31]
    values = (
        (
            "vlm_observation_request_record",
            f"vlm_observation_request_{suffix}",
            {
                "attempt_id": str(attempt.attempt_id),
                "request_hash": request.request_hash,
                "request_payload_blob": _blob(attempt.request_payload),
                "source_binding": request.source_binding.to_mapping(),
                "proxy_blob": _blob(request.proxy_blob),
            },
        ),
        (
            "vlm_observation_response_record",
            f"vlm_observation_response_{suffix}",
            {
                "attempt_id": str(attempt.attempt_id),
                "provider_request_id": attempt.provider_request_id,
                "raw_response_blob": _blob(attempt.raw_response),
                "raw_response_sha256": attempt.raw_response.content_hash,
            },
        ),
        (
            "vlm_observation_report",
            f"vlm_observation_report_{suffix}",
            {
                "schema_version": "vlm-observation-report-v1",
                "status": "observation_unverified",
                "report": report.to_diagnostic_mapping(),
                "raw_response_sha256": attempt.raw_response.content_hash,
            },
        ),
    )
    return tuple(
        ArtifactMember(
            kind,
            key,
            request.artifact_revision,
            request.artifact_scope,
            canonical_payload_hash(_json(value).decode()),
            _json(value).decode(),
        )
        for kind, key, value in values
    )


class GenerateObservationCommand:
    def __init__(
        self, store: ObservationGenerationStore, provider: ObservationProviderPort
    ) -> None:
        self._store, self._provider = store, provider

    def execute(
        self, request: ObservationGenerationRequest
    ) -> ObservationGenerationResult:
        try:
            self._store.assert_observation_source_binding(
                request.job,
                request.source_binding,
                request.manifest,
                request.manifest_set,
                request.proxy_blob,
            )
        except StoreValidationError as error:
            raise ValueError("observation source binding is unavailable or mismatched") from error
        outcome = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                OBSERVATION_GENERATION_COMMAND,
                request.request_hash,
                execution_kind="generation",
            )
        )
        if outcome.state in ("denied", "failed"):
            return ObservationGenerationResult(outcome)
        attempt = self._store.read_generation_attempt_for_slot(
            request.job, outcome.command_slot_id
        )
        if attempt is None:
            body = request.request_payload
            blob = self._store.put_immutable_blob(
                request.job,
                content=body,
                content_hash=_hash(body),
                media_type="application/json",
            )
            attempt = self._store.reserve_generation_attempt(
                outcome.command_slot_id,
                request.request_hash,
                provider_id=request.provider_id,
                provider_idempotency_key=request.provider_idempotency_key_for(1),
                request_payload=blob,
                retry_policy_hash=request.retry_policy.canonical_hash,
                max_attempts=request.retry_policy.max_attempts,
            )
        self._assert(request, outcome, attempt)
        if attempt.state == "committed" or outcome.state == "succeeded":
            return self._replay(request, outcome, attempt)
        if attempt.state in ("responded", "reconciled"):
            return self._commit(request, outcome, attempt)
        if attempt.state == "failed":
            return self._recover(request, outcome, attempt)
        is_new_dispatch = attempt.state == "reserved"
        if is_new_dispatch:
            leased = self._store.dispatch_generation_attempt(
                attempt.attempt_id, expected_version=attempt.version
            )
        else:
            leased = self._store.acquire_generation_reconcile_lease(
                attempt.attempt_id, expected_version=attempt.version
            )
        if leased is None:
            return ObservationGenerationResult(outcome, attempt)
        attempt = leased

        attempt_box = [leased]

        def saved(provider_request_id: str) -> None:
            current = attempt_box[0]
            attempt_box[0] = self._store.record_generation_provider_request_id(
                current.attempt_id,
                expected_version=current.version,
                provider_request_id=provider_request_id,
                dispatch_lease_token=self._lease(current),
            )

        try:
            if is_new_dispatch:
                result = self._provider.dispatch(
                    ObservationDispatchRequest(
                        request.provider_id,
                        request.model_id,
                        attempt.provider_idempotency_key,
                        request.request_payload,
                        _hash(request.request_payload),
                        request.manifest.proxy_blob_ref,
                        self._store.read_immutable_blob(
                            request.job, request.proxy_blob
                        ),
                        saved,
                    )
                )
            else:
                result = self._provider.reconcile(
                    ProviderReconcileQuery(
                        attempt.provider_id,
                        request.model_id,
                        attempt.provider_idempotency_key,
                        attempt.provider_request_id,
                    )
                )
        except Exception:
            attempt = attempt_box[0]
            attempt = self._store.mark_generation_indeterminate(
                attempt.attempt_id,
                expected_version=attempt.version,
                dispatch_lease_token=self._lease(attempt),
                provider_request_id=attempt.provider_request_id,
            )
            return ObservationGenerationResult(outcome, attempt)
        attempt = attempt_box[0]
        return self._handle(
            request, outcome, attempt, result, reconciling=not is_new_dispatch
        )

    def reconcile(
        self, request: ObservationGenerationRequest
    ) -> ObservationGenerationResult:
        return self.execute(request)

    def _handle(
        self,
        r: ObservationGenerationRequest,
        o: CommandOutcome,
        a: GenerationAttempt,
        x: ObservationProviderResult,
        *,
        reconciling: bool,
    ) -> ObservationGenerationResult:
        if isinstance(x, ProviderCompleted):
            raw = self._store.put_immutable_blob(
                r.job,
                content=x.raw_response,
                content_hash=_hash(x.raw_response),
                media_type="application/json",
            )
            method = (
                self._store.reconcile_generation_response
                if reconciling
                else self._store.record_generation_response
            )
            a = method(
                a.attempt_id,
                expected_version=a.version,
                raw_response=raw,
                dispatch_lease_token=self._lease(a),
                provider_request_id=x.provider_request_id,
            )
            return self._commit(r, o, a)
        if isinstance(x, ProviderFailed):
            a = self._store.fail_generation_attempt(
                a.attempt_id,
                expected_version=a.version,
                failure_code=x.failure_code,
                failure_detail_json=x.failure_detail_json,
                provider_request_id=x.provider_request_id,
                failure_disposition=x.disposition.value,
                dispatch_lease_token=self._lease(a),
            )
            return self._recover(r, o, a)
        a = self._store.mark_generation_indeterminate(
            a.attempt_id,
            expected_version=a.version,
            dispatch_lease_token=self._lease(a),
            provider_request_id=x.provider_request_id,
        )
        return ObservationGenerationResult(o, a)

    def _commit(
        self, r: ObservationGenerationRequest, o: CommandOutcome, a: GenerationAttempt
    ) -> ObservationGenerationResult:
        if a.raw_response is None:
            return ObservationGenerationResult(o, a)
        raw = self._store.read_immutable_blob(r.job, a.raw_response)
        try:
            report = decode_observation_report(raw, r.limits, r.alias_map)
        except ObservationContractError as error:
            a = self._store.fail_generation_attempt(
                a.attempt_id,
                expected_version=a.version,
                failure_code="OBSERVATION_RESPONSE_INVALID",
                failure_detail_json=_json(
                    {
                        "reason_code": "OBSERVATION_RESPONSE_INVALID",
                        "decoder_message": str(error),
                    }
                ).decode(),
                failure_disposition="nonretryable",
            )
            return self._recover(r, o, a)
        artifacts = observation_artifacts(r, a, report)
        success = CommandSuccess(
            command_slot_id=o.command_slot_id,
            set_hash=artifact_set_hash(artifacts),
            artifacts=artifacts,
        )
        committed = self._store.commit_observation_generation_success(r, a, success)
        return ObservationGenerationResult(
            CommandOutcome(
                command_slot_id=o.command_slot_id,
                state="succeeded",
                receipt_id=committed.receipt_id,
                artifact_set_id=committed.artifact_set_id,
                job_id=committed.job_id,
            ),
            committed,
            report,
            artifacts,
        )

    def _recover(
        self, r: ObservationGenerationRequest, o: CommandOutcome, a: GenerationAttempt
    ) -> ObservationGenerationResult:
        if (
            a.failure_disposition == ProviderFailureDisposition.RETRYABLE.value
            and a.attempt_ordinal < a.max_attempts
        ):
            a = self._store.reserve_next_generation_attempt(
                a.attempt_id,
                expected_version=a.version,
                provider_idempotency_key=r.provider_idempotency_key_for(
                    a.attempt_ordinal + 1
                ),
            )
            return ObservationGenerationResult(o, a)
        detail = _json(
            {
                "terminal_reason": a.failure_code or "OBSERVATION_GENERATION_FAILED",
                "attempts": [
                    str(x.attempt_id)
                    for x in self._store.read_generation_attempt_chain(
                        r.job, o.command_slot_id
                    )
                ],
            }
        ).decode()
        terminal = self._store.commit_generation_rejection(
            a.attempt_id,
            expected_version=a.version,
            rejection=CommandRejection(
                o.command_slot_id,
                a.failure_code or "OBSERVATION_GENERATION_FAILED",
                detail,
                "failed",
            ),
        )
        return ObservationGenerationResult(terminal, a)

    def _replay(
        self,
        r: ObservationGenerationRequest,
        o: CommandOutcome,
        a: GenerationAttempt | None,
    ) -> ObservationGenerationResult:
        if a is None or a.state != "committed" or o.state != "succeeded":
            return ObservationGenerationResult(o, a)
        # Reconstructing a plausible payload is insufficient: the reader checks
        # the exact committed receipt/set/attempt/request/raw chain.
        from ..store.observation import read_committed_observation_report

        persisted = read_committed_observation_report(self._store, r, o)
        return ObservationGenerationResult(
            o, persisted.attempt, persisted.report, persisted.artifacts
        )

    @staticmethod
    def _lease(a: GenerationAttempt) -> str:
        if a.dispatch_lease_token is None:
            raise ValueError("generation lease is required")
        return a.dispatch_lease_token

    @staticmethod
    def _assert(
        r: ObservationGenerationRequest, o: CommandOutcome, a: GenerationAttempt
    ) -> None:
        if (
            a.command_slot_id != o.command_slot_id
            or a.request_hash != r.request_hash
            or a.provider_id != r.provider_id
            or a.provider_idempotency_key
            != r.provider_idempotency_key_for(a.attempt_ordinal)
            or a.request_payload.content_hash != _hash(r.request_payload)
            or a.retry_policy_hash != r.retry_policy.canonical_hash
            or a.max_attempts != r.retry_policy.max_attempts
        ):
            raise ValueError("observation attempt differs from frozen request")


__all__ = (
    "OBSERVATION_GENERATION_COMMAND",
    "GenerateObservationCommand",
    "ObservationDispatchRequest",
    "ObservationGenerationRequest",
    "ObservationGenerationResult",
    "ObservationGenerationStore",
    "ObservationProviderPort",
    "ObservationSourceBinding",
    "observation_artifacts",
)
