"""Commit one truthful aggregate Receipt over terminal per-episode VLM outcomes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from ..media.types import canonical_sha256, sha256_prefixed
from ..store import (
    VLM_BATCH_FINALIZER_COMMAND_NAME,
    VLM_BATCH_FINALIZER_STRATEGY_VERSION,
    VLM_BATCH_IDEMPOTENCY_PREFIX,
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandSuccess,
    CommittedVlmInputReference,
    Job,
    PersistedVlmGenerationChild,
    VlmSemanticPackSetChild,
)
from ..store.models import canonical_payload_hash, canonical_recipe_scope

VlmBatchChildState = Literal["succeeded", "denied", "failed"]


@dataclass(frozen=True, slots=True)
class VlmBatchChildOutcome:
    episode_index: int
    idempotency_key: str
    window_manifest_sha256: str
    source_manifest_sha256: str
    source_provenance_sha256: str
    request_hash: str
    state: VlmBatchChildState
    receipt_id: UUID
    artifact_set_id: UUID | None

    def __post_init__(self) -> None:
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise ValueError("episode_index must be non-negative")
        if type(self.idempotency_key) is not str or not self.idempotency_key.strip():  # noqa: E721
            raise ValueError("child idempotency_key must be non-empty")
        for value, field_name in (
            (self.window_manifest_sha256, "window_manifest_sha256"),
            (self.source_manifest_sha256, "source_manifest_sha256"),
            (self.source_provenance_sha256, "source_provenance_sha256"),
            (self.request_hash, "request_hash"),
        ):
            sha256_prefixed(value, field_name)
        if self.state not in ("succeeded", "denied", "failed"):
            raise ValueError("VLM batch child must be terminal")
        if not isinstance(self.receipt_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("VLM batch child requires an exact Receipt")
        if self.state == "succeeded":
            if not isinstance(self.artifact_set_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise ValueError("succeeded VLM child requires an ArtifactSet")
        elif self.artifact_set_id is not None:
            raise ValueError("rejected VLM child cannot claim an ArtifactSet")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_set_id": (
                str(self.artifact_set_id) if self.artifact_set_id is not None else None
            ),
            "episode_index": self.episode_index,
            "idempotency_key": self.idempotency_key,
            "receipt_id": str(self.receipt_id),
            "request_hash": self.request_hash,
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_provenance_sha256": self.source_provenance_sha256,
            "state": self.state,
            "window_manifest_sha256": self.window_manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class FinalizeVlmBatchRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    declared_episode_count: int
    source_manifest_sha256: str
    source_provenance_sha256: str
    children: tuple[VlmBatchChildOutcome, ...]
    strategy_version: str = VLM_BATCH_FINALIZER_STRATEGY_VERSION

    def __post_init__(self) -> None:
        if type(self.job) is not Job:  # noqa: E721
            raise ValueError("job must be an exact Job")
        if type(self.idempotency_key) is not str or not self.idempotency_key.strip():  # noqa: E721
            raise ValueError("idempotency_key must be non-empty")
        if not self.idempotency_key.startswith(VLM_BATCH_IDEMPOTENCY_PREFIX):
            raise ValueError("VLM batch idempotency_key must use the reserved namespace")
        if self.artifact_scope != canonical_recipe_scope(self.job):
            raise ValueError("artifact_scope must be the canonical Job scope")
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:  # noqa: E721
            raise ValueError("artifact_revision must be positive")
        if type(self.declared_episode_count) is not int or self.declared_episode_count < 1:  # noqa: E721
            raise ValueError("declared_episode_count must be positive")
        sha256_prefixed(self.source_manifest_sha256, "source_manifest_sha256")
        sha256_prefixed(self.source_provenance_sha256, "source_provenance_sha256")
        if self.strategy_version != VLM_BATCH_FINALIZER_STRATEGY_VERSION:
            raise ValueError("VLM batch finalizer strategy is not registered")
        if type(self.children) is not tuple or not self.children:  # noqa: E721
            raise ValueError("VLM batch requires terminal child outcomes")
        if any(type(item) is not VlmBatchChildOutcome for item in self.children):  # noqa: E721
            raise ValueError("VLM batch children must be exact typed outcomes")
        if tuple(item.episode_index for item in self.children) != tuple(
            range(len(self.children))
        ):
            raise ValueError("VLM batch children must cover ordered episode indexes")
        if any(item.state != "succeeded" for item in self.children):
            raise ValueError(
                "VLM batch finalizer accepts only independently provable succeeded children"
            )
        if len(self.children) != self.declared_episode_count:
            raise ValueError("successful VLM batch must cover every declared episode")
        if any(
            item.source_manifest_sha256 != self.source_manifest_sha256
            or item.source_provenance_sha256 != self.source_provenance_sha256
            for item in self.children
        ):
            raise ValueError("VLM batch children do not bind the declared source provenance")

    @property
    def request_hash(self) -> str:
        return canonical_sha256(
            {
                "artifact_revision": self.artifact_revision,
                "artifact_scope": {
                    "key": self.artifact_scope.key,
                    "kind": self.artifact_scope.kind,
                    "namespace": self.artifact_scope.namespace,
                },
                "children": [item.to_mapping() for item in self.children],
                "declared_episode_count": self.declared_episode_count,
                "job": {"job_key": self.job.job_key, "profile": self.job.profile},
                "strategy_version": self.strategy_version,
                "source_manifest_sha256": self.source_manifest_sha256,
                "source_provenance_sha256": self.source_provenance_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class FinalizeVlmBatchResult:
    outcome: CommandOutcome
    artifact: ArtifactMember | None = None


class VlmBatchFinalizerStore(Protocol):
    def read_committed_vlm_generation_child(
        self,
        job: Job,
        idempotency_key: str,
    ) -> PersistedVlmGenerationChild: ...

    def read_committed_vlm_input_reference(
        self,
        job: Job,
        idempotency_key: str,
    ) -> CommittedVlmInputReference: ...

    def claim_vlm_batch_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def commit_vlm_batch_success(self, success: CommandSuccess) -> CommandOutcome: ...

class FinalizeVlmBatchCommand:
    """Commit only after every declared child has an exact terminal Receipt."""

    def __init__(self, store: VlmBatchFinalizerStore) -> None:
        self._store = store

    def execute(self, request: FinalizeVlmBatchRequest) -> FinalizeVlmBatchResult:
        persisted_children: list[PersistedVlmGenerationChild] = []
        member_children: list[VlmSemanticPackSetChild] = []
        for child in request.children:
            persisted = self._store.read_committed_vlm_generation_child(
                request.job,
                child.idempotency_key,
            )
            self._assert_exact_child(child, persisted)
            persisted_children.append(persisted)
            reference = self._store.read_committed_vlm_input_reference(
                request.job,
                child.idempotency_key,
            )
            if (
                reference.request_record.receipt_id != persisted.receipt_id
                or reference.request_record.artifact_set_id != persisted.artifact_set_id
                or reference.request_record.content_hash != persisted.reference.content_hash
            ):
                raise ValueError("VLM child member references do not match its committed outcome")
            member_children.append(
                VlmSemanticPackSetChild(
                    episode_index=persisted.episode_index,
                    idempotency_key=persisted.idempotency_key,
                    request_hash=persisted.request_hash,
                    request_record=reference.request_record,
                    response_record=reference.response_record,
                    semantic_pack=reference.semantic_pack,
                )
            )
        self._reject_duplicate_children(tuple(persisted_children))
        verified_payload = self._verified_payload(
            request,
            tuple(persisted_children),
            tuple(member_children),
        )
        verified_request_hash = canonical_sha256(
            {
                "artifact_revision": request.artifact_revision,
                "artifact_scope": {
                    "key": request.artifact_scope.key,
                    "kind": request.artifact_scope.kind,
                    "namespace": request.artifact_scope.namespace,
                },
                **verified_payload,
                "job": {
                    "job_key": request.job.job_key,
                    "profile": request.job.profile,
                },
            }
        )

        outcome = self._store.claim_vlm_batch_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                VLM_BATCH_FINALIZER_COMMAND_NAME,
                verified_request_hash,
                execution_kind="deterministic",
            )
        )
        if outcome.state in ("succeeded", "denied", "failed"):
            return FinalizeVlmBatchResult(outcome)

        payload_json = json.dumps(
            {
                "completion_policy": "all_committed_episodes",
                **verified_payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        artifact = ArtifactMember(
            "vlm_semantic_pack_set",
            "vlm_semantic_pack_set",
            request.artifact_revision,
            request.artifact_scope,
            canonical_payload_hash(payload_json),
            payload_json,
        )
        success = CommandSuccess(
            outcome.command_slot_id,
            canonical_sha256(
                [
                    {
                        "artifact_type": artifact.artifact_type,
                        "content_hash": artifact.content_hash,
                        "logical_id": artifact.logical_id,
                        "payload_json": json.loads(artifact.payload_json),
                        "revision": artifact.revision,
                        "scope": {
                            "key": artifact.scope.key,
                            "kind": artifact.scope.kind,
                            "namespace": artifact.scope.namespace,
                        },
                    }
                ]
            ),
            (artifact,),
        )
        committed = self._store.commit_vlm_batch_success(success)
        return FinalizeVlmBatchResult(committed, artifact)

    @staticmethod
    def _assert_exact_child(
        child: VlmBatchChildOutcome,
        persisted: PersistedVlmGenerationChild,
    ) -> None:
        if (
            child.state != "succeeded"
            or persisted.idempotency_key != child.idempotency_key
            or persisted.episode_index != child.episode_index
            or persisted.window_manifest_sha256 != child.window_manifest_sha256
            or persisted.source_manifest_sha256 != child.source_manifest_sha256
            or persisted.source_provenance_sha256 != child.source_provenance_sha256
            or persisted.request_hash != child.request_hash
            or persisted.receipt_id != child.receipt_id
            or persisted.artifact_set_id != child.artifact_set_id
        ):
            raise ValueError(
                "VLM batch child does not match its exact persisted Kernel outcome"
            )

    @staticmethod
    def _reject_duplicate_children(
        children: tuple[PersistedVlmGenerationChild, ...],
    ) -> None:
        for field_name in (
            "idempotency_key",
            "request_hash",
            "receipt_id",
            "artifact_set_id",
            "window_manifest_sha256",
            "attempt_id",
        ):
            values = tuple(getattr(child, field_name) for child in children)
            if len(values) != len(set(values)):
                raise ValueError(f"VLM batch contains duplicate child {field_name}")

    @staticmethod
    def _verified_payload(
        request: FinalizeVlmBatchRequest,
        children: tuple[PersistedVlmGenerationChild, ...],
        member_children: tuple[VlmSemanticPackSetChild, ...],
    ) -> dict[str, object]:
        if tuple(child.episode_index for child in children) != tuple(
            range(request.declared_episode_count)
        ):
            raise ValueError("persisted VLM children do not cover declared episode indexes")
        if any(
            child.source_manifest_sha256 != request.source_manifest_sha256
            or child.source_provenance_sha256 != request.source_provenance_sha256
            for child in children
        ):
            raise ValueError("persisted VLM children do not bind the declared source")
        policies = tuple(child.request_policy for child in children)
        if not policies or any(policy != policies[0] for policy in policies[1:]):
            raise ValueError("VLM batch children do not share one frozen request policy")
        return {
            "children": [child.to_mapping() for child in member_children],
            "declared_episode_count": request.declared_episode_count,
            "request_policy": policies[0].to_mapping(),
            "source_manifest_sha256": request.source_manifest_sha256,
            "source_provenance_sha256": request.source_provenance_sha256,
            "strategy_version": request.strategy_version,
        }


__all__ = (
    "FinalizeVlmBatchCommand",
    "FinalizeVlmBatchRequest",
    "FinalizeVlmBatchResult",
    "VLM_BATCH_FINALIZER_STRATEGY_VERSION",
    "VlmBatchChildOutcome",
    "VlmBatchFinalizerStore",
)
