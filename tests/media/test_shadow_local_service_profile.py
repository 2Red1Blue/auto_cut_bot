"""Synthetic measured content only: no native model, Record, or acceptance."""

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.media.shadow_local_service_profile import (
    SHADOW_LOCAL_SERVICE_PROFILE_SCHEMA,
    ShadowLocalServiceProfileError,
    build_shadow_local_service_profile,
    decode_shadow_local_service_profile,
)

H = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def measured_shadow_local_profile() -> dict[str, object]:
    """Complete synthetic pre-calibration fields, never deployment defaults."""
    return {
        "schema_version": SHADOW_LOCAL_SERVICE_PROFILE_SCHEMA,
        "provider_id": "funasr-http-v1",
        "provider_version": "1.0.0",
        "service_sha256": H,
        "funasr_version": "synthetic-funasr",
        "torch_version": "synthetic-torch",
        "device": "cpu",
        "word_timing_capability": "required",
        "max_request_bytes": 100_000,
        "timed_speech_policy_sha256": H,
        "word_gap_policy_sha256": H,
        "vad_merge_policy_sha256": H,
        "utterance_gap_milliseconds": 700,
        "vad_merge_gap_milliseconds": 350,
        "decoder_identity_sha256": H,
        "producers": [
            {
                "producer_kind": role,
                "producer_id": role,
                "producer_version": "synthetic-1",
                "generation_policy_sha256": H,
                "detector_sha256": H,
                "calibration_policy_sha256": H,
                "model_id": model,
                "model_revision": "synthetic-revision",
                "model_sha256": H,
                "service_sha256": H,
                "inference_kind": inference,
            }
            for role, model, inference in (
                ("asr", "SenseVoiceSmall", "sensevoice-word-timestamp"),
                ("vad", "fsmn-vad", "fsmn-vad-direct"),
            )
        ],
    }


def _sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def test_independent_two_level_hash_oracle_and_roundtrip() -> None:
    measured = measured_shadow_local_profile()
    profile = build_shadow_local_service_profile(measured)
    native_hash = _sha(measured)
    complete = {**measured, "native_port_identity_sha256": native_hash}
    assert profile.native_port_identity_sha256 == native_hash
    assert profile.to_mapping() == complete
    assert profile.canonical_hash == _sha(complete) != native_hash
    assert decode_shadow_local_service_profile(complete) == profile
    assert decode_shadow_local_service_profile(json.loads(json.dumps(complete))) == profile
    assert measured == measured_shadow_local_profile()


def test_unicode_and_key_order_use_media_canonical_json_not_caller_formatting() -> None:
    measured = measured_shadow_local_profile()
    measured["provider_version"] = "版本-é-😀"
    profile = build_shadow_local_service_profile(measured)
    reversed_mapping = dict(reversed(list(measured.items())))
    assert build_shadow_local_service_profile(reversed_mapping) == profile
    assert profile.native_port_identity_sha256 == _sha(measured)
    assert decode_shadow_local_service_profile(json.loads(json.dumps(
        profile.to_mapping(), ensure_ascii=False, indent=2,
    ))) == profile


@pytest.mark.parametrize("field", [
    "provider_id", "provider_version", "funasr_version", "torch_version", "device",
    "max_request_bytes", "timed_speech_policy_sha256", "word_gap_policy_sha256",
    "vad_merge_policy_sha256", "utterance_gap_milliseconds", "vad_merge_gap_milliseconds",
    "decoder_identity_sha256", "service_sha256",
])
def test_every_mutable_root_measurement_changes_both_hashes(field: str) -> None:
    measured = measured_shadow_local_profile()
    original = build_shadow_local_service_profile(measured)
    old = measured[field]
    measured[field] = OTHER if field.endswith("sha256") else old + 1 if type(old) is int else str(old) + "-changed"
    if field == "service_sha256":
        for producer in measured["producers"]:
            producer[field] = OTHER
    changed = build_shadow_local_service_profile(measured)
    assert changed.native_port_identity_sha256 != original.native_port_identity_sha256
    assert changed.canonical_hash != original.canonical_hash
    with pytest.raises(ShadowLocalServiceProfileError, match="native identity"):
        decode_shadow_local_service_profile({**changed.to_mapping(),
                                             "native_port_identity_sha256": original.native_port_identity_sha256})


@pytest.mark.parametrize("ordinal", [0, 1])
@pytest.mark.parametrize("field", [
    "producer_id", "producer_version", "generation_policy_sha256", "detector_sha256",
    "calibration_policy_sha256", "model_id", "model_revision", "model_sha256",
])
def test_each_producer_measurement_changes_both_hashes(ordinal: int, field: str) -> None:
    measured = measured_shadow_local_profile()
    original = build_shadow_local_service_profile(measured)
    producer = measured["producers"][ordinal]
    producer[field] = OTHER if field.endswith("sha256") else producer[field] + "-changed"
    changed = build_shadow_local_service_profile(measured)
    assert changed.native_port_identity_sha256 != original.native_port_identity_sha256
    assert changed.canonical_hash != original.canonical_hash


@pytest.mark.parametrize("field", list(measured_shadow_local_profile()))
def test_missing_root_field_rejected_by_builder_and_decoder(field: str) -> None:
    measured = measured_shadow_local_profile()
    wire = build_shadow_local_service_profile(measured).to_mapping()
    del measured[field]
    del wire[field]
    for decode, value in ((build_shadow_local_service_profile, measured),
                          (decode_shadow_local_service_profile, wire)):
        with pytest.raises(ShadowLocalServiceProfileError):
            decode(value)


@pytest.mark.parametrize("field", list(measured_shadow_local_profile()["producers"][0]))
def test_missing_nested_field_rejected(field: str) -> None:
    measured = measured_shadow_local_profile()
    del measured["producers"][0][field]
    with pytest.raises(ShadowLocalServiceProfileError):
        build_shadow_local_service_profile(measured)


@pytest.mark.parametrize("field", [
    "calibration_record_sha256", "profile_calibration_sha256", "timing_error_bound_tick",
    "accepted", "receipt_id", "unknown", "native_port_identity_sha256",
])
@pytest.mark.parametrize("nested", [False, True])
def test_builder_rejects_accepted_future_or_self_fields(field: str, nested: bool) -> None:
    measured = measured_shadow_local_profile()
    target = measured["producers"][0] if nested else measured
    target[field] = H
    with pytest.raises(ShadowLocalServiceProfileError):
        build_shadow_local_service_profile(measured)


@pytest.mark.parametrize("schema", [
    "funasr-measured-profile-v1", "funasr-shadow-calibration-profile-v1",
    "funasr-shadow-local-calibration-profile-v2", None, True,
])
def test_old_or_unknown_schema_rejected(schema: object) -> None:
    measured = measured_shadow_local_profile()
    measured["schema_version"] = schema
    with pytest.raises(ShadowLocalServiceProfileError):
        build_shadow_local_service_profile(measured)


@pytest.mark.parametrize("field", ["max_request_bytes", "utterance_gap_milliseconds", "vad_merge_gap_milliseconds"])
@pytest.mark.parametrize("bad", [True, False, 1.0, float("nan"), float("inf"), "1", -1, 2**53, None])
def test_integer_fields_reject_coercion_and_unsafe_values(field: str, bad: object) -> None:
    profile = build_shadow_local_service_profile(measured_shadow_local_profile())
    with pytest.raises(ShadowLocalServiceProfileError):
        replace(profile, **{field: bad})
    measured = measured_shadow_local_profile()
    measured[field] = bad
    with pytest.raises(ShadowLocalServiceProfileError):
        build_shadow_local_service_profile(measured)


def test_zero_gaps_allowed_but_positive_request_bound_required() -> None:
    profile = build_shadow_local_service_profile(measured_shadow_local_profile())
    assert replace(profile, utterance_gap_milliseconds=0, vad_merge_gap_milliseconds=0)
    with pytest.raises(ShadowLocalServiceProfileError):
        replace(profile, max_request_bytes=0)


@pytest.mark.parametrize("bad", ["", "  ", "\ud800", b"utf8", 1, True, None])
@pytest.mark.parametrize("nested", [False, True])
def test_text_must_be_exact_nonempty_utf8(bad: object, nested: bool) -> None:
    measured = measured_shadow_local_profile()
    target = measured["producers"][0] if nested else measured
    target["producer_version" if nested else "provider_version"] = bad
    with pytest.raises(ShadowLocalServiceProfileError):
        build_shadow_local_service_profile(measured)


@pytest.mark.parametrize("bad", ["sha256:" + "0" * 64, "sha256:" + "A" * 64, "a" * 64, True, H.encode()])
@pytest.mark.parametrize("nested", [False, True])
def test_identity_hashes_are_exact_nonzero_lowercase(bad: object, nested: bool) -> None:
    measured = measured_shadow_local_profile()
    target = measured["producers"][0] if nested else measured
    target["model_sha256" if nested else "decoder_identity_sha256"] = bad
    with pytest.raises(ShadowLocalServiceProfileError):
        build_shadow_local_service_profile(measured)


@pytest.mark.parametrize("mutation", [
    lambda m: m["producers"].reverse(),
    lambda m: m["producers"].pop(),
    lambda m: m["producers"].append(deepcopy(m["producers"][0])),
    lambda m: m["producers"][1].update(producer_id="asr"),
    lambda m: m["producers"][0].update(producer_kind="unknown"),
    lambda m: m["producers"][0].update(inference_kind="fsmn-vad-direct"),
    lambda m: m["producers"][0].update(service_sha256=OTHER),
    lambda m: m.update(word_timing_capability="sentence_only"),
    lambda m: m.update(producers=tuple(m["producers"])),
])
def test_closed_role_and_service_consistency(mutation) -> None:
    measured = measured_shadow_local_profile()
    mutation(measured)
    with pytest.raises(ShadowLocalServiceProfileError):
        build_shadow_local_service_profile(measured)


def test_direct_values_and_returned_mappings_are_immutable_and_detached() -> None:
    measured = measured_shadow_local_profile()
    profile = build_shadow_local_service_profile(measured)
    original = profile.to_mapping()
    measured["producers"][0]["model_id"] = "changed"
    returned = profile.to_mapping()
    returned["producers"][0]["model_id"] = "changed-again"
    assert profile.to_mapping() == original
    with pytest.raises(FrozenInstanceError):
        profile.device = "changed"
    with pytest.raises(FrozenInstanceError):
        profile.producers[0].model_id = "changed"
    with pytest.raises(ShadowLocalServiceProfileError):
        replace(profile, producers=list(profile.producers))
    with pytest.raises(ShadowLocalServiceProfileError):
        replace(profile, producers=(profile.producers[0], object()))


def test_decoder_requires_exact_complete_mapping_and_matching_native_identity() -> None:
    profile = build_shadow_local_service_profile(measured_shadow_local_profile())
    wire = profile.to_mapping()
    for bad in (None, [], json.dumps(wire).encode(), measured_shadow_local_profile(),
                {**wire, "native_port_identity_sha256": OTHER}, {**wire, "accepted": True}):
        with pytest.raises(ShadowLocalServiceProfileError):
            decode_shadow_local_service_profile(bad)
