"""Zero-provider derivation from one exact terminal VLM response.

The original generation and its failure remain immutable. This deterministic
producer has its own request identity, ArtifactSet and Receipt; it is not an
alternative way to manufacture a succeeded generation attempt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..context_pack import WindowContextPack
from ..media.types import canonical_sha256, sha256_prefixed
from ..source_manifest import decode_source_manifest
from ..store.models import (
    ArtifactMember, BlobRef, CommandClaim, CommandOutcome, CommandRejection, CommandSuccess,
    GenerationAttempt, Job, PersistedCommittedArtifactSet, PersistedWholeSeriesSourceManifest,
    CommittedArtifactMemberReference, CommittedVlmSemanticInput, PersistedReprocessedVlmChild,
    PersistedVlmSemanticPackV4, SourceWindowIdentity, VlmSemanticPackReference,
    artifact_set_hash, canonical_payload_hash, canonical_recipe_scope,
)
from ..vlm.enum_normalization import NormalizedVlmResponse, normalize_vlm_enum_sets
from ..vlm.models import VlmParsePolicy, VlmRequestIdentity
from ..vlm.normalized_contracts import (
    V4_PARSERS, VLM_PARSER_NORMALIZED_V4, parse_registered_vlm_response, require_parser_contract,
)
from ..vlm.parser import VlmResponseIndeterminate, VlmResponseRejected, _constant, _pairs_object
from ..vlm.retry_policy import GenerationRetryPolicy
from ..vlm.semantic_pack_v4 import VlmSemanticPackV4

REPROCESS_VLM_COMMAND = "ReprocessVlmEvidenceCommand@1"
_MAX_REQUEST_BYTES = 16 * 1024 * 1024


def _bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ReprocessVlmEvidenceRequest:
    job: Job
    parent_command_slot_id: UUID
    parent_receipt_id: UUID
    parent_attempt_id: UUID
    parent_request_hash: str
    parent_request_payload_sha256: str
    parent_raw_response_sha256: str
    source_artifact_set_id: UUID
    episode_index: int
    parent_artifact_revision: int
    target_parser_contract_sha256: str

    def __post_init__(self) -> None:
        if type(self.job) is not Job:
            raise ValueError("reprocess requires an exact Job")
        for name in ("parent_command_slot_id", "parent_receipt_id", "parent_attempt_id", "source_artifact_set_id"):
            if type(getattr(self, name)) is not UUID:
                raise ValueError(f"{name} must be an exact UUID")
        for name in ("parent_request_hash", "parent_request_payload_sha256", "parent_raw_response_sha256", "target_parser_contract_sha256"):
            sha256_prefixed(getattr(self, name), name)
        if type(self.episode_index) is not int or self.episode_index < 0:
            raise ValueError("episode_index must be non-negative")
        if type(self.parent_artifact_revision) is not int or self.parent_artifact_revision < 1:
            raise ValueError("parent_artifact_revision must be positive")
        require_parser_contract(VLM_PARSER_NORMALIZED_V4, self.target_parser_contract_sha256)

    def to_mapping(self) -> dict[str, object]:
        return {
            "strategy_version": "reprocess-vlm-evidence-v1",
            "target_parser_strategy": VLM_PARSER_NORMALIZED_V4,
            "target_parser_contract_sha256": self.target_parser_contract_sha256,
            "job": {"job_key": self.job.job_key, "profile": self.job.profile},
            "parent_command_slot_id": str(self.parent_command_slot_id),
            "parent_receipt_id": str(self.parent_receipt_id),
            "parent_attempt_id": str(self.parent_attempt_id),
            "parent_request_hash": self.parent_request_hash,
            "parent_request_payload_sha256": self.parent_request_payload_sha256,
            "parent_raw_response_sha256": self.parent_raw_response_sha256,
            "source_artifact_set_id": str(self.source_artifact_set_id),
            "episode_index": self.episode_index,
            "parent_artifact_revision": self.parent_artifact_revision,
            "provider_call_budget": 0,
        }

    @property
    def request_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def idempotency_key(self) -> str:
        return "vlm-reprocess:" + self.request_hash[7:]

    @classmethod
    def from_mapping(cls, value: object) -> ReprocessVlmEvidenceRequest:
        if type(value) is not dict or type(value.get("job")) is not dict:
            raise ValueError("reprocess selector must be a closed object")
        try:
            result = cls(
                Job(**value["job"]), UUID(value["parent_command_slot_id"]), UUID(value["parent_receipt_id"]),
                UUID(value["parent_attempt_id"]), value["parent_request_hash"], value["parent_request_payload_sha256"],
                value["parent_raw_response_sha256"], UUID(value["source_artifact_set_id"]), value["episode_index"],
                value["parent_artifact_revision"], value["target_parser_contract_sha256"],
            )
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("reprocess selector is malformed") from error
        if result.to_mapping() != value:
            raise ValueError("reprocess selector is noncanonical or contains unknown fields")
        return result


class VlmReprocessStore(Protocol):
    def read_vlm_reprocess_parent(self, job: Job, *, command_slot_id: UUID, receipt_id: UUID,
                                 attempt_id: UUID, expected_request_hash: str) -> GenerationAttempt: ...

    def read_whole_series_source_manifest(self, job: Job, artifact_set_id: UUID) -> PersistedWholeSeriesSourceManifest: ...

    def read_immutable_blob(self, job: Job, reference: BlobRef) -> bytes: ...

    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def commit_reprocessed_vlm_success(self, request: ReprocessVlmEvidenceRequest,
                                      success: CommandSuccess) -> CommandOutcome: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...

    def read_committed_artifact_set(self, job: Job, *, command_slot_id: UUID, receipt_id: UUID,
                                    artifact_set_id: UUID, expected_request_hash: str,
                                    expected_command_name: str, expected_execution_kind: str) -> PersistedCommittedArtifactSet: ...


@dataclass(frozen=True, slots=True)
class ReprocessedVlmEvidence:
    request: ReprocessVlmEvidenceRequest
    semantic_pack: VlmSemanticPackV4
    normalization: NormalizedVlmResponse
    artifact: ArtifactMember
    pack_artifact: ArtifactMember
    request_identity: VlmRequestIdentity
    source: PersistedWholeSeriesSourceManifest
    parent_attempt: GenerationAttempt

    @property
    def artifacts(self) -> tuple[ArtifactMember, ...]:
        return (self.artifact, self.pack_artifact)


def rebuild_reprocessed_vlm_evidence(
    store: VlmReprocessStore, request: ReprocessVlmEvidenceRequest,
) -> ReprocessedVlmEvidence:
    """Audit persisted ownership before parsing; used by producer and Store reader."""
    if type(request) is not ReprocessVlmEvidenceRequest:
        raise ValueError("reprocess requires an exact typed request")
    require_parser_contract(VLM_PARSER_NORMALIZED_V4, request.target_parser_contract_sha256)
    attempt = store.read_vlm_reprocess_parent(
        request.job, command_slot_id=request.parent_command_slot_id,
        receipt_id=request.parent_receipt_id, attempt_id=request.parent_attempt_id,
        expected_request_hash=request.parent_request_hash,
    )
    if (attempt.state != "failed" or attempt.raw_response is None
            or attempt.request_payload.content_hash != request.parent_request_payload_sha256
            or attempt.raw_response.content_hash != request.parent_raw_response_sha256):
        raise ValueError("reprocess parent is not the exact completed provider response")
    if attempt.request_payload.byte_length > _MAX_REQUEST_BYTES:
        raise ValueError("reprocess parent request exceeds the read budget")
    raw_request = store.read_immutable_blob(request.job, attempt.request_payload)
    if len(raw_request) > _MAX_REQUEST_BYTES or _hash(raw_request) != request.parent_request_payload_sha256:
        raise ValueError("reprocess parent request bytes differ from frozen identity")
    frozen = json.loads(raw_request.decode("utf-8", "strict"), object_pairs_hook=_pairs_object,
                        parse_constant=_constant)
    fields = {"model_id", "parse_policy", "parser_strategy_version", "parser_contract_sha256", "prompt", "prompt_version",
              "provider_id", "proxy_blob", "request_parameters", "retry_policy", "retry_policy_sha256", "response_schema",
              "window_manifest_sha256", "window_manifest_set_sha256"}
    if type(frozen) is not dict or set(frozen) not in (fields, fields | {"context_pack", "context_pack_sha256"}):
        raise ValueError("reprocess requires an exact V4 frozen request schema")
    # Selecting a new target is explicit. The old implementation may be unavailable;
    # its identity is preserved and bound, never treated as the new executable.
    if frozen["parser_strategy_version"] not in V4_PARSERS:
        raise ValueError("reprocess only supports the V4 response wire")
    sha256_prefixed(frozen["parser_contract_sha256"], "parent parser contract")
    if frozen["provider_id"] != attempt.provider_id:
        raise ValueError("reprocess parent provider binding differs")
    schema = frozen["response_schema"]
    properties = schema.get("properties") if type(schema) is dict else None
    version = properties.get("schema_version") if type(properties) is dict else None
    if type(version) is not dict or type(version.get("const")) is not int or version["const"] != 4:
        raise ValueError("reprocess parent did not request the V4 wire schema")
    policy_value = frozen["parse_policy"]
    retry_value = frozen["retry_policy"]
    if type(policy_value) is not dict or type(retry_value) is not dict:
        raise ValueError("reprocess parent policy is malformed")
    policy = VlmParsePolicy(**policy_value)
    if set(retry_value) != {"strategy_version", "max_attempts", "backoff_seconds"}:
        raise ValueError("reprocess parent retry policy has unknown or missing fields")
    if type(retry_value["backoff_seconds"]) is not list:
        raise ValueError("reprocess parent backoff must be an exact array")
    retry = GenerationRetryPolicy(retry_value["strategy_version"], retry_value["max_attempts"], tuple(retry_value["backoff_seconds"]))
    if (policy.to_mapping() != policy_value or retry.canonical_hash != frozen["retry_policy_sha256"]
            or retry.canonical_hash != attempt.retry_policy_hash or retry.max_attempts != attempt.max_attempts):
        raise ValueError("reprocess parent policy identity differs")
    source = store.read_whole_series_source_manifest(request.job, request.source_artifact_set_id)
    if source.job_id != attempt.job_id or source.reference.scope != canonical_recipe_scope(request.job):
        raise ValueError("reprocess source owner differs from provider attempt")
    prepared = decode_source_manifest(source.payload_json, source.proxy_blobs)
    prepared.census.require_purpose("semantic_analysis")
    if request.episode_index >= len(prepared.episodes):
        raise ValueError("reprocess episode is outside the committed source")
    episode = prepared.episodes[request.episode_index]
    manifest, manifests = episode.manifest, episode.manifest_set
    if (manifest.canonical_hash != frozen["window_manifest_sha256"]
            or manifests.canonical_hash != frozen["window_manifest_set_sha256"]
            or manifest.proxy_blob_ref.to_mapping() != frozen["proxy_blob"]):
        raise ValueError("reprocess source/window/proxy binding differs")
    context_hash: dict[str, object] = {}
    if "context_pack" in frozen:
        context = WindowContextPack.from_mapping(frozen["context_pack"])
        if context.canonical_hash != frozen["context_pack_sha256"]:
            raise ValueError("reprocess parent context hash differs")
        context_hash = {"context_pack_sha256": context.canonical_hash}
    identity = VlmRequestIdentity.from_manifest(
        manifest, manifests, prompt_template_sha256=_hash(frozen["prompt"].encode("utf-8")),
        prompt_version=frozen["prompt_version"], response_schema_sha256=_hash(_bytes(schema)),
        model_id=frozen["model_id"], provider_id=frozen["provider_id"],
        request_parameters_sha256=_hash(_bytes(frozen["request_parameters"])),
        request_payload_sha256=request.parent_request_payload_sha256, parse_policy=policy,
    )
    scope = canonical_recipe_scope(request.job)
    original_hash = canonical_sha256({
        "artifact_revision": request.parent_artifact_revision, "episode_index": request.episode_index,
        "artifact_scope": {"namespace": scope.namespace, "kind": scope.kind, "key": scope.key},
        "identity_sha256": identity.canonical_hash, "job": {"job_key": request.job.job_key, "profile": request.job.profile},
        "parser_strategy_version": frozen["parser_strategy_version"], "retry_policy_sha256": retry.canonical_hash,
        "proxy_blob": frozen["proxy_blob"], "source_provenance_sha256": source.canonical_hash,
        "source_manifest_sha256": source.reference.content_hash, **context_hash,
    })
    if original_hash != request.parent_request_hash or original_hash != attempt.request_hash:
        raise ValueError("reprocess source/request envelope does not close to original command hash")
    if attempt.raw_response.byte_length > policy.max_response_bytes:
        raise VlmResponseIndeterminate("RESPONSE_BUDGET_EXCEEDED", "parent response exceeds frozen budget")
    raw = store.read_immutable_blob(request.job, attempt.raw_response)
    if _hash(raw) != request.parent_raw_response_sha256:
        raise ValueError("reprocess raw response bytes differ from parent hash")
    normalized = normalize_vlm_enum_sets(raw, policy)
    pack = parse_registered_vlm_response(
        raw, parser_strategy_version=VLM_PARSER_NORMALIZED_V4,
        parser_contract_sha256=request.target_parser_contract_sha256,
        manifest=manifest, manifest_set=manifests, request_identity=identity, policy=policy,
    )
    if type(pack) is not VlmSemanticPackV4:
        raise ValueError("reprocess target did not produce an exact V4 pack")
    payload = {
        "schema_version": "reprocessed-vlm-evidence-v1", "request": request.to_mapping(),
        "parent_parser_strategy": frozen["parser_strategy_version"],
        "parent_parser_contract_sha256": frozen["parser_contract_sha256"],
        "parent_provider_request_id": attempt.provider_request_id,
        "source_manifest_sha256": source.reference.content_hash, "source_provenance_sha256": source.canonical_hash,
        "window_manifest_sha256": manifest.canonical_hash, "window_manifest_set_sha256": manifests.canonical_hash,
        "normalization": normalized.to_mapping(), "semantic_pack": pack.to_mapping(),
    }
    serialized = _bytes(payload).decode("utf-8")
    artifact = ArtifactMember("reprocessed_vlm_evidence", "reprocessed_vlm_" + request.request_hash[7:],
                              1, scope, canonical_payload_hash(serialized), serialized)
    pack_json = _bytes(pack.to_mapping()).decode("utf-8")
    pack_artifact = ArtifactMember("vlm_semantic_pack", "reprocessed_semantic_pack_" + request.request_hash[7:],
                                   1, scope, canonical_payload_hash(pack_json), pack_json)
    return ReprocessedVlmEvidence(request, pack, normalized, artifact, pack_artifact, identity, source, attempt)


@dataclass(frozen=True, slots=True)
class ReprocessVlmEvidenceResult:
    outcome: CommandOutcome
    evidence: ReprocessedVlmEvidence | None = None


def read_reprocessed_vlm_evidence(
    store: VlmReprocessStore, request: ReprocessVlmEvidenceRequest, outcome: CommandOutcome,
) -> ReprocessedVlmEvidence:
    if outcome.state != "succeeded" or outcome.receipt_id is None or outcome.artifact_set_id is None:
        raise ValueError("reprocessed evidence requires an exact succeeded Receipt")
    committed = store.read_committed_artifact_set(
        request.job, command_slot_id=outcome.command_slot_id, receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id, expected_request_hash=request.request_hash,
        expected_command_name=REPROCESS_VLM_COMMAND, expected_execution_kind="deterministic",
    )
    evidence = rebuild_reprocessed_vlm_evidence(store, request)
    if len(committed.members) != len(evidence.artifacts):
        raise ValueError("reprocessed evidence requires its exact provenance and pack artifacts")
    for ordinal, (member, expected) in enumerate(zip(committed.members, evidence.artifacts, strict=True)):
        if (member.reference.member_ordinal != ordinal or member.reference.artifact_type != expected.artifact_type
                or member.reference.logical_id != expected.logical_id
                or member.reference.revision != expected.revision or member.reference.scope != expected.scope
                or member.reference.content_hash != expected.content_hash
                or canonical_payload_hash(member.payload_json) != expected.content_hash
                or json.loads(member.payload_json) != json.loads(expected.payload_json)):
            raise ValueError("reprocessed evidence differs from exact parent raw reconstruction")
    return evidence


def project_reprocessed_semantic_input(
    store: VlmReprocessStore, request: ReprocessVlmEvidenceRequest, outcome: CommandOutcome,
) -> CommittedVlmSemanticInput:
    """Expose audited derived V4 observations without inventing a generation success."""
    evidence = read_reprocessed_vlm_evidence(store, request, outcome)
    if outcome.receipt_id is None or outcome.artifact_set_id is None:
        raise ValueError("derived observation projection requires a committed outcome")
    artifact, pack_artifact = evidence.artifacts
    provenance = CommittedArtifactMemberReference(
        outcome.receipt_id, outcome.artifact_set_id, 0, artifact.scope, artifact.artifact_type,
        artifact.logical_id, artifact.revision, artifact.content_hash,
    )
    child = PersistedReprocessedVlmChild(
        provenance, artifact.payload_json, request.job, evidence.source.job_id,
        outcome.command_slot_id, request.request_hash, request.parent_attempt_id,
        request.parent_receipt_id, evidence.request_identity, evidence.parent_attempt.request_payload,
        evidence.source.reference.content_hash, evidence.source.canonical_hash, request.episode_index,
    )
    persisted = PersistedVlmSemanticPackV4(
        VlmSemanticPackReference(pack_artifact.scope, pack_artifact.logical_id, pack_artifact.revision, pack_artifact.content_hash),
        pack_artifact.payload_json, evidence.semantic_pack, child,
    )
    source = decode_source_manifest(evidence.source.payload_json, evidence.source.proxy_blobs)
    episode = source.episodes[request.episode_index]
    manifest = episode.manifest
    window = SourceWindowIdentity(
        request.episode_index, manifest.stream_index, manifest.core_range.start_pts, manifest.core_range.end_pts,
        manifest.canonical_hash, manifest.source_id, manifest.source_sha256, manifest.source_clock_id,
        episode.manifest_set.canonical_hash, evidence.source.proxy_blobs[request.episode_index],
    )
    if evidence.parent_attempt.raw_response is None:
        raise ValueError("derived observation lost its original response")
    return CommittedVlmSemanticInput(window, evidence.request_identity, persisted, provenance, evidence.parent_attempt.raw_response)


class ReprocessVlmEvidenceCommand:
    def __init__(self, store: VlmReprocessStore) -> None:
        self._store = store

    def execute(self, request: ReprocessVlmEvidenceRequest) -> ReprocessVlmEvidenceResult:
        outcome = self._store.claim_command(CommandClaim(
            request.job, request.idempotency_key, REPROCESS_VLM_COMMAND, request.request_hash,
            execution_kind="deterministic",
        ))
        if outcome.state in ("failed", "denied"):
            return ReprocessVlmEvidenceResult(outcome)
        if outcome.state == "succeeded":
            return ReprocessVlmEvidenceResult(outcome, read_reprocessed_vlm_evidence(self._store, request, outcome))
        try:
            evidence = rebuild_reprocessed_vlm_evidence(self._store, request)
        except (VlmResponseRejected, VlmResponseIndeterminate) as error:
            rejected = self._store.commit_command_rejection(CommandRejection(
                outcome.command_slot_id, error.code,
                _bytes({"parser_message": str(error), "parent_attempt_id": str(request.parent_attempt_id),
                        "raw_response_sha256": request.parent_raw_response_sha256,
                        "provider_call_budget": 0, "retryability": "local_reprocess_denied"}).decode("utf-8"),
            ))
            return ReprocessVlmEvidenceResult(rejected)
        success = CommandSuccess(outcome.command_slot_id, artifact_set_hash(evidence.artifacts), evidence.artifacts)
        committed = self._store.commit_reprocessed_vlm_success(request, success)
        return ReprocessVlmEvidenceResult(committed, read_reprocessed_vlm_evidence(self._store, request, committed))
