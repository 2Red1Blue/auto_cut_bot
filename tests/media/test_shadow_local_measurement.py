"""Synthetic raw responses and independent gold; not Store or model evidence."""

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.media.shadow_local_calibration import build_shadow_local_request
from autocut_kernel.media.shadow_local_measurement import (
    ShadowLocalMeasurementError,
    ShadowLocalMeasurementEvidence,
    decode_shadow_local_measurement_evidence,
)
from autocut_kernel.media.types import TickRange

from tests.media.test_shadow_local_calibration import digest, local_case
from tests.media.test_shadow_local_calibration_projection import native_raw


def measurement_case() -> ShadowLocalMeasurementEvidence:
    case = local_case()
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    return ShadowLocalMeasurementEvidence(case, request, native_raw(request))


def test_negative_original_clock_and_zero_actual_errors_survive_closed_roundtrip() -> None:
    evidence = measurement_case()
    mapping = evidence.to_mapping()
    assert mapping["raw_response_sha256"] == "sha256:" + hashlib.sha256(evidence.raw_response).hexdigest()
    assert mapping["raw_response_byte_length"] == evidence.byte_length == len(evidence.raw_response)
    assert mapping["case"] == evidence.case.to_mapping()
    assert mapping["request"] == evidence.request.to_mapping()
    projection = mapping["projection"]
    assert projection["transcript"]["context"]["origin_tick"] == -480
    assert projection["asr_matches"][0]["observed_range"] == {"start_pts": -432, "end_pts": -336}
    assert all(match["absolute_tick"] == 0 for match in projection["asr_matches"] + projection["vad_matches"])
    raw = evidence.to_bytes()
    assert evidence.canonical_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert evidence.projection_sha256 == "sha256:" + hashlib.sha256(json.dumps(
        projection, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    assert ShadowLocalMeasurementEvidence.from_mapping(mapping, raw_response=evidence.raw_response) == evidence
    assert decode_shadow_local_measurement_evidence(
        raw, raw_response=evidence.raw_response, max_bytes=len(raw),
    ) == evidence
    assert evidence.raw_response is evidence.projection.raw_response
    for forbidden in ("object_id", "receipt_id", "accepted_bound_tick", "validation_status", '"pass"'):
        assert forbidden not in raw.decode()


def test_nonzero_errors_are_recomputed_not_promoted_to_accepted_bounds() -> None:
    case = local_case()
    case = replace(case, asr_anchors=(replace(case.asr_anchors[0], expected_range=TickRange(-430, -330)),
                                    case.asr_anchors[1]))
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    measured = ShadowLocalMeasurementEvidence(case, request, native_raw(request))
    match = measured.to_mapping()["projection"]["asr_matches"][0]
    assert (match["early_tick"], match["late_tick"], match["absolute_tick"]) == (6, 0, 6)
    assert "accepted_bound_tick" not in match


def test_raw_formatting_identity_is_preserved_not_canonicalized() -> None:
    original = measurement_case()
    raw = json.dumps(json.loads(original.raw_response), ensure_ascii=False, indent=2).encode()
    changed = ShadowLocalMeasurementEvidence(original.case, original.request, raw)
    assert changed.raw_response is raw
    assert changed.raw_response_sha256 != original.raw_response_sha256
    assert changed.byte_length != original.byte_length
    assert changed.projection.transcript == original.projection.transcript
    assert changed.projection_sha256 == original.projection_sha256
    assert changed.canonical_hash != original.canonical_hash


def test_empty_real_observations_remain_empty_measurements() -> None:
    case = replace(local_case(), asr_anchors=(), vad_anchors=())
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    raw = native_raw(request, asr=[{"text": "", "timestamp": []}], vad=[{"value": []}])
    evidence = ShadowLocalMeasurementEvidence(case, request, raw)
    mapping = evidence.to_mapping()
    assert mapping["projection"]["asr_matches"] == []
    assert mapping["projection"]["vad_matches"] == []
    assert ShadowLocalMeasurementEvidence.from_mapping(mapping, raw_response=raw) == evidence


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(schema_version="shadow-calibration-measurement-results-v2"),
    lambda m: m.update(raw_response_sha256=digest("foreign")),
    lambda m: m.update(raw_response_byte_length=m["raw_response_byte_length"] + 1),
    lambda m: m.update(raw_response_byte_length=True),
    lambda m: m.update(raw_response_byte_length=float(m["raw_response_byte_length"])),
    lambda m: m["request"].update(binding_sha256=digest("foreign")),
    lambda m: m["request"]["policy"].update(asr_producer_id="foreign"),
    lambda m: m["case"]["source"].update(source_id="foreign"),
    lambda m: m["case"].update(source_provenance_sha256=digest("foreign")),
    lambda m: m["projection"]["asr_matches"][0].update(absolute_tick=1),
    lambda m: m["projection"]["asr_matches"][0].update(absolute_tick=False),
    lambda m: m["projection"]["asr_matches"][0].update(absolute_tick=0.0),
    lambda m: m["projection"]["asr_matches"].clear(),
    lambda m: m["projection"]["asr_matches"][0].update(observation_id="foreign"),
    lambda m: m["projection"]["transcript"]["words"][0].update(text="rewritten"),
    lambda m: m["projection"]["speech_activity"]["context"].update(clock_id="foreign"),
    lambda m: m["projection"].update(passed=True),
    lambda m: m.update(accepted_bound_tick=1),
    lambda m: m.update(object_id="12345678-1234-5678-1234-567812345678"),
])
def test_mapping_cannot_claim_replacement_input_or_projection(mutation) -> None:
    original = measurement_case()
    mapping = original.to_mapping()
    mutation(mapping)
    with pytest.raises(ShadowLocalMeasurementError):
        ShadowLocalMeasurementEvidence.from_mapping(mapping, raw_response=original.raw_response)


@pytest.mark.parametrize("field", ["schema_version", "case", "request", "raw_response_sha256",
                                   "raw_response_byte_length", "projection"])
def test_each_wire_field_is_required(field: str) -> None:
    original = measurement_case()
    mapping = original.to_mapping()
    del mapping[field]
    with pytest.raises(ShadowLocalMeasurementError):
        ShadowLocalMeasurementEvidence.from_mapping(mapping, raw_response=original.raw_response)


@pytest.mark.parametrize("raw", [b"", b"{}", b"not JSON", bytearray(b"{}"), "{}"])
def test_direct_construction_requires_real_exact_response_bytes(raw: object) -> None:
    original = measurement_case()
    with pytest.raises(ShadowLocalMeasurementError):
        ShadowLocalMeasurementEvidence(original.case, original.request, raw)


def test_direct_request_and_case_must_match_raw_and_actual_response_limit() -> None:
    original = measurement_case()
    for changed in (replace(original.request, binding_sha256=digest("foreign")),
                    replace(original.request, max_response_bytes=original.byte_length - 1),
                    replace(original.request, max_response_bytes=100_001)):
        with pytest.raises(ShadowLocalMeasurementError):
            ShadowLocalMeasurementEvidence(original.case, changed, original.raw_response)
    with pytest.raises(ShadowLocalMeasurementError):
        ShadowLocalMeasurementEvidence(replace(original.case, asr_anchors=()), original.request,
                                       original.raw_response)
    with pytest.raises(TypeError):
        ShadowLocalMeasurementEvidence(original.case, original.request, original.raw_response,
                                       projection=original.projection)


def test_frozen_value_and_fresh_nested_mapping() -> None:
    original = measurement_case()
    before = original.to_mapping()
    changed = original.to_mapping()
    changed["projection"]["transcript"]["words"][0]["text"] = "changed"
    changed["case"]["asr_anchors"].clear()
    assert original.to_mapping() == before
    with pytest.raises(FrozenInstanceError):
        original.raw_response = b"changed"


@pytest.mark.parametrize("variant", ["overflow", "duplicate", "float", "nan", "utf8", "nested_unknown"])
def test_bounded_strict_json_metadata_decoder(variant: str) -> None:
    original = measurement_case()
    raw = original.to_bytes()
    limit = len(raw)
    if variant == "overflow":
        limit -= 1
    elif variant == "duplicate":
        raw = b'{"schema_version":"shadow-local-measurement-evidence-v1",' + raw[1:]
    elif variant in {"float", "nan"}:
        raw = raw.replace(b'"absolute_tick":0', b'"absolute_tick":' + (b"0.0" if variant == "float" else b"NaN"), 1)
    elif variant == "utf8":
        raw = b"\xff"
    else:
        mapping = original.to_mapping()
        mapping["projection"]["vad_matches"][0]["unknown"] = True
        raw = json.dumps(mapping).encode()
    with pytest.raises(ShadowLocalMeasurementError):
        decode_shadow_local_measurement_evidence(
            raw, raw_response=original.raw_response, max_bytes=limit if variant == "overflow" else len(raw),
        )


def test_replacement_raw_cannot_match_retained_response_metadata() -> None:
    original = measurement_case()
    replacement = json.dumps(json.loads(original.raw_response), indent=2).encode()
    with pytest.raises(ShadowLocalMeasurementError):
        ShadowLocalMeasurementEvidence.from_mapping(original.to_mapping(), raw_response=replacement)


def test_fully_rehashed_changed_raw_still_cannot_retain_old_projection() -> None:
    original = measurement_case()
    payload = json.loads(original.raw_response)
    payload["asr_native_output"][0]["text"] = "改 好"
    payload["asr_native_output"][0]["words"][0] = "改"
    replacement = json.dumps(payload, ensure_ascii=False).encode()
    mapping = deepcopy(original.to_mapping())
    mapping["raw_response_sha256"] = "sha256:" + hashlib.sha256(replacement).hexdigest()
    mapping["raw_response_byte_length"] = len(replacement)
    with pytest.raises(ShadowLocalMeasurementError, match="independent recomputation"):
        ShadowLocalMeasurementEvidence.from_mapping(mapping, raw_response=replacement)


@pytest.mark.parametrize("field", ["case", "request"])
def test_direct_input_requires_exact_typed_owner(field: str) -> None:
    original = measurement_case()
    with pytest.raises(ShadowLocalMeasurementError, match="exact local case and request"):
        replace(original, **{field: getattr(original, field).to_mapping()})
