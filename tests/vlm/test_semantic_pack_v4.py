"""Independent v4 observations preserve semantic closure without invented frames."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.vlm.models import VlmRequestIdentity, VlmValidationError
from autocut_kernel.vlm.parser import VlmResponseIndeterminate, VlmResponseRejected
from autocut_kernel.vlm.semantic_pack_v4 import VlmSemanticPackV4
from autocut_kernel.vlm.semantic_parser_v4 import (
    decode_vlm_semantic_pack_v4,
    parse_vlm_response_v4,
)
from autocut_kernel.vlm.semantic_support_v4 import (
    FrameAnchoredObservationSupportV4,
    VideoObservationSupportV4,
    frame_aliases,
)

from .test_parser import _context, _payload
from .test_semantic_support_v4 import _context as _support_context


def _v4_context():
    manifest, manifests, policy, identity = _context()
    return manifest, manifests, policy, replace(identity, prompt_version="semantic-pack-v4-video")


def _support(start: int = 20, end: int = 30) -> dict[str, object]:
    return {
        "support_kind": "video_observation",
        "interval_ms": {"start_ms": start, "end_ms": end, "uncertainty_ms": 0},
        "confidence": "0.90",
    }


def _wire() -> dict[str, Any]:
    """Provider-shaped dynamic JSON is intentional at this test boundary."""
    manifest, _, _, _ = _v4_context()
    wire: dict[str, Any] = deepcopy(_payload(manifest))
    wire["schema_version"] = 4
    for field in ("entities", "facts", "events", "candidate_hypotheses"):
        for item in wire[field]:
            item["support"] = _support()
    wire["continuity"]["temporal_segments"] = [{
        "mode": "present", "summary": "A brief event between sparse frames.", "support": _support(),
    }]
    return wire


def _raw(wire: dict[str, Any]) -> bytes:
    return json.dumps(wire, ensure_ascii=False).encode("utf-8")


def _parse(wire: dict[str, Any] | None = None) -> VlmSemanticPackV4:
    manifest, manifests, policy, identity = _v4_context()
    return parse_vlm_response_v4(
        _raw(_wire() if wire is None else wire), manifest=manifest, manifest_set=manifests,
        request_identity=identity, policy=policy,
    )


def _decode(mapping: object) -> VlmSemanticPackV4:
    manifest, manifests, policy, identity = _v4_context()
    return decode_vlm_semantic_pack_v4(
        mapping, manifest=manifest, manifest_set=manifests, request_identity=identity, policy=policy,
    )


def _fractional_context():
    manifest, manifests = _support_context(time_base=TimeBase(1, 12_800), duration=1280)
    _, _, policy, original_identity = _v4_context()
    identity = VlmRequestIdentity.from_manifest(
        manifest, manifests, prompt_template_sha256=original_identity.prompt_template_sha256,
        prompt_version=original_identity.prompt_version,
        response_schema_sha256=original_identity.response_schema_sha256,
        model_id=original_identity.model_id, provider_id=original_identity.provider_id,
        request_parameters_sha256=original_identity.request_parameters_sha256,
        request_payload_sha256=original_identity.request_payload_sha256, parse_policy=policy,
    )
    return manifest, manifests, policy, identity


def test_adjacent_millisecond_segments_do_not_overlap_after_outward_quantization() -> None:
    manifest, manifests, policy, identity = _fractional_context()
    wire = _wire()
    wire["continuity"]["temporal_segments"] = [
        {"mode": "present", "summary": "First segment", "support": _support(0, 1)},
        {"mode": "present", "summary": "Second segment", "support": _support(1, 2)},
    ]
    pack = parse_vlm_response_v4(
        _raw(wire), manifest=manifest, manifest_set=manifests, request_identity=identity, policy=policy,
    )
    first, second = pack.continuity.temporal_segments
    assert first.support.proxy_interval.proxy_range == TickRange(700, 713)
    assert second.support.proxy_interval.proxy_range == TickRange(712, 726)
    assert decode_vlm_semantic_pack_v4(
        pack.to_mapping(), manifest=manifest, manifest_set=manifests, request_identity=identity, policy=policy,
    ) == pack
    wire["continuity"]["temporal_segments"].reverse()
    with pytest.raises(VlmResponseRejected):
        parse_vlm_response_v4(
            _raw(wire), manifest=manifest, manifest_set=manifests, request_identity=identity, policy=policy,
        )


@pytest.mark.parametrize("relation", ["event_fact", "candidate_event"])
def test_quantization_does_not_manufacture_semantic_support_overlap(relation: str) -> None:
    manifest, manifests, policy, identity = _fractional_context()
    wire = _wire()
    for field in ("entities", "facts", "events", "candidate_hypotheses"):
        for item in wire[field]:
            item["support"] = _support(0, 1)
    if relation == "event_fact":
        wire["facts"][0]["support"] = _support(1, 2)
    else:
        wire["candidate_hypotheses"][0]["support"] = _support(1, 2)
    with pytest.raises(VlmResponseRejected, match="must overlap"):
        parse_vlm_response_v4(
            _raw(wire), manifest=manifest, manifest_set=manifests, request_identity=identity, policy=policy,
        )


def test_complete_video_observation_between_anchors_is_a_real_immutable_v4_pack() -> None:
    wire = _wire()
    pack = _parse(wire)
    assert type(pack) is VlmSemanticPackV4
    assert type(pack.facts[0].support) is VideoObservationSupportV4
    assert "frame_refs" not in pack.facts[0].support.to_wire_mapping()
    assert "frame_anchors" not in pack.facts[0].support.to_mapping()
    assert pack.raw_response_sha256 == "sha256:" + hashlib.sha256(_raw(wire)).hexdigest()
    assert pack.to_mapping()["schema_version"] == 4
    assert _decode(pack.to_mapping()) == pack
    assert _decode(pack.to_mapping()).canonical_hash == pack.canonical_hash
    with pytest.raises(FrozenInstanceError):
        pack.raw_response_sha256 = "sha256:" + "9" * 64  # type: ignore[misc]


def test_frame_branch_still_requires_a_real_registered_anchor_inside_interval() -> None:
    manifest, _, _, _ = _v4_context()
    wire = _wire()
    alias = frame_aliases(manifest).entries[1].alias
    wire["facts"][0]["support"] = {**_support(), "support_kind": "frame_anchored_observation", "frame_refs": [alias]}
    with pytest.raises(VlmResponseRejected):
        _parse(wire)
    for field in ("entities", "facts", "events", "candidate_hypotheses"):
        for item in wire[field]:
            item["support"] = _support(40, 60)
    wire["facts"][0]["support"] = {
        **_support(40, 60), "support_kind": "frame_anchored_observation", "frame_refs": [alias],
    }
    pack = _parse(wire)
    assert type(pack.facts[0].support) is FrameAnchoredObservationSupportV4
    assert _decode(pack.to_mapping()) == pack
    wire["facts"][0]["support"]["frame_refs"] = ["unknown"]
    with pytest.raises(VlmResponseRejected):
        _parse(wire)


@pytest.mark.parametrize("version", [3, 5, True, "4", None])
def test_other_wire_versions_are_not_reinterpreted(version: object) -> None:
    wire = _wire()
    wire["schema_version"] = version
    with pytest.raises(VlmResponseRejected, match="UNSUPPORTED_SCHEMA_VERSION"):
        _parse(wire)


@pytest.mark.parametrize("mutation", [
    "unknown_field", "missing_summary", "empty_facts", "duplicate_entity", "duplicate_fact",
    "unknown_subject", "unknown_object", "unknown_participant", "unknown_fact", "unknown_cause",
    "duplicate_reference", "self_loop", "unknown_summary_ref", "bad_continuity", "bad_entry",
    "overlapping_segments", "reverse_segments", "unknown_anchor", "missing_payoff", "empty_measurements",
    "unknown_measurement_ref", "unsupported_enum", "wrong_boolean", "noncanonical_tags", "no_event_overlap",
    "no_candidate_overlap", "video_frame_field", "fake_accepted",
])
def test_semantic_constraints_fail_closed(mutation: str) -> None:
    wire = _wire()
    if mutation == "unknown_field":
        wire["hidden_permission"] = True
    elif mutation == "missing_summary":
        del wire["window_summary"]["summary"]
    elif mutation == "empty_facts":
        wire["facts"] = []
    elif mutation == "duplicate_entity":
        wire["entities"].append(deepcopy(wire["entities"][0]))
    elif mutation == "duplicate_fact":
        wire["facts"].append(deepcopy(wire["facts"][0]))
    elif mutation == "unknown_subject":
        wire["facts"][0]["subject_ref"] = "missing"
    elif mutation == "unknown_object":
        wire["facts"][0]["object_ref"] = "missing"
    elif mutation == "unknown_participant":
        wire["events"][0]["participant_refs"] = ["missing"]
    elif mutation == "unknown_fact":
        wire["events"][0]["fact_refs"] = ["missing"]
    elif mutation == "unknown_cause":
        wire["events"][0]["cause_event_refs"] = ["missing"]
    elif mutation == "duplicate_reference":
        wire["events"][0]["fact_refs"] = ["fact_1", "fact_1"]
    elif mutation == "self_loop":
        wire["events"][0]["cause_event_refs"] = ["event_1"]
    elif mutation == "unknown_summary_ref":
        wire["window_summary"]["event_refs"] = ["missing"]
    elif mutation == "bad_continuity":
        wire["continuity"]["starts_mid_event"] = True
    elif mutation == "bad_entry":
        wire["continuity"]["entry_state_fact_refs"] = ["fact_1"]
    elif mutation == "overlapping_segments":
        wire["continuity"]["temporal_segments"] *= 2
    elif mutation == "reverse_segments":
        segment = deepcopy(wire["continuity"]["temporal_segments"][0])
        segment["support"] = _support(0, 10)
        wire["continuity"]["temporal_segments"].append(segment)
    elif mutation == "unknown_anchor":
        wire["candidate_hypotheses"][0]["anchor_event_ref"] = "missing"
    elif mutation == "missing_payoff":
        wire["candidate_hypotheses"][0]["payoff_event_refs"] = []
    elif mutation == "empty_measurements":
        wire["candidate_hypotheses"][0]["measurements"] = []
    elif mutation == "unknown_measurement_ref":
        wire["candidate_hypotheses"][0]["measurements"][0]["fact_refs"] = ["missing"]
    elif mutation == "unsupported_enum":
        wire["facts"][0]["fact_kind"] = "physical_safe"
    elif mutation == "wrong_boolean":
        wire["continuity"]["starts_mid_event"] = 0
    elif mutation == "noncanonical_tags":
        wire["candidate_hypotheses"][0]["tags"] = ["reveal", "dialogue"]
    elif mutation == "no_event_overlap":
        wire["events"][0]["support"] = _support(60, 70)
    elif mutation == "no_candidate_overlap":
        wire["candidate_hypotheses"][0]["support"] = _support(60, 70)
    elif mutation == "video_frame_field":
        wire["facts"][0]["support"]["frame_refs"] = []
    elif mutation == "fake_accepted":
        wire["facts"][0]["support"]["accepted"] = True
    with pytest.raises(VlmResponseRejected):
        _parse(wire)


@pytest.mark.parametrize("mutation", ["inverse", "cycle", "measurement_closure"])
def test_causal_graph_and_candidate_measurement_closure_are_preserved(mutation: str) -> None:
    wire = _wire()
    event = deepcopy(wire["events"][0])
    event["local_event_id"] = "event_2"
    wire["events"].append(event)
    if mutation == "inverse":
        wire["events"][0]["effect_event_refs"] = ["event_2"]
    elif mutation == "cycle":
        for index, other in ((0, "event_2"), (1, "event_1")):
            wire["events"][index]["cause_event_refs"] = [other]
            wire["events"][index]["effect_event_refs"] = [other]
    else:
        wire["candidate_hypotheses"][0]["measurements"][0]["event_refs"] = ["event_2"]
    with pytest.raises(VlmResponseRejected):
        _parse(wire)


@pytest.mark.parametrize("field,value", [
    ("start_ms", -1), ("start_ms", True), ("start_ms", 1.0), ("start_ms", 30),
    ("end_ms", 101), ("end_ms", 0), ("uncertainty_ms", -1), ("end_ms", "30"),
])
def test_bad_millisecond_intervals_are_not_clamped(field: str, value: object) -> None:
    wire = _wire()
    wire["facts"][0]["support"]["interval_ms"][field] = value
    with pytest.raises(VlmResponseRejected):
        _parse(wire)


@pytest.mark.parametrize("raw", [b"\xff", b'{"schema_version":4,"schema_version":4}', b'{"x":NaN}', b'{} trailing'])
def test_invalid_utf8_or_json_is_rejected(raw: bytes) -> None:
    manifest, manifests, policy, identity = _v4_context()
    with pytest.raises(VlmResponseRejected):
        parse_vlm_response_v4(raw, manifest=manifest, manifest_set=manifests, request_identity=identity, policy=policy)


def test_raw_byte_and_text_budgets_remain_frozen() -> None:
    manifest, manifests, policy, identity = _v4_context()
    raw = _raw(_wire())
    limited = replace(policy, max_response_bytes=len(raw) - 1)
    with pytest.raises(VlmResponseIndeterminate, match="RESPONSE_BUDGET_EXCEEDED"):
        parse_vlm_response_v4(raw, manifest=manifest, manifest_set=manifests,
                              request_identity=replace(identity, parse_policy_sha256=limited.canonical_hash), policy=limited)
    wire = _wire()
    wire["facts"][0]["summary"] = "x" * (policy.max_text_characters + 1)
    with pytest.raises(VlmResponseIndeterminate, match="TEXT_BUDGET_EXCEEDED"):
        _parse(wire)


@pytest.mark.parametrize("mutation", ["request", "window", "global_id", "support_derived", "unknown", "extra_frame", "unsorted"])
def test_persisted_mapping_is_recomputed_not_trusted(mutation: str) -> None:
    mapping: dict[str, Any] = deepcopy(_parse().to_mapping())
    if mutation == "request":
        mapping["provenance"]["request_identity_sha256"] = "sha256:" + "9" * 64
    elif mutation == "window":
        mapping["provenance"]["window_manifest_sha256"] = "sha256:" + "9" * 64
    elif mutation == "global_id":
        mapping["facts"][0]["fact_id"] = "sha256:" + "9" * 64
    elif mutation == "support_derived":
        mapping["facts"][0]["support"]["derived"] = {}
    elif mutation == "unknown":
        mapping["facts"][0]["hidden"] = True
    elif mutation == "extra_frame":
        mapping["facts"][0]["support"]["frame_anchors"] = []
    elif mutation == "unsorted":
        mapping["candidate_hypotheses"][0]["tags"].reverse()
    with pytest.raises(VlmValidationError):
        _decode(mapping)


def test_persisted_raw_hash_requires_external_blob_reparse_for_authenticity() -> None:
    mapping: dict[str, Any] = deepcopy(_parse().to_mapping())
    mapping["provenance"]["raw_response_sha256"] = "sha256:" + "9" * 64
    decoded = _decode(mapping)
    assert decoded.raw_response_sha256 != _parse().raw_response_sha256
    assert decoded.canonical_hash != _parse().canonical_hash


@pytest.mark.parametrize("field,budget", [
    ("entities", "max_entities"), ("facts", "max_facts"), ("events", "max_events"),
    ("candidate_hypotheses", "max_candidate_hypotheses"),
    ("temporal_segments", "max_temporal_segments"), ("measurements", "max_measurements"),
])
def test_each_structural_budget_is_applied_before_acceptance(field: str, budget: str) -> None:
    manifest, manifests, policy, identity = _v4_context()
    wire = _wire()
    if field == "temporal_segments":
        wire["continuity"][field] *= 2
    elif field == "measurements":
        wire["candidate_hypotheses"][0][field] *= 2
    else:
        wire[field] *= 2
    limited = replace(policy, **{budget: 1})
    with pytest.raises(VlmResponseIndeterminate, match="STRUCTURE_BUDGET_EXCEEDED"):
        parse_vlm_response_v4(_raw(wire), manifest=manifest, manifest_set=manifests,
                              request_identity=replace(identity, parse_policy_sha256=limited.canonical_hash), policy=limited)


def test_total_text_budget_is_separate_from_per_field_budget() -> None:
    manifest, manifests, policy, identity = _v4_context()
    limited = replace(policy, max_text_characters=64, max_total_text_characters=64)
    with pytest.raises(VlmResponseIndeterminate, match="total text budget"):
        parse_vlm_response_v4(_raw(_wire()), manifest=manifest, manifest_set=manifests,
                              request_identity=replace(identity, parse_policy_sha256=limited.canonical_hash), policy=limited)


@pytest.mark.parametrize("change", ["manifest", "parse_policy"])
def test_request_must_bind_exact_manifest_and_parse_policy(change: str) -> None:
    manifest, manifests, policy, identity = _v4_context()
    if change == "manifest":
        identity = replace(identity, window_manifest_sha256="sha256:" + "9" * 64)
    else:
        policy = replace(policy, max_entities=policy.max_entities + 1)
    with pytest.raises(VlmValidationError):
        parse_vlm_response_v4(_raw(_wire()), manifest=manifest, manifest_set=manifests, request_identity=identity, policy=policy)
    with pytest.raises(VlmValidationError):
        decode_vlm_semantic_pack_v4(_parse().to_mapping(), manifest=manifest, manifest_set=manifests,
                                    request_identity=identity, policy=policy)


def test_hook_rules_and_no_candidate_response_roundtrip_without_weakening_facts() -> None:
    wire = _wire()
    candidate = wire["candidate_hypotheses"][0]
    candidate["candidate_kind"] = "hook"
    candidate["open_question"] = "What is inside?"
    candidate["payoff_event_refs"] = []
    pack = _parse(wire)
    assert _decode(pack.to_mapping()) == pack
    candidate["open_question"] = None
    with pytest.raises(VlmResponseRejected, match="INVALID_CANDIDATE_KIND_RULE"):
        _parse(wire)
    wire["candidate_hypotheses"] = []
    pack = _parse(wire)
    assert pack.facts and not pack.candidate_hypotheses
    assert _decode(pack.to_mapping()) == pack


def test_typed_pack_cannot_claim_another_window_for_its_supports() -> None:
    with pytest.raises(VlmValidationError, match="different window manifest"):
        replace(_parse(), window_manifest_sha256="sha256:" + "9" * 64)


def test_original_raw_whitespace_changes_raw_identity_not_a_synthetic_projection_hash() -> None:
    manifest, manifests, policy, identity = _v4_context()
    raw = json.dumps(_wire(), indent=2).encode()
    pack = parse_vlm_response_v4(raw, manifest=manifest, manifest_set=manifests,
                                 request_identity=identity, policy=policy)
    assert pack.raw_response_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert pack.raw_response_sha256 != _parse().raw_response_sha256
    assert _decode(pack.to_mapping()).raw_response_sha256 == pack.raw_response_sha256


def test_surrogate_text_and_excessive_integer_json_fail_at_the_parser_boundary() -> None:
    manifest, manifests, policy, identity = _v4_context()
    wire = _wire()
    wire["facts"][0]["summary"] = "\ud800"
    for raw in (json.dumps(wire).encode(), b'{"x":' + b"1" * 5000 + b"}"):
        with pytest.raises(VlmResponseRejected):
            parse_vlm_response_v4(raw, manifest=manifest, manifest_set=manifests,
                                  request_identity=identity, policy=policy)
