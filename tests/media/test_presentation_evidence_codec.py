"""Persisted-value codec tests, not Store or physical-admission acceptance."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from fractions import Fraction
from uuid import UUID

import pytest
from autocut_kernel.media.presentation_evidence_codec import (
    decode_committed_video_to_audio_clock_map_certificate as decode_certificate,
)
from autocut_kernel.media.presentation_evidence_codec import (
    decode_timed_speech_profile_admission as decode_admission,
)
from autocut_kernel.media.root_evidence_codec import decode_root_media_evidence_bundle_json
from autocut_kernel.media.stage4_predecessor import (
    AVPresentationMapSegment,
    PresentationNonOverlap,
    PresentationNonOverlapMedia,
    PresentationNonOverlapPosition,
    RationalPresentationInterval,
    admit_timed_speech_profile,
)
from autocut_kernel.media.timed_evidence_codec import (
    decode_candidate_evidence_window_plan,
    decode_candidate_timed_evidence_set,
)
from autocut_kernel.media.types import MediaValidationError, TickRange, canonical_sha256
from autocut_kernel.source_manifest import decode_source_manifest

from tests.media.test_prepare_timed_media_evidence_command import (
    _command,
    _Producer,
    _request,
    _Store,
)
from tests.media.test_root_evidence import _bundle


@pytest.fixture(scope="module")
def persisted_payloads():
    store = _Store()
    request = _request(store)
    result = _command(store, _Producer(_bundle())).execute(request)
    assert result.outcome.state == "succeeded"
    payloads = {item.artifact_type: json.loads(item.payload_json)
                for item in store.successes[-1].artifacts}
    admission = payloads["timed_speech_profile_admission"]
    reference = admission.pop("registry_member_reference")
    return payloads["committed_video_to_audio_clock_map_certificate"], admission, reference


def test_producer_payload_roundtrip_retains_exact_hashes(persisted_payloads):
    certificate, admission, _ = persisted_payloads
    for decode, value in ((decode_certificate, certificate), (decode_admission, admission)):
        decoded = decode(copy.deepcopy(value))
        assert decoded.to_mapping() == value
        assert decoded.canonical_hash == canonical_sha256(value)
        assert decode(json.loads(json.dumps(decoded.to_mapping()))) == decoded


def test_piecewise_gap_and_negative_presentation_are_preserved(persisted_payloads):
    certificate = decode_certificate(persisted_payloads[0])
    intervals = (
        RationalPresentationInterval.from_fractions(Fraction(-1), Fraction(0)),
        RationalPresentationInterval.from_fractions(Fraction(1), Fraction(2)),
    )
    # A value-shaped vector, deliberately not a claim that the source probe
    # has these segments. The later reader must independently replay the probe.
    segments = (
        AVPresentationMapSegment(TickRange(-90_000, 0), TickRange(-48_000, 0), intervals[0]),
        AVPresentationMapSegment(TickRange(90_000, 180_000), TickRange(48_000, 96_000), intervals[1]),
    )
    non_overlap = PresentationNonOverlap(
        PresentationNonOverlapMedia.VIDEO, PresentationNonOverlapPosition.INTERNAL_GAP,
        RationalPresentationInterval.from_fractions(Fraction(0), Fraction(1)),
    )
    expected = replace(certificate, map_segments=segments, common_presentation_intervals=intervals,
                       non_overlaps=(non_overlap,))
    actual = decode_certificate(expected.to_mapping())
    assert actual == expected and actual.canonical_hash == expected.canonical_hash
    assert len(actual.map_segments) == 2 and actual.non_overlaps == (non_overlap,)


def _nodes(value, path=()):
    yield path, value
    if type(value) is dict:
        for key, item in value.items():
            yield from _nodes(item, (*path, key))
    elif type(value) is list:
        for index, item in enumerate(value):
            yield from _nodes(item, (*path, index))


def _replace_at(value, path, replacement):
    if not path:
        return replacement
    result = copy.deepcopy(value)
    parent = result
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = replacement
    return result


@pytest.mark.parametrize("index,decode", [(0, decode_certificate), (1, decode_admission)])
def test_all_nested_objects_are_closed_and_required(index, decode, persisted_payloads):
    value = persisted_payloads[index]
    for path, node in _nodes(value):
        if type(node) is not dict:
            continue
        mutations = [{**node, "unexpected": "not allowed"}, None, [], "object"]
        mutations.extend({key: item for key, item in node.items() if key != removed}
                         for removed in node)
        for mutation in mutations:
            with pytest.raises(MediaValidationError):
                decode(_replace_at(value, path, mutation))


@pytest.mark.parametrize("index,decode", [(0, decode_certificate), (1, decode_admission)])
def test_exact_json_leaf_and_array_types(index, decode, persisted_payloads):
    value = persisted_payloads[index]
    for path, node in _nodes(value):
        if type(node) is int:
            replacements = (float(node), True, str(node), None)
        elif type(node) is bool:
            replacements = (int(node), str(node).lower(), None)
        elif type(node) is list:
            replacements = (tuple(node), {}, None)
        elif type(node) is str:
            replacements = (0, False, None)
            if path[-1] in ("window_manifest_sha256", "source_proxy_timeline_map_sha256"):
                replacements = replacements[:-1]  # Null is explicitly supported, but only paired.
        else:
            continue
        for replacement in replacements:
            with pytest.raises(MediaValidationError):
                decode(_replace_at(value, path, replacement))


@pytest.mark.parametrize("path,value", [
    (("schema_version",), "committed-video-to-audio-presentation-map-v1"),
    (("algorithm",), "duration_ratio"),
    (("facts_sha256",), "unbound"),
    (("snap_error_allowance_audio_tick",), -1),
    (("map_segments",), []),
    (("common_presentation_intervals",), []),
    (("map_segments", 0, "video_tick_range", "end_tick"), -1),
    (("map_segments", 0, "presentation_interval", "start_denominator"), 0),
    (("common_presentation_intervals", 0, "end_numerator"), -1),
    (("window_manifest_sha256",), None),
])
def test_certificate_domain_invariants_are_not_bypassed(path, value, persisted_payloads):
    with pytest.raises(MediaValidationError):
        decode_certificate(_replace_at(persisted_payloads[0], path, value))


def test_noncanonical_rational_and_unknown_gap_enum_rejected(persisted_payloads):
    value = copy.deepcopy(persisted_payloads[0])
    interval = value["map_segments"][0]["presentation_interval"]
    interval["start_numerator"] *= 2
    interval["start_denominator"] *= 2
    with pytest.raises(MediaValidationError):
        decode_certificate(value)
    value = copy.deepcopy(persisted_payloads[0])
    value["non_overlaps"] = [{"media": "video", "position": "ignored_gap",
                              "presentation_interval": value["common_presentation_intervals"][0]}]
    with pytest.raises(MediaValidationError):
        decode_certificate(value)


def test_explicit_nullable_pair_not_absent_fields(persisted_payloads):
    value = copy.deepcopy(persisted_payloads[0])
    value["window_manifest_sha256"] = value["source_proxy_timeline_map_sha256"] = None
    assert decode_certificate(value).to_mapping() == value
    del value["window_manifest_sha256"]
    with pytest.raises(MediaValidationError):
        decode_certificate(value)


def test_decode_does_not_attest_registry_or_evidence(persisted_payloads):
    _, admission, reference = persisted_payloads
    with pytest.raises(MediaValidationError):
        decode_admission({**admission, "registry_member_reference": reference})
    value = {**admission, "words_complete": False}
    assert decode_admission(value).words_complete is False
    # Claim preservation is necessary: the exact reader must recompute it,
    # rather than the decoder silently setting a true/default pass.
    with pytest.raises(MediaValidationError):
        decode_admission({**admission, "capability": "auto"})


def _decoded_producer_case():
    store = _Store()
    request = _request(store)
    assert _command(store, _Producer(_bundle())).execute(request).outcome.state == "succeeded"
    payloads = {item.artifact_type: json.loads(item.payload_json)
                for item in store.successes[-1].artifacts}
    root_payload = payloads["root_media_evidence_bundle"]
    raw = store.blobs[UUID(root_payload["blob"]["object_id"])]
    root = decode_root_media_evidence_bundle_json(raw, max_bytes=len(raw))
    assert root.canonical_hash == root_payload["root_bundle_sha256"]
    index = payloads["candidate_timed_evidence_index"]
    candidate_raw = store.blobs[UUID(index["candidate_blobs"][0]["object_id"])]
    candidate = decode_candidate_timed_evidence_set(json.loads(candidate_raw))
    assert candidate.canonical_hash == index["candidate_set_sha256"][0]
    plan_set = json.loads(store.blobs[UUID(index["plan_blob"]["object_id"])] )
    plan = decode_candidate_evidence_window_plan(plan_set["plans"][0])
    assert plan.final_window == candidate.candidate_window
    assert plan.final_assessment == candidate.window_assessment
    assert canonical_sha256(plan_set) == index["plan_set_sha256"]
    source = decode_source_manifest(store.source_manifest.payload_json,
                                    store.source_manifest.proxy_blobs)
    probe = source.episodes[0].media_probe.presentation_timeline_probe
    assert probe.to_mapping() == payloads["presentation_timeline_probe"]
    certificate = decode_certificate(payloads["committed_video_to_audio_clock_map_certificate"])
    audio_binding = next(item for item in candidate.calibration_bindings
                         if item.producer_id == root.audio_sample_boundaries.context.producer_id)
    return store, request, payloads, root, candidate, probe, certificate, audio_binding


def test_actual_producer_shapes_replay_all_decoded_proof_inputs():
    store, request, payloads, root, candidate, probe, certificate, binding = _decoded_producer_case()
    certificate.assert_replays_probe(probe, root, source_manifest_sha256=request.source_manifest_sha256,
                                     calibration_binding=binding)
    body = dict(payloads["timed_speech_profile_admission"])
    reference = body.pop("registry_member_reference")
    actual = admit_timed_speech_profile(store.bootstrapped_entry, reference["content_hash"],
                                       root, candidate.calibration_bindings)
    assert decode_admission(body) == actual
    # These are in-memory producer values, not an exact PostgreSQL reader or
    # proof that a real detector succeeded. The integration protects wire joins.


def test_rehashed_decoded_certificate_still_needs_independent_probe_replay():
    _, request, _, root, _, probe, certificate, binding = _decoded_producer_case()
    changed = certificate.to_mapping()
    changed["snap_error_allowance_audio_tick"] += 1
    forged = decode_certificate(changed)
    assert forged.canonical_hash != certificate.canonical_hash
    with pytest.raises(MediaValidationError, match="does not bind"):
        forged.assert_replays_probe(probe, root,
                                    source_manifest_sha256=request.source_manifest_sha256,
                                    calibration_binding=binding)
