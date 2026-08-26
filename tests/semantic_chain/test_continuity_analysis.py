"""Real VLM parsing plus synthetic Store-shaped ownership, never DB acceptance."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.media.types import TickRange
from autocut_kernel.semantic_chain.continuity_analysis import (
    ContinuityAnalysisError,
    ContinuityClaim,
    ContinuityIssue,
    analyze_continuity,
)
from autocut_kernel.store import SourceWindowIdentity
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm import (
    ProxyTimelineMap,
    VlmRequestIdentity,
    WindowProxyBlobRef,
    parse_vlm_response,
)

from tests.semantic_chain.test_stage1_draft import HASH, POLICY, _blob, _synthetic_inputs
from tests.vlm import frame_pts_set
from tests.vlm.test_parser import _context, _payload, _raw


def _window(
    base,
    index,
    *,
    start=1000,
    stream=0,
    source_id="source-001",
    source_hash=HASH,
    clock="video-clock-0",
    previous=False,
    following=False,
):
    """Rebuild parsed request/response/pack identities for each synthetic window."""
    template = base.inputs[index % len(base.inputs)]
    manifest, manifest_set, parse_policy, request_template = _context()
    proxy = replace(template.source_window.proxy_blob, object_id=UUID(int=5000 + index))
    ticks = tuple(start + offset for offset in (10, 50, 90))
    manifest = replace(
        manifest,
        source_id=source_id,
        source_sha256=source_hash,
        source_clock_id=clock,
        stream_index=stream,
        source_range=TickRange(start, start + 100),
        core_range=TickRange(start, start + 100),
        proxy_blob_ref=WindowProxyBlobRef(
            str(proxy.object_id), proxy.content_hash, proxy.byte_length, proxy.media_type
        ),
        timeline_map=ProxyTimelineMap.translation(
            time_base=manifest.source_time_base,
            proxy_range=TickRange(0, 100),
            source_start_pts=start,
            max_source_error_pts=1,
        ),
        frame_samples=tuple(
            replace(sample, source_pts=tick)
            for sample, tick in zip(manifest.frame_samples, ticks, strict=True)
        ),
        frame_pts_index_set=frame_pts_set(
            source_id=source_id,
            source_sha256=source_hash,
            clock_id=clock,
            time_base=manifest.source_time_base,
            origin_tick=start,
            end_tick=start + 100,
            ticks=ticks,
        ),
    )
    manifest_set = replace(
        manifest_set,
        source_id=source_id,
        source_sha256=source_hash,
        source_clock_id=clock,
        stream_index=stream,
        declared_source_range=manifest.core_range,
        manifests=(manifest,),
    )
    identity = VlmRequestIdentity.from_manifest(
        manifest,
        manifest_set,
        prompt_template_sha256=request_template.prompt_template_sha256,
        prompt_version=request_template.prompt_version,
        response_schema_sha256=request_template.response_schema_sha256,
        model_id=request_template.model_id,
        provider_id=request_template.provider_id,
        request_parameters_sha256=request_template.request_parameters_sha256,
        request_payload_sha256=request_template.request_payload_sha256,
        parse_policy=parse_policy,
    )
    payload = _payload(manifest)
    payload["continuity"].update(
        starts_mid_event=previous,
        continues_from_previous=previous,
        ends_mid_event=following,
        continues_into_next=following,
        entry_state_fact_refs=["fact_1"] if previous else [],
        exit_state_fact_refs=["fact_1"] if following else [],
    )
    raw = _raw(payload)
    pack = parse_vlm_response(
        raw,
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=parse_policy,
    )
    raw_blob = replace(
        template.raw_response,
        object_id=UUID(int=6000 + index),
        content_hash=pack.raw_response_sha256,
        byte_length=len(raw),
    )
    old_child = template.semantic_pack.source_child
    request = json.loads(old_child.payload_json)
    request.update(
        episode_index=index,
        proxy_blob=_blob(proxy),
        request_identity=identity.to_mapping(),
        request_identity_sha256=identity.canonical_hash,
        window_manifest_sha256=identity.window_manifest_sha256,
        window_manifest_set_sha256=identity.window_manifest_set_sha256,
    )
    request_json = json.dumps(request)
    child = replace(
        old_child,
        payload_json=request_json,
        episode_index=index,
        window_manifest_sha256=identity.window_manifest_sha256,
        window_manifest_set_sha256=identity.window_manifest_set_sha256,
        request_identity_sha256=identity.canonical_hash,
        reference=replace(
            old_child.reference,
            logical_id=f"vlm_request_{identity.window_manifest_sha256[7:31]}",
            content_hash=canonical_payload_hash(request_json),
        ),
    )
    pack_json = json.dumps(pack.to_mapping())
    persisted = replace(
        template.semantic_pack,
        semantic_pack=pack,
        source_child=child,
        payload_json=pack_json,
        reference=replace(
            template.semantic_pack.reference,
            logical_id=f"semantic_pack_{identity.window_manifest_sha256[7:39]}",
            content_hash=canonical_payload_hash(pack_json),
        ),
    )
    response_json = json.dumps(
        {
            "attempt_id": str(child.attempt_id),
            "provider_request_id": f"synthetic-{index}",
            "raw_response_blob": _blob(raw_blob),
            "raw_response_sha256": pack.raw_response_sha256,
        }
    )
    return replace(
        template,
        request_identity=identity,
        semantic_pack=persisted,
        raw_response=raw_blob,
        response_record=replace(
            template.response_record,
            logical_id=f"vlm_response_{identity.window_manifest_sha256[7:31]}",
            content_hash=canonical_payload_hash(response_json),
        ),
        source_window=SourceWindowIdentity(
            index,
            stream,
            start,
            start + 100,
            identity.window_manifest_sha256,
            source_id,
            source_hash,
            clock,
            identity.window_manifest_set_sha256,
            proxy,
        ),
    )


def _inputs(*windows):
    base = _synthetic_inputs()
    return replace(
        base, inputs=tuple(_window(base, index, **spec) for index, spec in enumerate(windows))
    )


def _claim(*, window="a", direction="next", continues=True):
    return ContinuityClaim(
        "sha256:" + window * 64, direction, continues, ("sha256:" + "f" * 64,) if continues else ()
    )


def _conflict():
    claims = (_claim(), _claim(window="b", direction="previous", continues=False))
    return ContinuityIssue(
        "conflict",
        tuple(sorted(claim.window_manifest_sha256 for claim in claims)),
        tuple(sorted(claims, key=lambda item: canonical_json_bytes(item.to_mapping()))),
    )


@pytest.mark.parametrize("left,right", [(False, False), (True, True)])
def test_matching_adjacent_flags_do_not_claim_identity_equivalence(left, right):
    inputs = _inputs({"following": left}, {"start": 1100, "previous": right})
    assert analyze_continuity(inputs, policy=POLICY) == ()
    assert (
        inputs.inputs[0].semantic_pack.semantic_pack.facts[0].fact_id
        != inputs.inputs[1].semantic_pack.semantic_pack.facts[0].fact_id
    )


@pytest.mark.parametrize("left,right", [(False, True), (True, False)])
def test_conflict_retains_exact_opposing_claims_and_fact_ids(left, right):
    inputs = _inputs({"following": left}, {"start": 1100, "previous": right})
    issues = analyze_continuity(inputs, policy=POLICY)
    assert len(issues) == 1 and issues[0].kind == "conflict"
    claims = {claim.direction: claim for claim in issues[0].claims}
    assert claims["next"].continues is left and claims["previous"].continues is right
    assert (
        claims["next"].state_fact_ids
        == inputs.inputs[0].semantic_pack.semantic_pack.continuity.exit_state_fact_refs
    )
    assert (
        claims["previous"].state_fact_ids
        == inputs.inputs[1].semantic_pack.semantic_pack.continuity.entry_state_fact_refs
    )
    assert set(issues[0].windows) == {
        item.source_window.window_manifest_sha256 for item in inputs.inputs
    }


def test_both_open_outer_edges_are_separate_missing_context_not_a_conflict():
    issues = analyze_continuity(_inputs({"previous": True, "following": True}), policy=POLICY)
    assert len(issues) == 2
    assert {issue.claims[0].direction for issue in issues} == {"previous", "next"}
    assert all(issue.kind == "missing_context" and issue.claims[0].continues for issue in issues)
    assert issues[0].issue_id != issues[1].issue_id
    assert tuple(canonical_json_bytes(issue.to_mapping()) for issue in issues) == tuple(
        sorted(canonical_json_bytes(issue.to_mapping()) for issue in issues)
    )


@pytest.mark.parametrize(
    "right_change",
    [
        {"start": 1200},
        {"start": 1050},
        {"stream": 1},
        {"source_id": "another-source"},
        {"source_hash": "sha256:" + "c" * 64},
        {"clock": "another-clock"},
    ],
)
def test_gaps_overlaps_and_each_foreign_source_dimension_never_pair(right_change):
    right = {"start": 1100, "previous": True, **right_change}
    issues = analyze_continuity(_inputs({"following": True}, right), policy=POLICY)
    assert len(issues) == 2
    assert all(issue.kind == "missing_context" for issue in issues)


def test_neighbor_search_does_not_pair_interleaved_foreign_stream():
    inputs = _inputs({"following": True}, {"stream": 1}, {"start": 1100, "previous": True})
    assert analyze_continuity(inputs, policy=POLICY) == ()


def test_ambiguous_exact_neighbors_are_rejected_not_arbitrarily_selected():
    inputs = _inputs({"following": True}, {"start": 1100}, {"start": 1100})
    with pytest.raises(ContinuityAnalysisError, match="ambiguous"):
        analyze_continuity(inputs, policy=POLICY)


def test_shared_public_input_validation_rejects_identity_drift_and_policy_overflow():
    inputs = _inputs({})
    item = inputs.inputs[0]
    foreign = replace(item, response_record=replace(item.response_record, logical_id="foreign"))
    with pytest.raises(ContinuityAnalysisError, match="identity or policy"):
        analyze_continuity(replace(inputs, inputs=(foreign,)), policy=POLICY)
    with pytest.raises(ContinuityAnalysisError, match="identity or policy"):
        analyze_continuity(inputs, policy=replace(POLICY, max_prompt_bytes=1))
    with pytest.raises(ContinuityAnalysisError):
        analyze_continuity({}, policy=POLICY)


def test_wire_hash_roundtrip_and_deep_immutability_without_authority_fields():
    issue = _conflict()
    wire = issue.to_mapping()
    encoded = json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
    assert issue.issue_id == issue.canonical_hash == "sha256:" + hashlib.sha256(encoded).hexdigest()
    assert ContinuityIssue.from_mapping(wire) == issue
    assert set(wire) == {"kind", "windows", "claims"}
    assert set(wire["claims"][0]) == {
        "window_manifest_sha256",
        "direction",
        "continues",
        "state_fact_ids",
    }
    for obj, field, value in ((issue, "kind", "accepted"), (issue.claims[0], "continues", True)):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, value)
    wire["claims"][0]["state_fact_ids"].append(HASH)
    assert issue.to_mapping() != wire
    original = issue.to_mapping()
    decoded = ContinuityIssue.from_mapping(original)
    original["windows"].clear()
    assert decoded == issue


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}])
def test_continuation_flag_never_coerces(value):
    claim = _claim()
    with pytest.raises(ContinuityAnalysisError):
        replace(claim, continues=value)
    wire = claim.to_mapping()
    wire["continues"] = value
    with pytest.raises(ContinuityAnalysisError):
        ContinuityClaim.from_mapping(wire)


@pytest.mark.parametrize(
    "field,value",
    [
        ("direction", "left"),
        ("direction", True),
        ("direction", "next\ud800"),
        ("window_manifest_sha256", "sha256:" + "A" * 64),
        ("window_manifest_sha256", HASH + "\n"),
        ("window_manifest_sha256", "\ud800"),
        ("state_fact_ids", []),
        ("state_fact_ids", ()),
        ("state_fact_ids", (HASH, HASH)),
        ("state_fact_ids", ("not-a-hash",)),
        ("state_fact_ids", ("sha256:" + "f" * 64, HASH)),
    ],
)
def test_direct_claim_is_strict_and_preserves_vlm_flag_fact_shape(field, value):
    with pytest.raises(ContinuityAnalysisError):
        replace(_claim(), **{field: value})
    with pytest.raises(ContinuityAnalysisError):
        replace(_claim(continues=False), state_fact_ids=(HASH,))


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "missing",
        "wrong_array",
        "unknown_kind",
        "mutable_claims",
        "wrong_windows",
        "matching_flags",
        "false_missing",
    ],
)
def test_issue_rejects_nonclosed_or_inconsistent_shape(change):
    issue = _conflict()
    wire = deepcopy(issue.to_mapping())
    if change == "extra":
        wire["admission"] = "pass"
    elif change == "missing":
        del wire["claims"][0]["direction"]
    elif change == "wrong_array":
        wire["windows"] = tuple(wire["windows"])
    elif change == "unknown_kind":
        wire["kind"] = "pass"
    elif change == "mutable_claims":
        with pytest.raises(ContinuityAnalysisError):
            replace(issue, claims=list(issue.claims))
        return
    elif change == "wrong_windows":
        wire["windows"] = [HASH]
    elif change == "matching_flags":
        for claim in wire["claims"]:
            claim["continues"], claim["state_fact_ids"] = False, []
    else:
        wire = {
            "kind": "missing_context",
            "windows": [_claim(continues=False).window_manifest_sha256],
            "claims": [_claim(continues=False).to_mapping()],
        }
    with pytest.raises(ContinuityAnalysisError):
        ContinuityIssue.from_mapping(wire)


def test_canonical_order_duplicate_claims_and_wrong_container_are_rejected():
    issue = _conflict()
    for changes in (
        {"claims": tuple(reversed(issue.claims))},
        {"windows": list(issue.windows)},
        {"claims": (issue.claims[0], issue.claims[0])},
    ):
        with pytest.raises(ContinuityAnalysisError):
            replace(issue, **changes)
    with pytest.raises(ContinuityAnalysisError):
        ContinuityClaim.from_mapping([])
    with pytest.raises(ContinuityAnalysisError):
        ContinuityIssue.from_mapping([])
