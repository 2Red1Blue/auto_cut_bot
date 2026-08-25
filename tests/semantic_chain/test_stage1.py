from __future__ import annotations

import json
from uuid import uuid4

import pytest
from autocut_kernel.semantic_chain import Stage1AuthorityError, compile_stage1
from autocut_kernel.semantic_chain.authority import _decode_stage1_authority_for_test
from autocut_kernel.source_manifest import (
    DecodedSeriesSource,
    SourceOperationGrant,
    SourceOperationPolicy,
)
from autocut_kernel.store import (
    ArtifactScope,
    BlobRef,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedVlmSemanticInput,
    Job,
    PersistedVlmGenerationChild,
    PersistedVlmSemanticPack,
    PersistedWholeSeriesSourceManifest,
    SourceWindowIdentity,
    VlmSemanticPackReference,
    WholeSeriesSourceManifestReference,
)
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm import parse_vlm_response

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _decoded_store_projection(*, purpose: bool = True) -> CommittedSemanticInputs:
    """Narrow Store-reader-shaped fixture using a real VLM parse/decode result."""

    from tests.vlm.test_parser import _context, _payload, _raw

    manifest, manifest_set, policy, request_identity = _context()
    semantic_pack = parse_vlm_response(_raw(_payload(manifest)), manifest=manifest, manifest_set=manifest_set, request_identity=request_identity, policy=policy)
    job = Job("stage1-test", "shadow")
    blob = BlobRef(uuid4(), request_identity.request_payload_sha256, 1, "application/json")
    source_manifest_payload = json.dumps({"source": "committed"})
    persisted_source = PersistedWholeSeriesSourceManifest(
        WholeSeriesSourceManifestReference(ArtifactScope("pipeline", "job", job.job_key), "whole_series_source_manifest", 1, canonical_payload_hash(source_manifest_payload)),
        source_manifest_payload, (blob,), uuid4(), uuid4(), uuid4(), uuid4(), job,
    )
    grant = SourceOperationGrant(
        SourceOperationPolicy("stage1-authority", "stage1-series", 1, ("semantic_analysis",) if purpose else ("render_source",)),
        "all_or_nothing", (DecodedSeriesSource("episode-001.mp4", request_identity.source_id, request_identity.source_sha256, 1),),
    )
    source_window = SourceWindowIdentity(0, 0, 0, 10, request_identity.window_manifest_sha256, request_identity.source_id, request_identity.source_sha256, request_identity.source_clock_id, request_identity.window_manifest_set_sha256, blob)
    response = CommittedArtifactMemberReference(uuid4(), uuid4(), 1, ArtifactScope("pipeline", "job", job.job_key), "vlm_response_record", "response", 1, semantic_pack.raw_response_sha256)
    child = object.__new__(PersistedVlmGenerationChild)
    object.__setattr__(child, "window_manifest_sha256", request_identity.window_manifest_sha256)
    object.__setattr__(child, "request_identity_sha256", request_identity.canonical_hash)
    persisted_pack = object.__new__(PersistedVlmSemanticPack)
    object.__setattr__(persisted_pack, "semantic_pack", semantic_pack)
    object.__setattr__(persisted_pack, "source_child", child)
    object.__setattr__(persisted_pack, "reference", VlmSemanticPackReference(ArtifactScope("pipeline", "job", job.job_key), f"semantic_pack_{request_identity.window_manifest_sha256[7:39]}", 1, canonical_payload_hash(json.dumps(semantic_pack.to_mapping(), sort_keys=True))))
    return CommittedSemanticInputs(persisted_source, grant, (CommittedVlmSemanticInput(source_window, request_identity, persisted_pack, response, blob),))


def _authority(inputs: CommittedSemanticInputs, *, window: str = "resolved", obligation: str = "resolved", purpose_hash: str = HASH_A):
    return _decode_stage1_authority_for_test(inputs, audit_record_sha256=HASH_B, policy_id="strict-global-v1", policy_sha256=purpose_hash, window_statuses=(window,), obligation_statuses=(obligation, obligation))


def test_stage1_is_deterministic_semantic_only_and_not_caller_minted() -> None:
    inputs = _decoded_store_projection()
    first = compile_stage1(inputs, _authority(inputs))
    second = compile_stage1(inputs, _authority(inputs))
    assert first.canonical_hash == second.canonical_hash
    assert first.decision == "admitted"
    rendered = json.dumps(first.to_mapping()).casefold()
    assert all(value not in rendered for value in ("highlight", "asr", "vad", "pts", "cut", "mode"))
    assert "CoverageAdmission" not in __import__("autocut_kernel.semantic_chain", fromlist=["*"]).__all__


@pytest.mark.parametrize("status", ["tainted", "unresolved", "conflicted"])
def test_non_resolved_window_or_obligation_denies(status: str) -> None:
    inputs = _decoded_store_projection()
    assert compile_stage1(inputs, _authority(inputs, window=status)).decision == "denied"
    assert compile_stage1(inputs, _authority(inputs, obligation=status)).decision == "denied"


def test_missing_semantic_analysis_and_invalid_authority_are_denied_or_rejected() -> None:
    denied_inputs = _decoded_store_projection(purpose=False)
    assert compile_stage1(denied_inputs, _authority(denied_inputs)).decision == "denied"
    with pytest.raises(Stage1AuthorityError):
        compile_stage1({}, _authority(_decoded_store_projection()))  # type: ignore[arg-type]


def test_decoder_projection_requires_exact_universe_and_strict_hashes() -> None:
    inputs = _decoded_store_projection()
    with pytest.raises(ValueError):
        _decode_stage1_authority_for_test(inputs, audit_record_sha256="sha256:" + "Z" * 64, policy_id="strict-global-v1", policy_sha256=HASH_A, window_statuses=("resolved",), obligation_statuses=("resolved", "resolved"))
    with pytest.raises(ValueError):
        _decode_stage1_authority_for_test(inputs, audit_record_sha256=HASH_A, policy_id="strict-global-v1", policy_sha256=HASH_B, window_statuses=(), obligation_statuses=("resolved", "resolved"))
