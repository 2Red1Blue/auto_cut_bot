"""Strict provider wire mutation tests; no decoder/model/Store work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from autocut_kernel.media.local_audio_window import LocalAudioWindowSpec
from autocut_kernel.media.local_speech_window import (
    DecodedLocalPcmReport,
    LocalSpeechWindowPolicy,
    LocalSpeechWindowRequest,
)
from autocut_kernel.media.local_speech_window_codec import (
    decode_decoded_local_pcm_report,
    decode_local_audio_window_spec,
    decode_local_speech_window_request,
    decode_local_speech_window_response,
    encode_local_speech_window_response,
)
from autocut_kernel.media.types import TickRange, TimeBase

H = "sha256:" + "1" * 64
OTHER = "sha256:" + "2" * 64


def request_and_report(source_hash: str = H):
    spec = LocalAudioWindowSpec(
        "source", source_hash, 1, "original-audio", TimeBase(1, 48_000),
        TickRange(-48_000, 480_000), TickRange(48_000, 96_000), 48_000, 1,
        H, H, 1_000_000, 1000, 100_000, 192_000,
    )
    policy = LocalSpeechWindowPolicy(H, "asr", H, "vad", OTHER, 700, 350)
    request = LocalSpeechWindowRequest(spec, policy, H, 100_000)
    report = DecodedLocalPcmReport(source_hash, spec.canonical_hash, H, H, OTHER,
                                   192_080, 48_000, 1, 48_000, 48)
    return request, report


def valid_native_response(request, report):
    return encode_local_speech_window_response(
        request, report,
        [{"key": "local", "text": "你好", "words": ["你好"], "timestamp": [[100, 400]]}],
        [{"key": "local", "value": [[80, 500]]}],
    )


def test_roundtrip_retains_raw_bytes_original_source_and_local_extent():
    request, report = request_and_report()
    assert decode_local_audio_window_spec(request.extraction.to_mapping()) == request.extraction
    assert decode_local_speech_window_request(request.to_mapping()) == request
    assert decode_decoded_local_pcm_report(report.to_mapping()) == report
    raw = valid_native_response(request, report)
    decoded = decode_local_speech_window_response(raw, request)
    assert decoded.report == report and decoded.request == request
    assert decoded.raw_response is raw
    assert decoded.response_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert b"source_path" not in raw and b"endpoint" not in raw


@pytest.mark.parametrize("section", [None, "extraction", "policy"])
@pytest.mark.parametrize("mutation", ["extra", "missing", "version"])
def test_request_nested_closed_schemas(section, mutation):
    request, _ = request_and_report()
    value = request.to_mapping()
    target = value if section is None else value[section]
    if mutation == "extra":
        target["hidden_default"] = False
    elif mutation == "missing":
        del target["schema_version"]
    else:
        target["schema_version"] = "old-version"
    with pytest.raises(ValueError):
        decode_local_speech_window_request(value)


@pytest.mark.parametrize("field", [
    "audio_stream_index", "sample_rate", "channels", "max_source_bytes",
    "max_decode_frames", "max_frame_bytes", "max_pcm_bytes",
])
@pytest.mark.parametrize("value", [True, 1.0, "1", None])
def test_request_rejects_coerced_integers(field, value):
    request, _ = request_and_report()
    mapping = request.to_mapping()
    mapping["extraction"][field] = value
    with pytest.raises(ValueError):
        decode_local_speech_window_request(mapping)


@pytest.mark.parametrize(("field", "value"), [
    ("source_sha256", OTHER), ("spec_sha256", OTHER), ("decoder_identity_sha256", OTHER),
    ("sample_rate", 44_100), ("channels", 2), ("sample_count", 1),
    ("wav_byte_length", 192_000), ("wav_byte_length", 196_097), ("decoded_frames", 1001),
    ("sample_count", True), ("decoded_frames", 0),
])
def test_foreign_or_impossible_report_is_rejected(field, value):
    request, report = request_and_report()
    response = json.loads(valid_native_response(request, report))
    response["extraction_report"][field] = value
    with pytest.raises(ValueError):
        decode_local_speech_window_response(json.dumps(response).encode(), request)


def test_report_and_request_hash_change_cannot_be_hidden():
    request, report = request_and_report()
    raw = valid_native_response(request, report)
    for changed in (replace(request, binding_sha256=OTHER),
                    replace(request, policy=replace(request.policy, utterance_gap_milliseconds=701))):
        with pytest.raises(ValueError, match="identity"):
            decode_local_speech_window_response(raw, changed)


@pytest.mark.parametrize("replacement", [b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1e999"])
def test_nonfinite_native_json_numbers_reject(replacement):
    request, report = request_and_report()
    raw = valid_native_response(request, report).replace(b'"asr_native_output":', b'"extra":' + replacement + b',"asr_native_output":')
    with pytest.raises(ValueError, match="strict UTF-8 JSON"):
        decode_local_speech_window_response(raw, request)


def test_duplicate_keys_response_caps_and_invalid_utf8_reject():
    request, report = request_and_report()
    raw = valid_native_response(request, report)
    for bad in (b'{"x":1,"x":2}', b'\xff', b'{}' * request.max_response_bytes,
                raw.replace(b'"sample_count":48000', b'"sample_count":1,"sample_count":48000')):
        with pytest.raises(ValueError):
            decode_local_speech_window_response(bad, request)
    with pytest.raises(ValueError):
        encode_local_speech_window_response(request, report, [float("inf")], [])
    with pytest.raises(ValueError):
        encode_local_speech_window_response(replace(request, max_response_bytes=1), report, [], [])


def test_policy_rejects_collapsed_producers_and_invalid_gap():
    request, _ = request_and_report()
    for update in ({"vad_producer_id": "asr"}, {"utterance_gap_milliseconds": True},
                   {"vad_merge_gap_milliseconds": -1}, {"service_profile_sha256": "unknown"}):
        with pytest.raises(ValueError):
            replace(request.policy, **update)
