"""Candidate projection over a real decoded SourceManifest, without a Store."""

from __future__ import annotations

import hashlib
from dataclasses import fields, replace
from fractions import Fraction
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.pipeline.build_narrative_graph_command import BuildNarrativeGraphCommand
from autocut_kernel.pipeline.build_narrative_graph_request import BuildNarrativeGraphRequest
from autocut_kernel.semantic_chain.candidate_catalog import CandidateCatalogPolicy
from autocut_kernel.semantic_chain.candidate_projection import (
    CandidateProjectionError,
    decode_candidate_source_context,
    project_candidate_catalog,
)
from autocut_kernel.semantic_chain.coverage_admission import CoverageAdmission
from autocut_kernel.semantic_chain.coverage_analysis import Stage1CoveragePolicy
from autocut_kernel.semantic_chain.coverage_compiler import compile_stage1_coverage
from autocut_kernel.semantic_chain.dependency_projection import DependencyProjectionPolicy
from autocut_kernel.semantic_chain.dependency_proof import build_dependency_proof
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.narrative_models import NarrativeGraph
from autocut_kernel.semantic_chain.stage1_checks import Stage1Check
from autocut_kernel.semantic_chain.stage1_command_policy import Stage1GenerationPolicy
from autocut_kernel.semantic_chain.stage1_draft import (
    STAGE1_DRAFT_SCHEMA_VERSION,
    Stage1DraftPolicy,
    stage1_draft_prompt_inputs,
)
from autocut_kernel.semantic_chain.stage1_evaluation import evaluate_stage1_business_members
from autocut_kernel.semantic_chain.stage1_members import decode_coverage_members
from autocut_kernel.semantic_chain.stage1_result import decode_stage1_members
from autocut_kernel.source_manifest import SourceOperationPolicy, decode_source_manifest
from autocut_kernel.store import (
    BlobRef,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    CommittedVlmSemanticInput,
    PersistedVlmGenerationChild,
    PersistedVlmSemanticPack,
    SourceWindowIdentity,
    VlmRequestRecordReference,
    VlmSemanticPackReference,
)
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm import VlmRequestIdentity, parse_vlm_response
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy

from tests.media.test_prepare_timed_media_evidence_command import _request, _Store
from tests.semantic_chain.test_build_narrative_graph_command import (
    MemoryNarrativeGraphStore,
    ScriptedDraftProvider,
)
from tests.semantic_chain.test_stage1_result import _member
from tests.vlm.test_parser import _context, _payload, _raw

POLICY = Stage1DraftPolicy(64_000, 64_000, 4, 32, 4, 4, 4, 4, 8, 256, 2048)
COVERAGE = Stage1CoveragePolicy("0.5", "strict_global")


def _inputs(*, command_ready: bool = False) -> CommittedSemanticInputs:
    store = _Store()
    _request(store)
    assert store.source_manifest is not None
    original = store.source_manifest
    decoded = decode_source_manifest(original.payload_json, original.proxy_blobs)
    grant = replace(
        decoded.census,
        policy=SourceOperationPolicy(
            decoded.census.policy.authorization_id,
            decoded.census.policy.series_id,
            decoded.census.policy.expected_source_count,
            ("semantic_analysis", "render_source"),
        ),
    )
    source_raw = canonical_json_bytes(replace(decoded, census=grant).to_mapping()).decode("utf-8")
    source = replace(
        original,
        reference=replace(original.reference, content_hash=canonical_payload_hash(source_raw)),
        payload_json=source_raw,
    )
    manifest = decode_source_manifest(source.payload_json, source.proxy_blobs).episodes[0].manifest
    manifest_set = decode_source_manifest(source.payload_json, source.proxy_blobs).episodes[0].manifest_set
    _unused_manifest, _unused_set, parse_policy, template = _context()
    identity = VlmRequestIdentity.from_manifest(
        manifest, manifest_set, prompt_template_sha256=template.prompt_template_sha256,
        prompt_version=template.prompt_version, response_schema_sha256=template.response_schema_sha256,
        model_id=template.model_id, provider_id=template.provider_id,
        request_parameters_sha256=template.request_parameters_sha256,
        request_payload_sha256=template.request_payload_sha256, parse_policy=parse_policy,
    )
    response_payload = _payload(manifest)
    _bind_payload_supports_to_manifest(response_payload, manifest)
    if command_ready:
        _make_payload_command_ready(response_payload)
    raw = _raw(response_payload)
    pack = parse_vlm_response(raw, manifest=manifest, manifest_set=manifest_set, request_identity=identity, policy=parse_policy)
    scope = source.reference.scope
    request_blob = BlobRef(UUID(int=101), identity.request_payload_sha256, 20, "application/json")
    raw_blob = BlobRef(UUID(int=102), pack.raw_response_sha256, len(raw), "application/json")
    request_payload = {
        "attempt_id": str(UUID(int=103)), "episode_index": 0, "idempotency_key": "candidate-test",
        "provider_idempotency_key": "candidate-test-provider", "proxy_blob": {
            "object_id": str(source.proxy_blobs[0].object_id), "content_hash": source.proxy_blobs[0].content_hash,
            "byte_length": source.proxy_blobs[0].byte_length, "media_type": source.proxy_blobs[0].media_type,
        },
        "request_hash": "sha256:" + "1" * 64, "request_identity": identity.to_mapping(),
        "request_identity_sha256": identity.canonical_hash, "request_payload_blob": {
            "object_id": str(request_blob.object_id), "content_hash": request_blob.content_hash,
            "byte_length": request_blob.byte_length, "media_type": request_blob.media_type,
        },
        "source_manifest_sha256": source.reference.content_hash, "source_provenance_sha256": source.canonical_hash,
        "window_manifest_set_sha256": identity.window_manifest_set_sha256,
        "window_manifest_sha256": identity.window_manifest_sha256,
    }
    request_json = canonical_json_bytes(request_payload).decode("utf-8")
    child = PersistedVlmGenerationChild(
        VlmRequestRecordReference(scope, f"vlm_request_{identity.window_manifest_sha256[7:31]}", 1, canonical_payload_hash(request_json)),
        request_json, source.source_job, source.job_id, UUID(int=104), "candidate-test", "sha256:" + "1" * 64,
        UUID(int=103), "candidate-test-provider", request_blob, UUID(int=105), UUID(int=106), 0,
        identity.window_manifest_sha256, identity.window_manifest_set_sha256, source.reference.content_hash,
        source.canonical_hash, identity.canonical_hash,
    )
    pack_json = canonical_json_bytes(pack.to_mapping()).decode("utf-8")
    persisted = PersistedVlmSemanticPack(
        VlmSemanticPackReference(scope, f"semantic_pack_{identity.window_manifest_sha256[7:39]}", 1, canonical_payload_hash(pack_json)),
        pack_json, pack, child,
    )
    response = CommittedArtifactMemberReference(
        UUID(int=105), UUID(int=106), 1, scope, "vlm_response_record",
        f"vlm_response_{identity.window_manifest_sha256[7:31]}", 1, "sha256:" + "2" * 64,
    )
    window = SourceWindowIdentity(
        0, manifest.stream_index, manifest.core_range.start_pts, manifest.core_range.end_pts,
        identity.window_manifest_sha256, manifest.source_id, manifest.source_sha256, manifest.source_clock_id,
        identity.window_manifest_set_sha256, source.proxy_blobs[0],
    )
    aggregate = CommittedArtifactMemberReference(
        UUID(int=107), UUID(int=108), 0, scope, "vlm_semantic_pack_set", "vlm_semantic_pack_set", 1, "sha256:" + "3" * 64,
    )
    return CommittedSemanticInputs(source, grant, aggregate, child.request_policy, (CommittedVlmSemanticInput(window, identity, persisted, response, raw_blob),))


def _make_payload_command_ready(payload: object) -> None:
    """Keep parser-derived values but make a complete one-window Stage 1 case."""
    assert type(payload) is dict
    continuity = payload["continuity"]
    assert type(continuity) is dict
    continuity["ends_mid_event"] = False
    continuity["continues_into_next"] = False
    continuity["exit_state_fact_refs"] = []

    def visit(value: object) -> None:
        if type(value) is dict:
            if "confidence" in value:
                value["confidence"] = "0.9"
            for nested in value.values():
                visit(nested)
        elif type(value) is list:
            for nested in value:
                visit(nested)

    visit(payload)


def _bind_payload_supports_to_manifest(payload: object, manifest: object) -> None:
    """Retarget parser-fixture support to the committed manifest's sampled frame."""
    assert type(payload) is dict
    assert hasattr(manifest, "frame_samples")
    frame = manifest.frame_samples[1]

    def visit(value: object) -> None:
        if type(value) is dict:
            if {"proxy_interval", "supporting_frame_ids"} <= set(value):
                value["proxy_interval"] = {
                    "start_pts": frame.proxy_pts - 1,
                    "end_pts": frame.proxy_pts + 1,
                    "uncertainty_pts": 0,
                }
                value["supporting_frame_ids"] = [frame.frame_id]
            for nested in value.values():
                visit(nested)
        elif type(value) is list:
            for nested in value:
                visit(nested)

    visit(payload)


def _draft_raw(inputs: CommittedSemanticInputs) -> bytes:
    pack = inputs.inputs[0].semantic_pack.semantic_pack
    prompt = stage1_draft_prompt_inputs(inputs, policy=POLICY)
    payload = {
        "schema_version": STAGE1_DRAFT_SCHEMA_VERSION, "input_binding_sha256": prompt["input_binding_sha256"],
        "beats": [{"beat_id": "beat_1", "summary": "Reveal", "phase": "reveal", "event_refs": [{"window_manifest_sha256": pack.window_manifest_sha256, "object_type": "event", "object_id": pack.events[0].event_id}], "obligation_ids": ["obligation_1"]}],
        "obligations": [{"obligation_id": "obligation_1", "description": "Keep reveal", "required_fact_refs": [{"window_manifest_sha256": pack.window_manifest_sha256, "object_type": "fact", "object_id": pack.facts[0].fact_id}], "success_criteria": "Visible reveal"}],
        "story_threads": [{"story_thread_id": "thread_1", "title": "Reveal", "premise": "A reveal", "obligation_ids": ["obligation_1"]}],
        "merge_proposals": [],
    }
    return canonical_json_bytes(payload)


def _stage1(inputs: CommittedSemanticInputs):
    raw = _draft_raw(inputs)
    compilation = compile_stage1_coverage(inputs, raw, draft_policy=POLICY, coverage_policy=COVERAGE, scope=inputs.source_manifest.reference.scope, revision=1)
    dependency_policy = DependencyProjectionPolicy("semantic-dependencies-v1")
    proof = build_dependency_proof(inputs, graph_member=compilation.narrative.narrative_graph, event_card_member=compilation.narrative.event_cards, ledger_member=compilation.coverage_ledger, policy=dependency_policy, revision=1)
    business = (*compilation.members, proof)
    checks = evaluate_stage1_business_members(inputs, raw, members=business, draft_policy=POLICY, coverage_policy=COVERAGE, dependency_policy=dependency_policy)
    coverage = decode_coverage_members(compilation.members, scope=inputs.source_manifest.reference.scope)
    admission = CoverageAdmission("admission-v1", coverage.coverage_ledger.input_binding_sha256, "sha256:" + hashlib.sha256(raw).hexdigest(), coverage.coverage_ledger.draft_sha256, POLICY.canonical_hash, COVERAGE.canonical_hash, dependency_policy.canonical_hash, "strict_global", "stage1-kc-v1", tuple(SemanticMemberIdentity.from_artifact_member(item) for item in business), (*checks, Stage1Check("KC-IN-001", "indeterminate", ("store_read_not_performed",))))
    members = (*business, _member("coverage_admission", inputs.source_manifest.reference.scope, admission.to_mapping()))
    return decode_stage1_members(members, scope=inputs.source_manifest.reference.scope)


def _command_request(inputs: CommittedSemanticInputs) -> BuildNarrativeGraphRequest:
    source = inputs.source_manifest
    source_reference = CommittedArtifactMemberReference(
        source.receipt_id,
        source.artifact_set_id,
        0,
        source.reference.scope,
        source.reference.artifact_type,
        source.reference.logical_id,
        source.reference.revision,
        source.reference.content_hash,
    )
    return BuildNarrativeGraphRequest(
        CommittedSemanticInputsRequest(source.source_job, source_reference, inputs.vlm_semantic_pack_set),
        "candidate-projection:stage1",
        1,
        Stage1GenerationPolicy(
            "doubao-ark-text-responses-stream",
            "test-model",
            "stage1-v1",
            "Build the graph.",
            "doubao-ark-text-responses-stream-v1",
            1024,
            "0.5",
        ),
        POLICY,
        COVERAGE,
        DependencyProjectionPolicy("semantic-dependencies-v1"),
        GenerationRetryPolicy("generation-retry-v1", 2, (0,)),
    )


def _committed_stage1_case() -> tuple[CommittedSemanticInputs, object]:
    """Return real decoded input plus the public-Command committed Stage 1 result."""
    inputs = _inputs(command_ready=True)
    store = MemoryNarrativeGraphStore(inputs)
    provider = ScriptedDraftProvider(_draft_raw(inputs))
    result = BuildNarrativeGraphCommand(store, provider).execute(_command_request(inputs))
    assert result.outcome.state == "succeeded"
    assert result.committed is not None
    assert result.committed.values.admission.next_action == "continue"
    return inputs, result.committed.values


def test_projection_joins_real_decoded_source_vlm_and_stage1_values():
    inputs = _inputs()
    result = project_candidate_catalog(
        inputs,
        _stage1(inputs),
        scope=inputs.source_manifest.reference.scope,
        revision=2,
        policy=CandidateCatalogPolicy("candidate-catalog-v1", "0.5", ("hook_strength",)),
    )
    candidate = result.catalog.candidates[0]
    raw_candidate = inputs.inputs[0].semantic_pack.semantic_pack.candidate_hypotheses[0]
    assert candidate.editing_modes == ("dialogue", "action")
    assert candidate.narrative_functions == ("hook", "reveal", "payoff")
    assert candidate.tags == ("dialogue", "emotion", "reveal")
    assert candidate.source_window_ref.object_id == inputs.inputs[0].source_window.window_manifest_sha256
    assert candidate.candidate_ref == SemanticObjectRef(
        SemanticMemberIdentity(
            inputs.inputs[0].semantic_pack.reference.artifact_type,
            inputs.inputs[0].semantic_pack.reference.logical_id,
            inputs.inputs[0].semantic_pack.reference.revision,
            inputs.inputs[0].semantic_pack.reference.scope,
            inputs.inputs[0].semantic_pack.reference.content_hash,
        ),
        "vlm_candidate",
        raw_candidate.candidate_id,
    )
    assert candidate.support.proxy_interval == raw_candidate.support.proxy_interval
    assert candidate.support.source_interval == raw_candidate.support.source_interval
    assert candidate.support.conservative_duration.fraction == Fraction(1, 45_000)
    assert candidate.measurements[0].value == "0.9"
    assert candidate.measurements[0].confidence == "0.06"


def test_actual_command_fixture_is_a_continue_admission_with_real_coarse_support():
    inputs, values = _committed_stage1_case()
    result = project_candidate_catalog(
        inputs,
        values,
        scope=inputs.source_manifest.reference.scope,
        revision=2,
        policy=CandidateCatalogPolicy("candidate-catalog-v1", "0.01", ("reveal_strength",)),
    )
    assert values.admission.next_action == "continue"
    assert result.catalog.candidates[0].support.conservative_duration.fraction == Fraction(1, 45_000)


def test_projection_denies_missing_render_source_before_source_decode():
    inputs = _inputs()
    denied = replace(inputs, source_grant=replace(inputs.source_grant, policy=replace(inputs.source_grant.policy, authorized_purposes=("semantic_analysis",))))
    with pytest.raises(CandidateProjectionError, match="render_source"):
        project_candidate_catalog(denied, _stage1(inputs), scope=inputs.source_manifest.reference.scope, revision=1, policy=CandidateCatalogPolicy("candidate-catalog-v1", "0.5", ()))


def test_source_context_rejects_a_grant_that_is_not_the_decoded_census():
    inputs = _inputs()
    wrong_source = replace(inputs.source_grant.sources[0], content_sha256="sha256:" + "f" * 64)
    supplied = replace(inputs, source_grant=replace(inputs.source_grant, sources=(wrong_source,)))
    with pytest.raises(CandidateProjectionError, match="census"):
        decode_candidate_source_context(supplied)


def test_source_context_rejects_a_tampered_source_payload_reference_before_decode():
    inputs = _inputs()
    source = inputs.source_manifest
    # Simulate an untrusted Store return after DTO construction. The public
    # decoder must still bind bytes to the exact reference before consuming it.
    tampered = object.__new__(type(source))
    for field in fields(source):
        object.__setattr__(tampered, field.name, getattr(source, field.name))
    object.__setattr__(
        tampered,
        "reference",
        replace(source.reference, content_hash="sha256:" + "f" * 64),
    )
    with pytest.raises(CandidateProjectionError, match="payload hash"):
        decode_candidate_source_context(replace(inputs, source_manifest=tampered))


def test_projection_rejects_a_foreign_output_scope():
    inputs = _inputs()
    foreign_scope = replace(inputs.source_manifest.reference.scope, key="foreign-job")
    with pytest.raises(CandidateProjectionError, match="output scope"):
        project_candidate_catalog(
            inputs,
            _stage1(inputs),
            scope=foreign_scope,
            revision=1,
            policy=CandidateCatalogPolicy("candidate-catalog-v1", "0.01", ()),
        )


def test_source_context_rejects_a_window_clock_substitution():
    inputs = _inputs()
    supplied_window = replace(inputs.inputs[0].source_window, source_clock_id="foreign-clock")
    supplied = replace(inputs, inputs=(replace(inputs.inputs[0], source_window=supplied_window),))
    with pytest.raises(CandidateProjectionError, match="SourceWindow"):
        decode_candidate_source_context(supplied)


@pytest.mark.parametrize("field", ("manifest_set", "proxy_blob", "episode_index"))
def test_source_context_rejects_each_remaining_window_binding_substitution(field: str):
    inputs = _inputs()
    original = inputs.inputs[0].source_window
    replacement = {
        "manifest_set": replace(
            original,
            window_manifest_set_sha256="sha256:" + "f" * 64,
        ),
        "proxy_blob": replace(original, proxy_blob=replace(original.proxy_blob, object_id=UUID(int=999))),
        "episode_index": replace(original, episode_index=1),
    }[field]
    supplied = replace(inputs, inputs=(replace(inputs.inputs[0], source_window=replacement),))
    with pytest.raises(CandidateProjectionError, match="SourceWindow"):
        decode_candidate_source_context(supplied)


def test_projection_rejects_source_not_in_the_exact_render_grant():
    inputs = _inputs()
    wrong_source = replace(inputs.source_grant.sources[0], content_sha256="sha256:" + "f" * 64)
    supplied = replace(inputs, source_grant=replace(inputs.source_grant, sources=(wrong_source,)))
    with pytest.raises(CandidateProjectionError, match="exact grant"):
        project_candidate_catalog(
            supplied,
            _stage1(inputs),
            scope=inputs.source_manifest.reference.scope,
            revision=1,
            policy=CandidateCatalogPolicy("candidate-catalog-v1", "0.01", ()),
        )


def test_projection_rejects_ledger_omission_of_a_raw_window_fact():
    inputs = _inputs()
    stage1 = _stage1(inputs)
    ledger = stage1.coverage.coverage_ledger
    truncated = replace(ledger.windows[0], fact_refs=())
    values = replace(
        stage1,
        coverage=replace(stage1.coverage, coverage_ledger=replace(ledger, windows=(truncated,))),
    )
    with pytest.raises(CandidateProjectionError, match="exact VLM observation closure"):
        project_candidate_catalog(
            inputs,
            values,
            scope=inputs.source_manifest.reference.scope,
            revision=1,
            policy=CandidateCatalogPolicy("candidate-catalog-v1", "0.01", ()),
        )


def test_projection_rejects_graph_fact_omitted_from_raw_universe():
    inputs = _inputs()
    stage1 = _stage1(inputs)
    graph_mapping = stage1.coverage.narrative_graph.to_mapping()
    graph_mapping["nodes"] = [
        node
        for node in graph_mapping["nodes"]
        if node["node_type"] not in {"fact", "obligation", "beat", "story_thread"}
    ]
    graph_mapping["edges"] = []
    values = replace(
        stage1,
        coverage=replace(
            stage1.coverage,
            narrative_graph=NarrativeGraph.from_mapping(graph_mapping),
        ),
    )
    with pytest.raises(CandidateProjectionError, match="Graph Fact/Event universe"):
        project_candidate_catalog(
            inputs,
            values,
            scope=inputs.source_manifest.reference.scope,
            revision=1,
            policy=CandidateCatalogPolicy("candidate-catalog-v1", "0.01", ()),
        )
