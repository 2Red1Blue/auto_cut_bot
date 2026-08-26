"""Pure synthetic multi-clock corpus; no native execution or acceptance."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

import pytest
from autocut_kernel.media.shadow_local_calibration import build_shadow_local_request
from autocut_kernel.media.shadow_local_measurement import ShadowLocalMeasurementEvidence
from autocut_kernel.media.shadow_local_measurement_set import (
    ShadowLocalMeasurementManifest,
    ShadowLocalMeasurementManifestMember,
    ShadowLocalMeasurementResults,
    ShadowLocalMeasurementSetError,
    ShadowLocalMeasurementValidationReport,
)
from autocut_kernel.media.types import TickRange, TimeBase

from tests.media.test_shadow_local_calibration import digest, local_case
from tests.media.test_shadow_local_calibration_projection import native_raw


def measurement_set_case():
    """Same source, distinct original clocks/TBs and independently authored gold."""
    first = local_case()
    base = TimeBase(1, 1000)
    clock = "second-native-audio-clock"

    def interval(value):
        return TickRange(value.start_pts // 48, value.end_pts // 48)

    second = replace(
        first,
        extraction=replace(first.extraction, audio_stream_index=3, clock_id=clock, time_base=base,
                           source_range=interval(first.extraction.source_range),
                           requested_range=interval(first.extraction.requested_range)),
        asr_anchors=tuple(replace(anchor, clock_id=clock, time_base=base,
                                 expected_range=interval(anchor.expected_range)) for anchor in first.asr_anchors),
        vad_anchors=tuple(replace(anchor, clock_id=clock, time_base=base,
                                 expected_range=interval(anchor.expected_range)) for anchor in first.vad_anchors),
    )
    second = replace(second, asr_anchors=(replace(second.asr_anchors[0], expected_range=TickRange(-8, -5)),
                                         second.asr_anchors[1]))
    evidence = []
    for case in (first, second):
        request = build_shadow_local_request(case, max_response_bytes=100_000)
        evidence.append(ShadowLocalMeasurementEvidence(case, request, native_raw(request)))
    manifest = ShadowLocalMeasurementManifest.from_evidence(tuple(evidence))
    results = ShadowLocalMeasurementResults(manifest, tuple(evidence))
    responses = {member.raw_response_key: item.raw_response
                 for member, item in zip(manifest.members, results.evidence, strict=True)}
    return manifest, results, responses


def test_ordered_same_source_distinct_clock_corpus_results_and_report_roundtrip() -> None:
    manifest, results, responses = measurement_set_case()
    assert len({member.case.source.source_sha256 for member in manifest.members}) == 1
    assert len({member.case.extraction.clock_id for member in manifest.members}) == 2
    assert len({member.case.extraction.time_base for member in manifest.members}) == 2
    assert ShadowLocalMeasurementManifest.from_mapping(manifest.to_mapping()) == manifest
    restored = ShadowLocalMeasurementResults.from_mapping(
        results.to_mapping(), manifest=manifest, raw_responses=MappingProxyType(responses),
    )
    assert restored == results
    assert [row["evidence"] for row in results.to_mapping()["members"]] == [
        item.to_mapping() for item in results.evidence
    ]
    report = ShadowLocalMeasurementValidationReport(restored)
    wire = report.to_mapping()
    assert wire["manifest_sha256"] == manifest.canonical_hash
    assert wire["results_sha256"] == results.canonical_hash
    assert [member["asr"]["maximum_absolute_tick"] for member in wire["members"]] == [0, 2]
    assert wire["members"][0]["asr"]["matches"][0]["observed_range"]["start_pts"] == -432
    assert wire["members"][1]["asr"]["matches"][0]["observed_range"]["start_pts"] == -9
    assert [row["time_base"]["denominator"] for row in wire["members"]] == [48_000, 1000]
    assert ShadowLocalMeasurementValidationReport.from_mapping(
        wire, results=results, raw_responses=responses,
    ) == report
    for value in (manifest, results, report):
        raw = json.dumps(value.to_mapping(), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        assert value.canonical_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
        for forbidden in ("accepted_bound", "receipt", "registry", "record_sha", '"pass"', '"aggregate"'):
            assert forbidden not in raw.decode()


def test_empty_producer_matches_are_not_reported_as_zero_measured_error() -> None:
    manifest, results, _responses = measurement_set_case()
    original = results.evidence[0]
    case = replace(original.case, asr_anchors=(), vad_anchors=())
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    empty = ShadowLocalMeasurementEvidence(case, request, native_raw(
        request, asr=[{"text": "", "timestamp": []}], vad=[{"value": []}],
    ))
    manifest = ShadowLocalMeasurementManifest.from_evidence((empty, results.evidence[1]))
    report = ShadowLocalMeasurementValidationReport(ShadowLocalMeasurementResults(
        manifest, (empty, results.evidence[1]),
    ))
    row = report.to_mapping()["members"][0]
    for role in ("asr", "vad"):
        assert row[role] == {"matches": [], "maximum_absolute_tick": None}


@pytest.mark.parametrize("variant", ["reordered", "duplicate", "omitted", "foreign_request", "extra", "bool_ordinal"])
def test_manifest_is_closed_contiguous_and_pairs_exact_request(variant: str) -> None:
    manifest, _results, _responses = measurement_set_case()
    wire = manifest.to_mapping()
    if variant == "reordered":
        wire["members"].reverse()
    elif variant == "duplicate":
        wire["members"][1] = {**wire["members"][0], "ordinal": 1}
    elif variant == "omitted":
        wire["members"].pop(0)
    elif variant == "foreign_request":
        wire["members"][0]["request"] = wire["members"][1]["request"]
    elif variant == "bool_ordinal":
        wire["members"][0]["ordinal"] = False
    else:
        wire["members"][0]["accepted"] = True
    with pytest.raises(ShadowLocalMeasurementSetError):
        ShadowLocalMeasurementManifest.from_mapping(wire)


@pytest.mark.parametrize("variant", ["reverse", "renumber_reverse", "duplicate", "omit", "extra", "manifest",
                                    "case", "request", "projection", "unknown_nested", "bool_ordinal"])
def test_result_rows_must_preserve_exact_manifest_and_replayed_projection(variant: str) -> None:
    manifest, results, responses = measurement_set_case()
    wire = results.to_mapping()
    rows = wire["members"]
    if variant in {"reverse", "renumber_reverse"}:
        rows.reverse()
        if variant == "renumber_reverse":
            for ordinal, row in enumerate(rows):
                row["ordinal"] = ordinal
    elif variant == "duplicate":
        rows[1] = {**rows[0], "ordinal": 1}
    elif variant == "omit":
        rows.pop()
    elif variant == "extra":
        rows.append(rows[0])
    elif variant == "manifest":
        wire["manifest_sha256"] = digest("foreign-manifest")
    elif variant in {"case", "request"}:
        rows[0][variant + "_sha256"] = digest("foreign")
    elif variant == "projection":
        rows[1]["evidence"]["projection"]["asr_matches"][0]["absolute_tick"] = 0
    elif variant == "bool_ordinal":
        rows[0]["ordinal"] = False
    else:
        rows[0]["evidence"]["projection"]["accepted_bound_tick"] = 1
    with pytest.raises(ShadowLocalMeasurementSetError):
        ShadowLocalMeasurementResults.from_mapping(wire, manifest=manifest, raw_responses=responses)


@pytest.mark.parametrize("variant", ["missing", "extra", "source_key", "only_ordinal", "case_only", "bool_ordinal",
                                    "foreign_case", "swap", "wrong_bytes", "bytearray"])
def test_raw_lookup_accepts_only_exact_ordinal_case_pair_and_exact_bytes(variant: str) -> None:
    manifest, results, responses = measurement_set_case()
    keys = list(responses)
    if variant == "missing":
        responses.pop(keys[-1])
    elif variant == "extra":
        responses[(2, keys[0][1])] = responses[keys[0]]
    elif variant == "swap":
        responses[keys[0]], responses[keys[1]] = responses[keys[1]], responses[keys[0]]
    elif variant == "wrong_bytes":
        responses[keys[0]] += b" "
    elif variant == "bytearray":
        responses[keys[0]] = bytearray(responses[keys[0]])
    else:
        key = {"source_key": manifest.members[0].case.source.source_id, "only_ordinal": 0,
               "case_only": keys[0][1], "bool_ordinal": (False, keys[0][1]),
               "foreign_case": (0, digest("foreign"))}[variant]
        responses[key] = responses.pop(keys[0])
    with pytest.raises(ShadowLocalMeasurementSetError):
        ShadowLocalMeasurementResults.from_mapping(results.to_mapping(), manifest=manifest, raw_responses=responses)


def test_raw_response_mapping_iteration_order_does_not_change_corpus_order() -> None:
    manifest, results, responses = measurement_set_case()
    reordered = dict(reversed(list(responses.items())))
    assert ShadowLocalMeasurementResults.from_mapping(
        results.to_mapping(), manifest=manifest, raw_responses=reordered,
    ) == results


@pytest.mark.parametrize("variant", ["maximum", "zero_float", "zero_bool", "reorder", "omission", "source", "clock",
                                    "time_base", "results", "manifest", "accepted"])
def test_report_has_no_caller_verdict_or_substitutable_per_case_truth(variant: str) -> None:
    _manifest, results, responses = measurement_set_case()
    report = ShadowLocalMeasurementValidationReport(results)
    wire = report.to_mapping()
    row = wire["members"][0]
    if variant in {"maximum", "zero_float", "zero_bool"}:
        row["asr"]["maximum_absolute_tick"] = {"maximum": 1, "zero_float": 0.0, "zero_bool": False}[variant]
    elif variant == "reorder":
        wire["members"].reverse()
    elif variant == "omission":
        wire["members"].pop()
    elif variant in {"source", "clock"}:
        row[variant + "_id"] = "foreign"
    elif variant == "time_base":
        row["time_base"] = {"numerator": 1, "denominator": 1000}
    elif variant in {"results", "manifest"}:
        wire[variant + "_sha256"] = digest("foreign")
    else:
        wire["pass"] = True
    with pytest.raises(ShadowLocalMeasurementSetError):
        ShadowLocalMeasurementValidationReport.from_mapping(wire, results=results, raw_responses=responses)


def test_changed_raw_fully_rehashed_in_result_cannot_retain_claimed_projection() -> None:
    manifest, results, responses = measurement_set_case()
    wire = results.to_mapping()
    key = manifest.members[1].raw_response_key
    payload = json.loads(responses[key])
    payload["asr_native_output"][0]["text"] = "改 好"
    payload["asr_native_output"][0]["words"][0] = "改"
    responses[key] = json.dumps(payload).encode()
    item = wire["members"][1]["evidence"]
    item["raw_response_sha256"] = "sha256:" + hashlib.sha256(responses[key]).hexdigest()
    item["raw_response_byte_length"] = len(responses[key])
    with pytest.raises(ShadowLocalMeasurementSetError, match="independent raw replay"):
        ShadowLocalMeasurementResults.from_mapping(wire, manifest=manifest, raw_responses=responses)


def test_independent_report_replays_instead_of_trusting_frozen_type_label() -> None:
    _manifest, results, _responses = measurement_set_case()
    # Simulate a corrupted in-memory producer-derived projection, not a native
    # call or a replacement validation helper. Report must reread actual bytes.
    item = results.evidence[1]
    object.__setattr__(item.projection, "asr_matches", ())
    with pytest.raises(ShadowLocalMeasurementSetError, match="independent raw replay"):
        ShadowLocalMeasurementValidationReport(results)


def test_frozen_tuples_empty_corpus_and_foreign_direct_results_rejected() -> None:
    manifest, results, _responses = measurement_set_case()
    for members in ((), list(manifest.members), (object(),)):
        with pytest.raises(ShadowLocalMeasurementSetError):
            ShadowLocalMeasurementManifest(members)
    for evidence in ((), list(results.evidence), tuple(reversed(results.evidence))):
        with pytest.raises(ShadowLocalMeasurementSetError):
            ShadowLocalMeasurementResults(manifest, evidence)
    with pytest.raises(ShadowLocalMeasurementSetError):
        replace(manifest.members[0], ordinal=True)
    with pytest.raises(ShadowLocalMeasurementSetError):
        ShadowLocalMeasurementManifestMember(0, manifest.members[0].case, manifest.members[1].request)
    with pytest.raises(FrozenInstanceError):
        manifest.members = ()
    mapping = results.to_mapping()
    mapping["members"][0]["evidence"]["projection"]["asr_matches"].clear()
    assert results.to_mapping()["members"][0]["evidence"]["projection"]["asr_matches"]
