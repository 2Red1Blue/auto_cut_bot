"""Stage 1 reads V4 core observations without retyping them as V3."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.continuity_analysis import analyze_continuity
from autocut_kernel.semantic_chain.core_observations import (
    observation_confidence,
    observation_source_interval,
    semantic_pack,
)
from autocut_kernel.semantic_chain.narrative_projection import project_narrative
from autocut_kernel.semantic_chain.stage1_draft import (
    decode_stage1_draft,
    stage1_draft_prompt_inputs,
)
from autocut_kernel.store import (
    BlobRef,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedVlmSemanticInput,
    PersistedVlmGenerationChild,
    PersistedVlmSemanticPack,
    PersistedVlmSemanticPackV4,
    SourceWindowIdentity,
    StoreValidationError,
    VlmRequestRecordReference,
    VlmSemanticPackReference,
)
from autocut_kernel.store.models import (
    VLM_BATCH_FINALIZER_STRATEGY_VERSION,
    VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
    canonical_payload_hash,
)
from autocut_kernel.vlm.semantic_pack_v4 import VlmSemanticPackV4

from tests.semantic_chain.test_stage1_draft import POLICY, _blob, _synthetic_inputs
from tests.vlm.test_semantic_pack_v4 import _raw, _v4_context, _wire


def _v4_inputs() -> CommittedSemanticInputs:
    base = _synthetic_inputs()
    source, grant, aggregate = base.source_manifest, base.source_grant, base.vlm_semantic_pack_set
    manifest, manifest_set, parse_policy, identity = _v4_context()
    raw = _raw(_wire())
    from autocut_kernel.vlm.semantic_parser_v4 import parse_vlm_response_v4

    pack = parse_vlm_response_v4(
        raw,
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=parse_policy,
    )
    proxy = base.inputs[0].source_window.proxy_blob
    request_blob = BlobRef(UUID(int=901), identity.request_payload_sha256, 20, "application/json")
    raw_blob = BlobRef(UUID(int=902), pack.raw_response_sha256, len(raw), "application/json")
    attempt, receipt, artifact_set, slot = (UUID(int=value) for value in range(903, 907))
    request = {
        "attempt_id": str(attempt),
        "episode_index": 0,
        "idempotency_key": "request-v4",
        "provider_idempotency_key": "provider-v4",
        "proxy_blob": _blob(proxy),
        "request_hash": "sha256:" + "a" * 64,
        "request_identity": identity.to_mapping(),
        "request_identity_sha256": identity.canonical_hash,
        "request_payload_blob": _blob(request_blob),
        "source_manifest_sha256": source.reference.content_hash,
        "source_provenance_sha256": source.canonical_hash,
        "window_manifest_set_sha256": identity.window_manifest_set_sha256,
        "window_manifest_sha256": identity.window_manifest_sha256,
    }
    request_json = json.dumps(request)
    child = PersistedVlmGenerationChild(
        VlmRequestRecordReference(
            source.reference.scope,
            f"vlm_request_{identity.window_manifest_sha256[7:31]}",
            1,
            canonical_payload_hash(request_json),
        ),
        request_json,
        source.source_job,
        source.job_id,
        slot,
        "request-v4",
        "sha256:" + "a" * 64,
        attempt,
        "provider-v4",
        request_blob,
        receipt,
        artifact_set,
        0,
        identity.window_manifest_sha256,
        identity.window_manifest_set_sha256,
        source.reference.content_hash,
        source.canonical_hash,
        identity.canonical_hash,
        "strict-semantic-pack-v4",
        4,
    )
    pack_json = json.dumps(pack.to_mapping())
    persisted = PersistedVlmSemanticPackV4(
        VlmSemanticPackReference(
            source.reference.scope,
            f"semantic_pack_{identity.window_manifest_sha256[7:39]}",
            1,
            canonical_payload_hash(pack_json),
        ),
        pack_json,
        pack,
        child,
    )
    response_json = json.dumps(
        {
            "attempt_id": str(attempt),
            "provider_request_id": "response-v4",
            "raw_response_blob": _blob(raw_blob),
            "raw_response_sha256": pack.raw_response_sha256,
        }
    )
    response = CommittedArtifactMemberReference(
        receipt,
        artifact_set,
        1,
        source.reference.scope,
        "vlm_response_record",
        f"vlm_response_{identity.window_manifest_sha256[7:31]}",
        1,
        canonical_payload_hash(response_json),
    )
    window = SourceWindowIdentity(
        0,
        manifest.stream_index,
        manifest.core_range.start_pts,
        manifest.core_range.end_pts,
        identity.window_manifest_sha256,
        identity.source_id,
        identity.source_sha256,
        identity.source_clock_id,
        identity.window_manifest_set_sha256,
        proxy,
    )
    member = CommittedVlmSemanticInput(window, identity, persisted, response, raw_blob)
    return CommittedSemanticInputs(
        source,
        grant,
        aggregate,
        child.request_policy,
        (member,),
        VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
    )


def _draft(inputs: CommittedSemanticInputs) -> dict[str, object]:
    pack = semantic_pack(inputs.inputs[0])
    binding = stage1_draft_prompt_inputs(inputs, policy=POLICY)["input_binding_sha256"]
    event_ref = {
        "window_manifest_sha256": pack.window_manifest_sha256,
        "object_type": "event",
        "object_id": pack.events[0].event_id,
    }
    fact_ref = {
        "window_manifest_sha256": pack.window_manifest_sha256,
        "object_type": "fact",
        "object_id": pack.facts[0].fact_id,
    }
    return {
        "schema_version": "stage1-cross-window-draft-v1",
        "input_binding_sha256": binding,
        "beats": [
            {
                "beat_id": "beat_1",
                "summary": "A discovery.",
                "phase": "reveal",
                "event_refs": [event_ref],
                "obligation_ids": ["obligation_1"],
            }
        ],
        "obligations": [
            {
                "obligation_id": "obligation_1",
                "description": "Explain the discovery.",
                "required_fact_refs": [fact_ref],
                "success_criteria": "Retain the visible action.",
            }
        ],
        "story_threads": [
            {
                "story_thread_id": "thread_1",
                "title": "Discovery",
                "premise": "Visible evidence changes the situation.",
                "obligation_ids": ["obligation_1"],
            }
        ],
        "merge_proposals": [],
    }


def test_v4_core_observations_flow_through_draft_projection_and_continuity() -> None:
    inputs = _v4_inputs()
    item = inputs.inputs[0]
    pack = semantic_pack(item)
    assert type(item.semantic_pack) is PersistedVlmSemanticPackV4
    assert type(pack) is VlmSemanticPackV4
    assert observation_confidence(pack.events[0]) == pack.events[0].support.confidence
    assert observation_source_interval(pack.events[0]) == pack.events[0].support.source_interval

    raw_draft = canonical_json_bytes(_draft(inputs))
    draft = decode_stage1_draft(raw_draft, inputs=inputs, policy=POLICY)
    projected = project_narrative(
        inputs,
        draft,
        scope=inputs.source_manifest.reference.scope,
        revision=1,
    )
    assert projected.event_cards.artifact_type == "event_card_set"
    assert projected.narrative_graph.artifact_type == "narrative_graph"
    assert analyze_continuity(inputs, policy=POLICY)


def test_v4_is_not_constructed_as_v3_and_aggregate_version_mixing_fails() -> None:
    inputs = _v4_inputs()
    persisted = inputs.inputs[0].semantic_pack
    assert type(persisted) is PersistedVlmSemanticPackV4
    with pytest.raises(StoreValidationError, match="value is invalid"):
        PersistedVlmSemanticPack(
            persisted.reference,
            persisted.payload_json,
            persisted.semantic_pack,  # type: ignore[arg-type]
            persisted.source_child,
        )
    with pytest.raises(StoreValidationError, match="cannot mix child parser/schema"):
        replace(inputs, vlm_batch_strategy_version=VLM_BATCH_FINALIZER_STRATEGY_VERSION)
