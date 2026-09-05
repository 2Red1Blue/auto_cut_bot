"""Pure content joins for explicit derived evidence; never a Store authority."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from ..contracts.compiler.canonical import canonical_json_hash, load_canonical_json_bytes
from ..media.types import canonical_sha256, sha256_prefixed
from ..store.models import (
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedVlmSemanticInput,
    PersistedReprocessedVlmChild,
    PersistedVlmSemanticPackV4,
    canonical_payload_hash,
)


def derived_record(payload_json: str) -> dict[str, object]:
    """Read a versioned provenance record, checking its local hash joins only."""
    value = load_canonical_json_bytes(payload_json.encode(), origin="derived VLM evidence")[0]
    if type(value) is not dict:
        raise ValueError("derived evidence must be an object")
    record = cast(dict[str, object], value)
    version = record.get("schema_version")
    keys = {"schema_version", "request", "parent_parser_strategy", "parent_parser_contract_sha256",
            "parent_provider_request_id", "source_manifest_sha256", "source_provenance_sha256",
            "window_manifest_sha256", "window_manifest_set_sha256", "normalization", "semantic_pack"}
    if version == "reprocessed-vlm-evidence-v2":
        keys.update(("request_identity", "parse_policy", "proxy_blob"))
    elif version != "reprocessed-vlm-evidence-v1":
        raise ValueError("unsupported derived evidence version")
    if set(record) != keys or type(record["request"]) is not dict or type(record["normalization"]) is not dict:
        raise ValueError("derived evidence has missing or unknown fields")
    request = cast(dict[str, object], record["request"])
    normalization = cast(dict[str, object], record["normalization"])
    request_keys = {"strategy_version", "target_parser_strategy", "target_parser_contract_sha256", "job",
                    "parent_command_slot_id", "parent_receipt_id", "parent_attempt_id", "parent_request_hash",
                    "parent_request_payload_sha256", "parent_raw_response_sha256", "source_artifact_set_id",
                    "episode_index", "parent_artifact_revision", "provider_call_budget"}
    if version == "reprocessed-vlm-evidence-v2":
        request_keys.add("projection_version")
        if type(request.get("projection_version")) is not int or request["projection_version"] != 2:
            raise ValueError("derived projection version is invalid")
    if set(request) != request_keys:
        raise ValueError("derived request has missing or unknown fields")
    for name in ("parent_command_slot_id", "parent_receipt_id", "parent_attempt_id", "source_artifact_set_id"):
        value = request[name]
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError("derived parent identity is invalid")
    for name in ("target_parser_contract_sha256", "parent_request_hash", "parent_request_payload_sha256", "parent_raw_response_sha256"):
        sha256_prefixed(request[name], name)
    for name in ("source_manifest_sha256", "source_provenance_sha256", "window_manifest_sha256", "window_manifest_set_sha256",
                 "parent_parser_contract_sha256"):
        sha256_prefixed(record[name], name)
    for name, minimum in (("episode_index", 0), ("parent_artifact_revision", 1)):
        if type(request[name]) is not int or request[name] < minimum:
            raise ValueError("derived request ordinal is invalid")
    job = request["job"]
    if (type(job) is not dict or set(job) != {"job_key", "profile"}
            or any(type(value) is not str or not value for value in job.values())):
        raise ValueError("derived job identity is invalid")
    normalization_keys = {"strategy_version", "implementation_sha256", "raw_response_sha256", "normalized_response_sha256",
                          "normalized_response", "transformations"}
    if (set(normalization) != normalization_keys
            or canonical_json_hash(normalization["normalized_response"]) != normalization["normalized_response_sha256"]):
        raise ValueError("derived normalization content is not closed")
    expected_strategy = str(version).replace("reprocessed-", "reprocess-", 1)
    if (request.get("strategy_version") != expected_strategy
            or request.get("target_parser_strategy") != "normalized-semantic-pack-v4-v1"
            or type(request.get("provider_call_budget")) is not int or request["provider_call_budget"] != 0
            or request.get("parent_raw_response_sha256") != normalization.get("raw_response_sha256")):
        raise ValueError("derived evidence parent/normalization identities do not close")
    return record


def bind_derived_input(
    item: CommittedVlmSemanticInput, inputs: CommittedSemanticInputs,
) -> tuple[CommittedArtifactMemberReference, dict[str, object]]:
    """Bind the actual derivation owner and pack, without inventing generation."""
    persisted, identity, source = item.semantic_pack, item.request_identity, item.source_window
    child = persisted.source_child
    if type(persisted) is not PersistedVlmSemanticPackV4 or type(child) is not PersistedReprocessedVlmChild:
        raise ValueError("derived input requires exact derived V4 values")
    record = derived_record(child.payload_json)
    request = cast(dict[str, object], record["request"])
    pack = persisted.semantic_pack
    if (canonical_payload_hash(child.payload_json) != child.reference.content_hash
            or canonical_sha256(request) != child.request_hash
            or item.response_record != child.reference
            or child.reference.member_ordinal != 0 or child.reference.artifact_type != "reprocessed_vlm_evidence"
            or child.reference.logical_id != "reprocessed_vlm_" + child.request_hash[7:]
            or persisted.reference.logical_id != "reprocessed_semantic_pack_" + child.request_hash[7:]
            or persisted.reference.scope != child.reference.scope
            or persisted.reference.revision != child.reference.revision
            or canonical_payload_hash(persisted.payload_json) != persisted.reference.content_hash
            or record["semantic_pack"] != pack.to_mapping()
            or request.get("parent_raw_response_sha256") != item.raw_response.content_hash
            or item.raw_response.content_hash != pack.raw_response_sha256
            or request.get("parent_attempt_id") != str(child.parent_attempt_id)
            or request.get("parent_receipt_id") != str(child.parent_receipt_id)
            or request.get("parent_request_payload_sha256") != identity.request_payload_sha256
            or request.get("episode_index") != source.episode_index
            or identity != child.request_identity
            or pack.request_identity_sha256 != identity.canonical_hash
            or source.window_manifest_sha256 != identity.window_manifest_sha256
            or pack.window_manifest_sha256 != identity.window_manifest_sha256
            or source.window_manifest_set_sha256 != identity.window_manifest_set_sha256
            or source.source_id != identity.source_id or source.source_sha256 != identity.source_sha256
            or source.source_clock_id != identity.source_clock_id
            or record["window_manifest_sha256"] != identity.window_manifest_sha256
            or record["window_manifest_set_sha256"] != identity.window_manifest_set_sha256
            or record["source_manifest_sha256"] != inputs.source_manifest.reference.content_hash
            or record["source_provenance_sha256"] != inputs.source_manifest.canonical_hash
            or child.source_job != inputs.source_manifest.source_job
            or child.request_policy != inputs.vlm_aggregate_policy
            or child.reference.scope != inputs.source_manifest.reference.scope
            or inputs.vlm_semantic_pack_set.scope != child.reference.scope):
        raise ValueError("derived VLM input provenance is not closed")
    pack_ref = CommittedArtifactMemberReference(
        child.receipt_id, child.artifact_set_id, 1, persisted.reference.scope, "vlm_semantic_pack",
        persisted.reference.logical_id, persisted.reference.revision, persisted.reference.content_hash,
    )
    return pack_ref, record
