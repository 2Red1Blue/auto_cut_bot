"""Complete-batch authority over explicitly selected generation or derived V4 evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from ..media.types import canonical_sha256
from ..source_manifest import decode_source_manifest
from ..store.models import (
    ArtifactMember, CommandClaim, CommandOutcome, CommandSuccess, CommittedArtifactMemberReference,
    CommittedSemanticInputs, CommittedSemanticInputsRequest, CommittedV4SemanticChildInspection,
    CommittedVlmSemanticInput, Job, PersistedCommittedArtifactMember, artifact_set_hash,
    canonical_payload_hash, canonical_recipe_scope,
)
from .reprocess_vlm_evidence_command import (
    ReprocessVlmEvidenceRequest, VlmReprocessStore, project_reprocessed_semantic_input,
)

FINALIZE_DERIVED_VLM_BATCH_COMMAND = "FinalizeDerivedVlmBatchCommand@1"
DERIVED_VLM_BATCH_STRATEGY = "vlm-semantic-pack-set-derived-v1"


@dataclass(frozen=True, slots=True)
class VlmBatchEvidenceSelection:
    episode_index: int
    idempotency_key: str
    provenance: CommittedArtifactMemberReference

    def __post_init__(self) -> None:
        if type(self.episode_index) is not int or self.episode_index < 0:
            raise ValueError("batch evidence episode index is invalid")
        if type(self.idempotency_key) is not str or not self.idempotency_key.strip():
            raise ValueError("batch evidence requires an exact idempotency key")
        if (type(self.provenance) is not CommittedArtifactMemberReference
                or self.provenance.member_ordinal != 0
                or self.provenance.artifact_type not in ("vlm_request_record", "reprocessed_vlm_evidence")):
            raise ValueError("batch evidence requires generation or derivation provenance")

    def to_mapping(self) -> dict[str, object]:
        return {"episode_index": self.episode_index, "idempotency_key": self.idempotency_key,
                "provenance": self.provenance.to_mapping()}


@dataclass(frozen=True, slots=True)
class FinalizeDerivedVlmBatchRequest:
    job: Job
    source_manifest: CommittedArtifactMemberReference
    children: tuple[VlmBatchEvidenceSelection, ...]
    artifact_revision: int = 1

    def __post_init__(self) -> None:
        if type(self.job) is not Job or type(self.source_manifest) is not CommittedArtifactMemberReference:
            raise ValueError("derived batch requires exact Job and source reference")
        if (self.source_manifest.artifact_type != "whole_series_source_manifest"
                or self.source_manifest.logical_id != "whole_series_source_manifest"
                or self.source_manifest.member_ordinal != 0
                or self.source_manifest.scope != canonical_recipe_scope(self.job)):
            raise ValueError("derived batch source reference is invalid")
        if (type(self.children) is not tuple or not self.children
                or any(type(item) is not VlmBatchEvidenceSelection for item in self.children)
                or tuple(item.episode_index for item in self.children) != tuple(range(len(self.children)))):
            raise ValueError("derived batch requires one ordered selection per episode")
        if any(item.provenance.scope != self.source_manifest.scope for item in self.children):
            raise ValueError("derived batch child belongs to another Job")
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:
            raise ValueError("derived batch artifact revision must be positive")

    def to_mapping(self) -> dict[str, object]:
        return {"strategy_version": DERIVED_VLM_BATCH_STRATEGY,
                "job": {"job_key": self.job.job_key, "profile": self.job.profile},
                "source_manifest": self.source_manifest.to_mapping(),
                "children": [item.to_mapping() for item in self.children], "artifact_revision": self.artifact_revision}

    @property
    def request_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def idempotency_key(self) -> str:
        return "vlm-derived-batch:" + self.request_hash[7:]

    @classmethod
    def from_mapping(cls, value: object) -> FinalizeDerivedVlmBatchRequest:
        if type(value) is not dict:
            raise ValueError("derived batch request must be a closed object")
        try:
            result = cls(Job(**value["job"]), CommittedArtifactMemberReference.from_mapping(value["source_manifest"]),
                         tuple(VlmBatchEvidenceSelection(item["episode_index"], item["idempotency_key"],
                               CommittedArtifactMemberReference.from_mapping(item["provenance"])) for item in value["children"]),
                         value["artifact_revision"])
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("derived batch request is malformed") from error
        if result.to_mapping() != value:
            raise ValueError("derived batch request is noncanonical or has unknown fields")
        return result


class DerivedVlmBatchStore(VlmReprocessStore, Protocol):
    def read_committed_artifact_member(self, reference: CommittedArtifactMemberReference) -> PersistedCommittedArtifactMember: ...

    def read_committed_v4_semantic_child_inspection(self, job: Job, idempotency_key: str) -> CommittedV4SemanticChildInspection: ...

    def commit_derived_vlm_batch_success(self, request: FinalizeDerivedVlmBatchRequest, success: CommandSuccess) -> CommandOutcome: ...


def rebuild_derived_vlm_batch(store: DerivedVlmBatchStore, request: FinalizeDerivedVlmBatchRequest):
    """Independently verify all selected windows, source ownership and frozen policies."""
    source = store.read_whole_series_source_manifest(request.job, request.source_manifest.artifact_set_id)
    actual_source = CommittedArtifactMemberReference(
        source.receipt_id, source.artifact_set_id, 0, source.reference.scope, source.reference.artifact_type,
        source.reference.logical_id, source.reference.revision, source.reference.content_hash,
    )
    if request.source_manifest != actual_source:
        raise ValueError("derived batch SourceManifest reference differs from committed owner")
    decoded = decode_source_manifest(source.payload_json, source.proxy_blobs)
    decoded.census.require_purpose("semantic_analysis")
    if len(decoded.episodes) != len(request.children):
        raise ValueError("derived batch must cover every committed source episode")
    inputs: list[CommittedVlmSemanticInput] = []
    for selection, episode in zip(request.children, decoded.episodes, strict=True):
        member = store.read_committed_artifact_member(selection.provenance)
        if member.reference != selection.provenance:
            raise ValueError("derived batch child provenance differs")
        if selection.provenance.artifact_type == "reprocessed_vlm_evidence":
            payload = json.loads(member.payload_json)
            child_request = ReprocessVlmEvidenceRequest.from_mapping(payload["request"])
            if (child_request.job != request.job or child_request.source_artifact_set_id != source.artifact_set_id
                    or child_request.idempotency_key != selection.idempotency_key):
                raise ValueError("derived batch child request belongs to another source or Job")
            item = project_reprocessed_semantic_input(store, child_request, CommandOutcome(
                member.command_slot_id, "succeeded", receipt_id=selection.provenance.receipt_id,
                artifact_set_id=selection.provenance.artifact_set_id,
            ))
        else:
            inspected = store.read_committed_v4_semantic_child_inspection(request.job, selection.idempotency_key)
            child = inspected.semantic_input.semantic_pack.source_child
            if (child.receipt_id != selection.provenance.receipt_id or child.artifact_set_id != selection.provenance.artifact_set_id
                    or child.reference.content_hash != selection.provenance.content_hash
                    or child.source_manifest_sha256 != source.reference.content_hash
                    or child.source_provenance_sha256 != source.canonical_hash):
                raise ValueError("derived batch generation child differs from selected ownership")
            value = inspected.semantic_input
            item = CommittedVlmSemanticInput(value.source_window, value.request_identity,
                                            value.semantic_pack, value.response_record, value.raw_response)
        if (item.source_window.episode_index != selection.episode_index
                or item.source_window.window_manifest_sha256 != episode.manifest.canonical_hash
                or item.source_window.window_manifest_set_sha256 != episode.manifest_set.canonical_hash
                or item.source_window.source_id != episode.manifest.source_id
                or item.source_window.source_sha256 != episode.manifest.source_sha256):
            raise ValueError("derived batch selected child differs from its exact source window")
        inputs.append(item)
    policies = tuple(item.semantic_pack.source_child.request_policy for item in inputs)
    if any(policy != policies[0] for policy in policies):
        raise ValueError("derived batch cannot mix frozen provider observation policies")
    payload = {"schema_version": DERIVED_VLM_BATCH_STRATEGY, "request": request.to_mapping(),
               "source_manifest_sha256": source.reference.content_hash, "source_provenance_sha256": source.canonical_hash,
               "request_policy": policies[0].to_mapping(), "completion_policy": "all_committed_episodes"}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    artifact = ArtifactMember("vlm_semantic_pack_set", "vlm_semantic_pack_set", request.artifact_revision,
                              canonical_recipe_scope(request.job), canonical_payload_hash(serialized), serialized)
    return artifact, source, decoded.census, policies[0], tuple(inputs)


def read_derived_vlm_semantic_inputs(store: DerivedVlmBatchStore, request: CommittedSemanticInputsRequest) -> CommittedSemanticInputs:
    member = store.read_committed_artifact_member(request.vlm_semantic_pack_set)
    payload = json.loads(member.payload_json)
    batch = FinalizeDerivedVlmBatchRequest.from_mapping(payload["request"])
    if batch.job != request.job or batch.source_manifest != request.source_manifest:
        raise ValueError("derived batch input request differs from committed source and Job")
    committed = store.read_committed_artifact_set(
        request.job, command_slot_id=member.command_slot_id, receipt_id=member.reference.receipt_id,
        artifact_set_id=member.reference.artifact_set_id, expected_request_hash=batch.request_hash,
        expected_command_name=FINALIZE_DERIVED_VLM_BATCH_COMMAND, expected_execution_kind="deterministic",
    )
    artifact, source, grant, policy, inputs = rebuild_derived_vlm_batch(store, batch)
    if (len(committed.members) != 1 or committed.members[0].reference != request.vlm_semantic_pack_set
            or artifact.content_hash != member.reference.content_hash
            or json.loads(artifact.payload_json) != payload):
        raise ValueError("derived VLM aggregate differs from audited child reconstruction")
    return CommittedSemanticInputs(source, grant, request.vlm_semantic_pack_set, policy, inputs, DERIVED_VLM_BATCH_STRATEGY)


class FinalizeDerivedVlmBatchCommand:
    def __init__(self, store: DerivedVlmBatchStore) -> None:
        self._store = store

    def execute(self, request: FinalizeDerivedVlmBatchRequest) -> CommandOutcome:
        artifact, *_ = rebuild_derived_vlm_batch(self._store, request)
        outcome = self._store.claim_command(CommandClaim(request.job, request.idempotency_key,
            FINALIZE_DERIVED_VLM_BATCH_COMMAND, request.request_hash, execution_kind="deterministic"))
        if outcome.state in ("failed", "denied"):
            return outcome
        if outcome.state != "succeeded":
            outcome = self._store.commit_derived_vlm_batch_success(request, CommandSuccess(
                outcome.command_slot_id, artifact_set_hash((artifact,)), (artifact,)))
        if outcome.receipt_id is None or outcome.artifact_set_id is None:
            raise ValueError("derived batch did not commit an exact Receipt")
        reference = CommittedArtifactMemberReference(outcome.receipt_id, outcome.artifact_set_id, 0,
            artifact.scope, artifact.artifact_type, artifact.logical_id, artifact.revision, artifact.content_hash)
        read_derived_vlm_semantic_inputs(self._store, CommittedSemanticInputsRequest(request.job, request.source_manifest, reference))
        return outcome
