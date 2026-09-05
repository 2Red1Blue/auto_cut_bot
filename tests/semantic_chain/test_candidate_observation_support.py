"""Synthetic request-bound V4 candidate supports; no Store or provider acceptance."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.candidate_catalog import (
    CANDIDATE_LEGACY_STRATEGY,
    CANDIDATE_OBSERVATION_STRATEGY,
    Candidate,
    CandidateCatalog,
    CandidateCatalogError,
    CandidateCatalogPolicy,
    CandidateSupport,
    FrameAnchoredCandidateSupport,
    VideoCandidateSupport,
    project_candidate_support,
)
from autocut_kernel.semantic_chain.candidate_duration import conservative_support_duration
from autocut_kernel.semantic_chain.candidate_projection import (
    decode_candidate_source_context,
    project_candidate_catalog,
)
from autocut_kernel.semantic_chain.editorial_feasibility import (
    EditorialFeasibilityError,
    _candidate_domain,
)
from autocut_kernel.store import PersistedVlmSemanticPackV4
from autocut_kernel.store.models import (
    VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
    canonical_payload_hash,
)
from autocut_kernel.vlm.semantic_parser_v4 import parse_vlm_response_v4
from autocut_kernel.vlm.semantic_support_v4 import parse_support_v4

from tests.semantic_chain.test_candidate_catalog import _candidate, catalog_for
from tests.semantic_chain.test_candidate_projection import _inputs, _stage1
from tests.vlm.test_parser import _context as _parse_context
from tests.vlm.test_semantic_pack_v4 import _wire
from tests.vlm.test_semantic_support_v4 import _context, _frame_wire
from tests.vlm.test_semantic_support_v4 import _wire as _video_wire


def _project_support(wire, *, context=None):
    manifest, manifest_set = context or _context()
    raw = parse_support_v4(wire, manifest, manifest_set)
    projected = project_candidate_support(
        raw, conservative_support_duration(raw, manifest.timeline_map),
        strategy_version=CANDIDATE_OBSERVATION_STRATEGY,
        expected_manifest=manifest, expected_manifest_set=manifest_set,
    )
    return raw, projected


def test_video_observation_has_no_frame_claim_and_keeps_original_uncertainty():
    raw, projected = _project_support(_video_wire(20, 40, 3))
    assert type(projected) is VideoCandidateSupport
    assert not hasattr(projected, "supporting_frame_ids")
    assert "supporting_frame_ids" not in projected.to_mapping()
    assert "frame_anchors" not in projected.to_mapping()
    assert projected.interval_ms == raw.interval_ms
    assert projected.proxy_interval == raw.proxy_interval
    assert projected.source_interval == raw.source_interval
    assert projected.observed_window_manifest_sha256 == raw.manifest.canonical_hash
    assert projected.proxy_blob_ref_sha256 == raw.manifest.proxy_blob_ref.canonical_hash
    assert VideoCandidateSupport.from_mapping(projected.to_mapping()) == projected


def test_frame_support_retains_only_declared_anchors_in_original_order():
    raw, projected = _project_support(_frame_wire(0, 80, refs=("f0003", "f0001")))
    assert type(projected) is FrameAnchoredCandidateSupport
    assert projected.support_kind == "frame_anchored"
    assert tuple(anchor.alias for anchor in projected.frame_anchors) == ("f0003", "f0001")
    assert tuple(anchor.frame_id for anchor in projected.frame_anchors) == tuple(
        anchor.frame_id for anchor in raw.frame_anchors
    )
    # Two distinct sampled identities may legitimately carry identical image bytes.
    assert len(projected.frame_anchors) == 2
    assert len({anchor.frame_sha256 for anchor in projected.frame_anchors}) == 1
    assert FrameAnchoredCandidateSupport.from_mapping(projected.to_mapping()) == projected
    assert "f0002" not in json.dumps(projected.to_mapping())


@pytest.mark.parametrize("wire", [_video_wire(), _frame_wire()])
def test_support_from_other_window_is_rejected_even_when_frame_bytes_match(wire):
    manifest, manifest_set = _context()
    foreign_manifest, foreign_set = _context(proxy_start=900)
    raw = parse_support_v4(wire, foreign_manifest, foreign_set)
    with pytest.raises(CandidateCatalogError, match="another request window"):
        project_candidate_support(
            raw, conservative_support_duration(raw, foreign_manifest.timeline_map),
            strategy_version=CANDIDATE_OBSERVATION_STRATEGY,
            expected_manifest=manifest, expected_manifest_set=manifest_set,
        )


@pytest.mark.parametrize("field,value", [("supporting_frame_ids", []), ("frame_anchors", []),
                                         ("support_kind", "frame_anchored")])
def test_video_wire_cannot_be_extended_to_claim_frames(field, value):
    _, projected = _project_support(_video_wire())
    with pytest.raises(CandidateCatalogError):
        VideoCandidateSupport.from_mapping({**projected.to_mapping(), field: value})


def test_frame_branch_cannot_lose_its_required_anchors():
    _, projected = _project_support(_frame_wire())
    with pytest.raises(CandidateCatalogError):
        replace(projected, frame_anchors=())
    with pytest.raises(CandidateCatalogError):
        replace(projected, frame_anchors=projected.frame_anchors * 2)


def test_legacy_support_remains_nonempty_and_legacy_catalog_rejects_new_support():
    original = _candidate()
    with pytest.raises(CandidateCatalogError):
        replace(original.support, supporting_frame_ids=())
    _, support = _project_support(_video_wire())
    candidate = replace(original, support=support, source_window_ref=replace(
        original.source_window_ref, object_id=support.core_owner_window_manifest_sha256,
    ))
    with pytest.raises(CandidateCatalogError):
        catalog_for(candidate)
    with pytest.raises(CandidateCatalogError):
        Candidate.from_mapping(candidate.to_mapping())
    catalog = replace(catalog_for(original), candidates=(candidate,),
                      schema_version=CANDIDATE_OBSERVATION_STRATEGY)
    wire = catalog.to_mapping()
    assert wire["schema_version"] == CANDIDATE_OBSERVATION_STRATEGY
    assert CandidateCatalog.from_mapping(wire) == catalog
    del wire["schema_version"]
    with pytest.raises(CandidateCatalogError):
        CandidateCatalog.from_mapping(wire)


def _v4_projection_inputs(*, frame_anchored=False):
    """Synthesize exact decoded Source/request/V4 DTO bindings from existing fixtures."""
    inputs = _inputs(command_ready=True)
    original = inputs.inputs[0]
    episode = decode_candidate_source_context(inputs).episodes[0]
    manifest, manifest_set = episode.manifest, episode.manifest_set
    identity = replace(original.request_identity, prompt_version="semantic-pack-v4-video")
    wire = _wire()
    wire["continuity"]["ends_mid_event"] = False
    wire["continuity"]["continues_into_next"] = False
    wire["continuity"]["exit_state_fact_refs"] = []
    # This Source fixture spans 100 native 90 kHz ticks, not the 100 ms
    # duration of _wire's own manifest. Bind every observation, including
    # temporal segments, to the actual representable playback milliseconds.
    timeline = manifest.timeline_map
    duration_ms = (timeline.proxy_range.duration_pts * timeline.proxy_time_base.numerator
                   * 1000 // timeline.proxy_time_base.denominator)
    assert duration_ms >= 1
    interval = {"start_ms": 0, "end_ms": duration_ms, "uncertainty_ms": 0}
    for name in ("entities", "facts", "events", "candidate_hypotheses"):
        for item in wire[name]:
            item["support"]["interval_ms"] = dict(interval)
    for segment in wire["continuity"]["temporal_segments"]:
        segment["support"]["interval_ms"] = dict(interval)
    if frame_anchored:
        # Prove the declared frame lies before the exact whole-ms boundary;
        # outward tick rounding must not manufacture anchor inclusion.
        frame = manifest.frame_samples[1]
        assert (0 <= (frame.proxy_pts - timeline.proxy_range.start_pts)
                * timeline.proxy_time_base.numerator * 1000
                < duration_ms * timeline.proxy_time_base.denominator)
        wire["candidate_hypotheses"][0]["support"].update({
            "support_kind": "frame_anchored_observation", "frame_refs": ["f0002"],
        })
    raw = canonical_json_bytes(wire)
    _, _, policy, _ = _parse_context()
    pack = parse_vlm_response_v4(raw, manifest=manifest, manifest_set=manifest_set,
                                 request_identity=identity, policy=policy)
    child = original.semantic_pack.source_child
    request = json.loads(child.payload_json)
    request["request_identity"] = identity.to_mapping()
    request["request_identity_sha256"] = identity.canonical_hash
    request_json = canonical_json_bytes(request).decode()
    child = replace(child, payload_json=request_json,
                    reference=replace(child.reference, content_hash=canonical_payload_hash(request_json)),
                    request_identity_sha256=identity.canonical_hash,
                    parser_strategy_version="strict-semantic-pack-v4", semantic_schema_version=4)
    pack_json = canonical_json_bytes(pack.to_mapping()).decode()
    persisted = PersistedVlmSemanticPackV4(
        replace(original.semantic_pack.reference, content_hash=canonical_payload_hash(pack_json)),
        pack_json, pack, child,
    )
    committed = replace(original, request_identity=identity, semantic_pack=persisted,
                        raw_response=replace(original.raw_response, content_hash=pack.raw_response_sha256,
                                             byte_length=len(raw)))
    return replace(inputs, inputs=(committed,), vlm_aggregate_policy=child.request_policy,
                   vlm_batch_strategy_version=VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4)


@pytest.mark.parametrize("frame_anchored", [False, True])
def test_public_projection_and_catalog_reader_use_new_support_strategy(frame_anchored):
    inputs = _v4_projection_inputs(frame_anchored=frame_anchored)
    stage1 = _stage1(inputs)
    policy = CandidateCatalogPolicy(CANDIDATE_OBSERVATION_STRATEGY, "0.5", ())
    kwargs = dict(scope=inputs.source_manifest.reference.scope, revision=1, policy=policy)
    first = project_candidate_catalog(inputs, stage1, **kwargs)
    assert first == project_candidate_catalog(inputs, stage1, **kwargs)
    assert first.catalog.schema_version == CANDIDATE_OBSERVATION_STRATEGY
    assert CandidateCatalog.from_mapping(first.catalog.to_mapping()) == first.catalog
    support = first.catalog.candidates[0].support
    assert type(support) is (FrameAnchoredCandidateSupport if frame_anchored else VideoCandidateSupport)
    assert "supporting_frame_ids" not in support.to_mapping()
    if frame_anchored:
        assert tuple(anchor.alias for anchor in support.frame_anchors) == ("f0002",)


def test_new_catalog_keeps_real_v3_frame_support_and_old_projection_bytes():
    inputs = _inputs()
    stage1 = _stage1(inputs)
    old_policy = CandidateCatalogPolicy(CANDIDATE_LEGACY_STRATEGY, "0.5", ())
    old = project_candidate_catalog(inputs, stage1, scope=inputs.source_manifest.reference.scope,
                                    revision=1, policy=old_policy)
    new = project_candidate_catalog(inputs, stage1, scope=inputs.source_manifest.reference.scope,
                                    revision=1, policy=replace(old_policy, strategy_version=CANDIDATE_OBSERVATION_STRATEGY))
    assert "schema_version" not in old.catalog.to_mapping()
    assert type(new.catalog.candidates[0].support) is CandidateSupport
    assert new.catalog.candidates == old.catalog.candidates
    assert canonical_json_bytes(CandidateCatalog.from_mapping(old.catalog.to_mapping()).to_mapping()) == canonical_json_bytes(old.catalog.to_mapping())


def test_editorial_support_reader_rebuilds_new_strategy_and_rejects_tampered_binding():
    inputs = _v4_projection_inputs()
    stage1 = _stage1(inputs)
    projection = project_candidate_catalog(
        inputs, stage1, scope=inputs.source_manifest.reference.scope, revision=1,
        policy=CandidateCatalogPolicy(CANDIDATE_OBSERVATION_STRATEGY, "0.5", ()),
    )
    # Exercise only the independent support reader. This synthetic holder is
    # deliberately not a Stage 2 admission or an accepted portfolio fixture.
    holder = SimpleNamespace(members=(projection.member,), business=SimpleNamespace(
        candidate_catalog=projection.catalog,
        proposal_set=SimpleNamespace(source_grant_sha256=inputs.source_grant.canonical_hash),
    ))
    domain = _candidate_domain(inputs, stage1, holder)
    assert len(domain) == len(projection.catalog.candidates)
    assert all(type(item.candidate.support) is VideoCandidateSupport for item in domain.values())
    candidate = projection.catalog.candidates[0]
    tampered = replace(candidate, support=replace(candidate.support,
                                                proxy_blob_ref_sha256="sha256:" + "f" * 64))
    holder.business.candidate_catalog = replace(projection.catalog, candidates=(tampered,))
    with pytest.raises(EditorialFeasibilityError, match="raw capability/Source/support"):
        _candidate_domain(inputs, stage1, holder)
