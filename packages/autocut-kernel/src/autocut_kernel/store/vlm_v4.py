"""Versioned VLM persistence checks; v3 records and consumers stay separate."""

from __future__ import annotations

import hashlib
import json
from typing import cast

from ..context_pack import WindowContextPack
from ..media.types import canonical_sha256
from ..source_manifest import decode_source_manifest
from ..vlm.models import VlmParsePolicy, VlmRequestIdentity
from ..vlm.retry_policy import GenerationRetryPolicy
from ..vlm.semantic_contracts import VLM_PARSER_V3, VLM_PARSER_V4, parser_contract_sha256_for
from ..vlm.semantic_parser_v4 import decode_vlm_semantic_pack_v4, parse_vlm_response_v4
from .errors import StoreValidationError
from .models import (
    VLM_BATCH_FINALIZER_STRATEGY_VERSION,
    VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
    ArtifactMember,
    PersistedVlmGenerationChild,
    PersistedVlmSemanticPackV4,
    PersistedWholeSeriesSourceManifest,
    VlmSemanticPackReference,
    canonical_payload_hash,
)


def batch_version_fields(strategy: str) -> dict[str, object]:
    if strategy == VLM_BATCH_FINALIZER_STRATEGY_VERSION:
        return {}
    if strategy == VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4:
        return {"schema_version": 4, "parser_strategy_version": VLM_PARSER_V4}
    raise StoreValidationError("VLM batch finalizer strategy is not registered")


def require_batch_child_version(strategy: str, parser: str, schema: int) -> None:
    batch_version_fields(strategy)
    expected = (
        (VLM_PARSER_V4, 4) if strategy == VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4
        else (VLM_PARSER_V3, 3)
    )
    if (parser, schema) != expected or type(schema) is not int:  # noqa: E721
        raise StoreValidationError("VLM batch strategy cannot mix parser or semantic schema versions")


def generation_semantic_version(
    request_payload: dict[str, object], pack_payload: dict[str, object],
) -> tuple[str, int]:
    parser = request_payload.get("parser_strategy_version")
    schema = pack_payload.get("schema_version")
    if type(schema) is not int or (parser, schema) not in (  # noqa: E721
        (VLM_PARSER_V3, 3), (VLM_PARSER_V4, 4),
    ):
        raise StoreValidationError("VLM frozen parser and semantic pack schema disagree")
    if parser == VLM_PARSER_V4:
        response_schema = request_payload.get("response_schema")
        properties = cast(dict[str, object], response_schema).get("properties") if type(response_schema) is dict else None
        version = cast(dict[str, object], properties).get("schema_version") if type(properties) is dict else None
        constant = cast(dict[str, object], version).get("const") if type(version) is dict else None
        if type(constant) is not int or constant != 4:  # noqa: E721
            raise StoreValidationError("V4 frozen request must declare response schema version four")
    return cast(str, parser), schema


def verify_v4_semantic_pack(
    *, child: PersistedVlmGenerationChild, artifact: ArtifactMember,
    request_record: dict[str, object], request_payload: dict[str, object],
    pack_payload: dict[str, object], raw_response: bytes,
    source: PersistedWholeSeriesSourceManifest,
) -> PersistedVlmSemanticPackV4:
    """Reconstruct from the exact committed Source owner and original provider bytes."""
    if generation_semantic_version(request_payload, pack_payload) != (VLM_PARSER_V4, 4):
        raise StoreValidationError("V4 verifier cannot accept a legacy semantic pack")
    base_fields = {
        "model_id", "parse_policy", "parser_strategy_version", "parser_contract_sha256", "prompt", "prompt_version",
        "provider_id", "proxy_blob", "request_parameters", "retry_policy", "retry_policy_sha256",
        "response_schema", "window_manifest_sha256", "window_manifest_set_sha256",
    }
    context_fields = {"context_pack", "context_pack_sha256"}
    payload_fields = set(request_payload)
    context_pack: WindowContextPack | None = None
    if payload_fields == base_fields:
        pass
    elif payload_fields == base_fields | context_fields:
        try:
            context_pack = WindowContextPack.from_mapping(request_payload["context_pack"])
        except (TypeError, ValueError) as error:
            raise StoreValidationError("V4 frozen WindowContextPack is invalid") from error
        if request_payload["context_pack_sha256"] != context_pack.canonical_hash:
            raise StoreValidationError("V4 frozen WindowContextPack hash is invalid")
    else:
        raise StoreValidationError("V4 provider request payload does not match its closed schema")
    if request_payload["parser_contract_sha256"] != parser_contract_sha256_for(VLM_PARSER_V4):
        raise StoreValidationError("V4 frozen parser contract does not match the installed implementation")
    retry_value = request_payload["retry_policy"]
    if type(retry_value) is not dict:  # noqa: E721
        raise StoreValidationError("V4 frozen retry policy must be an object")
    retry_mapping = cast(dict[str, object], retry_value)
    if set(retry_mapping) != {"strategy_version", "max_attempts", "backoff_seconds"} or type(retry_mapping["backoff_seconds"]) is not list:
        raise StoreValidationError("V4 frozen retry policy does not match its closed schema")
    retry = GenerationRetryPolicy(
        cast(str, retry_mapping["strategy_version"]), cast(int, retry_mapping["max_attempts"]),
        tuple(cast(list[int], retry_mapping["backoff_seconds"])),
    )
    prepared = decode_source_manifest(source.payload_json, source.proxy_blobs)
    prepared.census.require_purpose("semantic_analysis")
    if (
        source.reference.content_hash != child.source_manifest_sha256
        or source.canonical_hash != child.source_provenance_sha256
        or child.episode_index >= len(prepared.episodes)
    ):
        raise StoreValidationError("V4 child does not bind the committed Source owner")
    episode = prepared.episodes[child.episode_index]
    manifest, manifest_set = episode.manifest, episode.manifest_set
    identity_value = request_record.get("request_identity")
    policy_value = request_payload["parse_policy"]
    if type(identity_value) is not dict or type(policy_value) is not dict:  # noqa: E721
        raise StoreValidationError("V4 frozen request identity or parse policy is invalid")
    identity = VlmRequestIdentity(**cast(dict[str, str], identity_value))
    policy = VlmParsePolicy(**cast(dict[str, int], policy_value))
    prompt = request_payload["prompt"]
    if type(prompt) is not str:  # noqa: E721
        raise StoreValidationError("V4 frozen prompt must be text")
    expected_identity = VlmRequestIdentity.from_manifest(
        manifest, manifest_set,
        prompt_template_sha256="sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        prompt_version=cast(str, request_payload["prompt_version"]),
        response_schema_sha256=canonical_payload_hash(json.dumps(request_payload["response_schema"])),
        model_id=cast(str, request_payload["model_id"]),
        provider_id=cast(str, request_payload["provider_id"]),
        request_parameters_sha256=canonical_payload_hash(json.dumps(request_payload["request_parameters"])),
        request_payload_sha256=child.request_payload.content_hash,
        parse_policy=policy,
    )
    if (
        identity != expected_identity
        or identity.canonical_hash != child.request_identity_sha256
        or request_payload["window_manifest_sha256"] != manifest.canonical_hash
        or request_payload["window_manifest_set_sha256"] != manifest_set.canonical_hash
        or request_payload["proxy_blob"] != manifest.proxy_blob_ref.to_mapping()
        or request_record.get("proxy_blob") != manifest.proxy_blob_ref.to_mapping()
        or policy.to_mapping() != policy_value
    ):
        raise StoreValidationError("V4 frozen request differs from its Source/Window identity")
    expected_request_hash = canonical_sha256({
        "artifact_revision": artifact.revision,
        "episode_index": child.episode_index,
        "artifact_scope": {
            "namespace": artifact.scope.namespace, "kind": artifact.scope.kind, "key": artifact.scope.key,
        },
        "identity_sha256": identity.canonical_hash,
        "job": {"job_key": child.source_job.job_key, "profile": child.source_job.profile},
        "parser_strategy_version": VLM_PARSER_V4,
        "retry_policy_sha256": request_payload["retry_policy_sha256"],
        "proxy_blob": manifest.proxy_blob_ref.to_mapping(),
        "source_provenance_sha256": source.canonical_hash,
        "source_manifest_sha256": source.reference.content_hash,
        **(
            {"context_pack_sha256": context_pack.canonical_hash}
            if context_pack is not None
            else {}
        ),
    })
    if (
        expected_request_hash != child.request_hash
        or retry.canonical_hash != request_payload["retry_policy_sha256"]
    ):
        raise StoreValidationError("V4 command request hash does not bind its frozen payload")
    decoded = decode_vlm_semantic_pack_v4(
        pack_payload, manifest=manifest, manifest_set=manifest_set,
        request_identity=identity, policy=policy,
    )
    reparsed = parse_vlm_response_v4(
        raw_response, manifest=manifest, manifest_set=manifest_set,
        request_identity=identity, policy=policy,
    )
    if reparsed.canonical_hash != decoded.canonical_hash or reparsed.to_mapping() != decoded.to_mapping():
        raise StoreValidationError("V4 persisted pack does not match exact raw-response reparse")
    return PersistedVlmSemanticPackV4(
        VlmSemanticPackReference(artifact.scope, artifact.logical_id, artifact.revision, artifact.content_hash),
        artifact.payload_json, decoded, child,
    )
