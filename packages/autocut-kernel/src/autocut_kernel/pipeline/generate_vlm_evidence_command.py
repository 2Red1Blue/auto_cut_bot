"""Durable VLM generation command over the current Kernel Store.

The command owns orchestration only.  Provider adapters perform one external
invocation or reconciliation; the strict Kernel parser remains the sole
producer of a Semantic Pack.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol, cast
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
    GenerationRetryPolicy,
    ProviderCompleted,
    ProviderDispatchRequest,
    ProviderFailed,
    ProviderFailureDisposition,
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
    VlmParsePolicy,
    VlmProviderPort,
    VlmRequestIdentity,
    VlmResponseIndeterminate,
    VlmResponseRejected,
    WindowManifest,
    WindowManifestSet,
)
from ..vlm.semantic_contracts import (
    REGISTERED_VLM_PARSERS,
    VLM_PARSER_V4,
    SemanticPackValue,
    parse_registered_vlm_response,
    require_parser_contract,
)

_COMMAND_NAME = "GenerateVlmEvidenceCommand"
VLM_PARSER_STRATEGY_VERSION = "strict-semantic-pack-v3"


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
        retry_policy_hash: str,
        max_attempts: int,
    ) -> GenerationAttempt: ...

    def reserve_next_generation_attempt(
        self,
        previous_attempt_id: UUID,
        *,
        expected_version: int,
        provider_idempotency_key: str,
    ) -> GenerationAttempt: ...

    def dispatch_generation_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt | None: ...

    def acquire_generation_reconcile_lease(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
    ) -> GenerationAttempt | None: ...

    def record_generation_provider_request_id(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        provider_request_id: str,
        dispatch_lease_token: str,
    ) -> GenerationAttempt: ...

    def record_generation_response(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        raw_response: BlobRef,
        dispatch_lease_token: str,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt: ...

    def mark_generation_indeterminate(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        dispatch_lease_token: str,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt: ...

    def reconcile_generation_response(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        raw_response: BlobRef,
        dispatch_lease_token: str,
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
        failure_disposition: str = "nonretryable",
        dispatch_lease_token: str | None = None,
    ) -> GenerationAttempt: ...

    def commit_generation_success(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        success: CommandSuccess,
    ) -> GenerationAttempt: ...

    def commit_generation_rejection(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        rejection: CommandRejection,
    ) -> CommandOutcome: ...

    def read_generation_attempt_for_slot(
        self,
        job: Job,
        command_slot_id: UUID,
    ) -> GenerationAttempt | None: ...

    def read_generation_attempt_chain(
        self,
        job: Job,
        command_slot_id: UUID,
    ) -> tuple[GenerationAttempt, ...]: ...


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
    retry_policy: GenerationRetryPolicy
    episode_index: int = 0
    parser_strategy_version: str = VLM_PARSER_STRATEGY_VERSION
    source_provenance_sha256: str | None = None
    source_manifest_sha256: str | None = None
    parser_contract_sha256: str | None = None

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
        if self.parser_strategy_version not in REGISTERED_VLM_PARSERS:
            raise ValueError("parser_strategy_version is not registered")
        require_parser_contract(self.parser_strategy_version, self.parser_contract_sha256)
        if self.source_provenance_sha256 is not None:
            sha256_prefixed(
                self.source_provenance_sha256,
                "source_provenance_sha256",
            )
        if self.source_manifest_sha256 is not None:
            sha256_prefixed(self.source_manifest_sha256, "source_manifest_sha256")
        response_schema = _json_object(self.response_schema_json, "response_schema_json")
        properties = response_schema.get("properties")
        version_schema = (
            cast(dict[str, object], properties).get("schema_version")
            if isinstance(properties, dict) else None
        )
        wire_version = (
            cast(dict[str, object], version_schema).get("const")
            if isinstance(version_schema, dict) else None
        )
        if self.parser_strategy_version == VLM_PARSER_V4 or wire_version == 4:
            if self.parser_strategy_version != VLM_PARSER_V4 or type(wire_version) is not int or wire_version != 4:  # noqa: E721
                raise ValueError("V4 parser requires the explicit V4 response wire schema")
        _json_object(self.request_parameters_json, "request_parameters_json")
        if type(self.parse_policy) is not VlmParsePolicy:  # noqa: E721
            raise ValueError("parse_policy must be a VlmParsePolicy")
        if type(self.retry_policy) is not GenerationRetryPolicy:  # noqa: E721
            raise ValueError("retry_policy must be a GenerationRetryPolicy")
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise ValueError("episode_index must be non-negative")

    @property
    def request_payload(self) -> bytes:
        return _json_bytes(
            {
                **({"parser_contract_sha256": self.parser_contract_sha256}
                   if self.parser_strategy_version == VLM_PARSER_V4 else {}),
                "model_id": self.model_id,
                "parse_policy": self.parse_policy.to_mapping(),
                "parser_strategy_version": self.parser_strategy_version,
                "prompt": self.prompt_template,
                "prompt_version": self.prompt_version,
                "provider_id": self.provider_id,
                "proxy_blob": self.manifest.proxy_blob_ref.to_mapping(),
                "request_parameters": _json_object(
                    self.request_parameters_json,
                    "request_parameters_json",
                ),
                "retry_policy": self.retry_policy.to_mapping(),
                "retry_policy_sha256": self.retry_policy.canonical_hash,
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
                "retry_policy_sha256": self.retry_policy.canonical_hash,
                "proxy_blob": _blob_mapping(self.proxy_blob),
                "source_provenance_sha256": self.source_provenance_sha256,
                "source_manifest_sha256": self.source_manifest_sha256,
            }
        )

    @property
    def provider_request_base(self) -> str:
        return canonical_sha256(
            {
                "command": _COMMAND_NAME,
                "idempotency_key": self.idempotency_key,
                "job_key": self.job.job_key,
                "request_hash": self.request_hash,
                "retry_policy_sha256": self.retry_policy.canonical_hash,
            }
        )

    def provider_idempotency_key_for(self, attempt_ordinal: int) -> str:
        if type(attempt_ordinal) is not int or not (  # noqa: E721
            1 <= attempt_ordinal <= self.retry_policy.max_attempts
        ):
            raise ValueError("attempt_ordinal is outside the frozen retry budget")
        return canonical_sha256(
            {
                "attempt_ordinal": attempt_ordinal,
                "provider_request_base": self.provider_request_base,
            }
        )

    @property
    def provider_idempotency_key(self) -> str:
        """Compatibility projection for the first durable invocation identity."""

        return self.provider_idempotency_key_for(1)


@dataclass(frozen=True, slots=True)
class GenerateVlmEvidenceResult:
    outcome: CommandOutcome
    attempt: GenerationAttempt | None = None
    semantic_pack: SemanticPackValue | None = None
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
                execution_kind="generation",
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
                retry_policy_hash=request.retry_policy.canonical_hash,
                max_attempts=request.retry_policy.max_attempts,
            )
        self._assert_attempt_identity(request, outcome, attempt)

        if attempt.state == "reserved":
            dispatched = self._store.dispatch_generation_attempt(
                attempt.attempt_id,
                expected_version=attempt.version,
            )
            if dispatched is None:
                return GenerateVlmEvidenceResult(outcome, attempt)
            attempt = dispatched
            proxy_content = self._store.read_immutable_blob(
                request.job,
                request.proxy_blob,
            )
            attempt_box = [attempt]

            def persist_provider_request_id(provider_request_id: str) -> None:
                current = attempt_box[0]
                attempt_box[0] = self._store.record_generation_provider_request_id(
                        current.attempt_id,
                        expected_version=current.version,
                        provider_request_id=provider_request_id,
                        dispatch_lease_token=self._lease_token(current),
                )

            try:
                provider_result = self._provider.dispatch(
                    ProviderDispatchRequest(
                        request.provider_id,
                        request.model_id,
                        attempt.provider_idempotency_key,
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
                    dispatch_lease_token=self._lease_token(attempt),
                )
                return GenerateVlmEvidenceResult(outcome, attempt)
            attempt = attempt_box[0]
            return self._handle_provider_result(request, outcome, attempt, provider_result)

        if attempt.state in ("dispatched", "indeterminate"):
            acquired = self._store.acquire_generation_reconcile_lease(
                attempt.attempt_id,
                expected_version=attempt.version,
            )
            if acquired is None:
                return GenerateVlmEvidenceResult(outcome, attempt)
            attempt = acquired
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
                attempt = self._store.mark_generation_indeterminate(
                    attempt.attempt_id,
                    expected_version=attempt.version,
                    dispatch_lease_token=self._lease_token(attempt),
                    provider_request_id=attempt.provider_request_id,
                )
                return GenerateVlmEvidenceResult(outcome, attempt)
            return self._handle_provider_result(request, outcome, attempt, provider_result)

        if attempt.state in ("responded", "reconciled"):
            return self._parse_and_commit(request, outcome, attempt)
        if attempt.state == "failed":
            return self._recover_failed_attempt(request, outcome, attempt)
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
                    dispatch_lease_token=self._lease_token(attempt),
                    provider_request_id=provider_result.provider_request_id,
                )
            elif attempt.state == "indeterminate":
                attempt = self._store.reconcile_generation_response(
                    attempt.attempt_id,
                    expected_version=attempt.version,
                    raw_response=response_blob,
                    dispatch_lease_token=self._lease_token(attempt),
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
                failure_disposition=provider_result.disposition.value,
                dispatch_lease_token=self._lease_token(attempt),
            )
            return self._recover_failed_attempt(request, outcome, attempt)

        if isinstance(provider_result, (ProviderPending, ProviderIndeterminate)):
            provider_request_id = provider_result.provider_request_id
            if attempt.state in ("dispatched", "indeterminate"):
                attempt = self._store.mark_generation_indeterminate(
                    attempt.attempt_id,
                    expected_version=attempt.version,
                    dispatch_lease_token=self._lease_token(attempt),
                    provider_request_id=provider_request_id,
                )
            return GenerateVlmEvidenceResult(outcome, attempt)

        if attempt.state in ("dispatched", "indeterminate"):
            attempt = self._store.mark_generation_indeterminate(
                attempt.attempt_id,
                expected_version=attempt.version,
                dispatch_lease_token=self._lease_token(attempt),
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
            semantic_pack = parse_registered_vlm_response(
                raw_response,
                parser_strategy_version=request.parser_strategy_version,
                parser_contract_sha256=request.parser_contract_sha256,
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
                failure_disposition=ProviderFailureDisposition.REPAIRABLE.value,
            )
            rejection = self._commit_terminal_failure(
                request,
                outcome,
                failed,
                terminal_code=code,
                terminal_outcome="denied",
            )
            return GenerateVlmEvidenceResult(rejection, failed)

        artifacts = _artifacts(request, attempt, semantic_pack)
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
            semantic_pack,
            artifacts,
        )

    def _recover_failed_attempt(
        self,
        request: GenerateVlmEvidenceRequest,
        outcome: CommandOutcome,
        attempt: GenerationAttempt,
    ) -> GenerateVlmEvidenceResult:
        if (
            attempt.failure_disposition
            == ProviderFailureDisposition.RETRYABLE.value
            and attempt.attempt_ordinal < attempt.max_attempts
        ):
            next_ordinal = attempt.attempt_ordinal + 1
            next_attempt = self._store.reserve_next_generation_attempt(
                attempt.attempt_id,
                expected_version=attempt.version,
                provider_idempotency_key=request.provider_idempotency_key_for(
                    next_ordinal
                ),
            )
            self._assert_attempt_identity(request, outcome, next_attempt)
            return GenerateVlmEvidenceResult(outcome, next_attempt)
        terminal_code = (
            "RETRY_BUDGET_EXHAUSTED"
            if attempt.failure_disposition
            == ProviderFailureDisposition.RETRYABLE.value
            else attempt.failure_code or "GENERATION_FAILED"
        )
        rejection = self._commit_terminal_failure(
            request,
            outcome,
            attempt,
            terminal_code=terminal_code,
            terminal_outcome="failed",
        )
        return GenerateVlmEvidenceResult(rejection, attempt)

    def _commit_terminal_failure(
        self,
        request: GenerateVlmEvidenceRequest,
        outcome: CommandOutcome,
        attempt: GenerationAttempt,
        *,
        terminal_code: str,
        terminal_outcome: Literal["denied", "failed"],
    ) -> CommandOutcome:
        chain = self._store.read_generation_attempt_chain(
            request.job, outcome.command_slot_id
        )
        if not chain or chain[-1].attempt_id != attempt.attempt_id:
            raise ValueError("terminal generation failure lost its exact Attempt chain")
        causal_attempts: list[dict[str, object]] = []
        for member in chain:
            if member.state != "failed":
                raise ValueError("terminal generation chain contains a non-failed predecessor")
            causal_attempts.append(
                {
                    "attempt_id": str(member.attempt_id),
                    "attempt_ordinal": member.attempt_ordinal,
                    "failure_code": member.failure_code,
                    "failure_detail": _json_object(
                        member.failure_detail_json or "{}",
                        "failure_detail_json",
                    ),
                    "failure_disposition": member.failure_disposition,
                    "provider_idempotency_key": member.provider_idempotency_key,
                    "provider_request_id": member.provider_request_id,
                }
            )
        detail = _json_bytes(
            {
                "attempts": causal_attempts,
                "max_attempts": attempt.max_attempts,
                "retry_policy_sha256": attempt.retry_policy_hash,
                "terminal_reason": terminal_code,
            }
        ).decode("utf-8")
        return self._store.commit_generation_rejection(
            attempt.attempt_id,
            expected_version=attempt.version,
            rejection=CommandRejection(
                outcome.command_slot_id,
                terminal_code,
                detail,
                outcome=terminal_outcome,
            ),
        )

    @staticmethod
    def _lease_token(attempt: GenerationAttempt) -> str:
        token = attempt.dispatch_lease_token
        if token is None:
            raise ValueError("generation provider operation requires its persisted lease token")
        return token

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
        semantic_pack = parse_registered_vlm_response(
            raw_response,
            parser_strategy_version=request.parser_strategy_version,
            parser_contract_sha256=request.parser_contract_sha256,
            manifest=request.manifest,
            manifest_set=request.manifest_set,
            request_identity=request.request_identity,
            policy=request.parse_policy,
        )
        return GenerateVlmEvidenceResult(
            outcome,
            attempt,
            semantic_pack,
            _artifacts(request, attempt, semantic_pack),
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
            or attempt.provider_idempotency_key
            != request.provider_idempotency_key_for(attempt.attempt_ordinal)
            or attempt.request_payload.content_hash
            != request.request_identity.request_payload_sha256
            or attempt.retry_policy_hash != request.retry_policy.canonical_hash
            or attempt.max_attempts != request.retry_policy.max_attempts
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
    semantic_pack: SemanticPackValue,
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
        "raw_response_sha256": semantic_pack.raw_response_sha256,
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
            artifact_type="vlm_semantic_pack",
            logical_id=f"semantic_pack_{request.manifest.canonical_hash[7:39]}",
            payload=semantic_pack.to_mapping(),
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
