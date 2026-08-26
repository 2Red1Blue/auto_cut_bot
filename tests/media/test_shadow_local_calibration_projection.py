"""Exercise the real shared decoder/projector using synthetic native JSON only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest
from autocut_kernel.media.calibration import CalibrationAnchor
from autocut_kernel.media.local_speech_window import DecodedLocalPcmReport, LocalSpeechWindowRequest
from autocut_kernel.media.local_speech_window_codec import (
    decode_local_speech_window_response,
    encode_local_speech_window_response,
)
from autocut_kernel.media.local_speech_window_projection import project_local_speech_window
from autocut_kernel.media.root_evidence import EvidenceCompleteness, TranscriptSourceOutcome
from autocut_kernel.media.shadow_local_calibration import (
    ShadowLocalCalibrationCase,
    ShadowLocalCalibrationError,
    build_shadow_local_request,
)
from autocut_kernel.media.shadow_local_calibration_projection import (
    ShadowLocalCalibrationProjection,
    project_shadow_local_calibration,
)
from autocut_kernel.media.types import TickRange

from tests.media.test_shadow_local_calibration import digest, local_case


def native_raw(request: LocalSpeechWindowRequest, *, asr: object = None, vad: object = None) -> bytes:
    """The PCM report and native outputs are synthetic, not executed models."""
    spec = request.extraction
    report = DecodedLocalPcmReport(
        spec.source_sha256, spec.canonical_hash, spec.decoder_identity_sha256,
        digest("synthetic-pcm"), digest("synthetic-wav"), spec.expected_samples * spec.channels * 4 + 128,
        spec.sample_rate, spec.channels, spec.expected_samples, 2,
    )
    return encode_local_speech_window_response(
        request, report,
        [{"text": "你 好", "words": ["你", "好"], "timestamp": [[1, 3], [8, 10]]}] if asr is None else asr,
        [{"value": [[0, 3], [4, 6], [12, 15]]}] if vad is None else vad,
    )


def project(case: ShadowLocalCalibrationCase) -> ShadowLocalCalibrationProjection:
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    return project_shadow_local_calibration(native_raw(request), case=case, request=request)


def test_real_raw_replay_exact_gold_zero_errors_are_not_forced_to_positive_bounds() -> None:
    case = local_case()
    measured = project(case)
    assert [m.absolute_tick for m in measured.asr_matches] == [0, 0]
    assert [m.absolute_tick for m in measured.vad_matches] == [0, 0]
    assert measured.asr_matches[0].observation.observed_range == TickRange(-432, -336)
    assert measured.vad_matches[0].observation.observed_range == TickRange(-480, -192)
    assert measured.transcript.coverage.in_tick == -480
    assert measured.transcript.coverage.out_tick == 960  # Not whole-source coverage.
    assert measured.transcript.context.origin_tick != case.extraction.source_range.start_pts
    assert measured.transcript.completeness.sentence is EvidenceCompleteness.NOT_APPLICABLE
    assert tuple(word.text for word in measured.transcript.words) == ("你", "好")
    assert measured.case_sha256 == case.canonical_hash
    assert measured.request_sha256 == measured.request.canonical_hash
    assert measured.response_sha256 == "sha256:" + hashlib.sha256(measured.raw_response).hexdigest()
    assert not hasattr(measured, "summary") and not hasattr(measured, "accepted_bound_tick")
    with pytest.raises(FrozenInstanceError):
        setattr(measured, "asr_matches", ())


def test_raw_bytes_not_reencoded_and_native_mutation_cannot_change_measurements() -> None:
    case = local_case()
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    raw = json.dumps(json.loads(native_raw(request)), ensure_ascii=False, indent=2).encode()
    measured = project_shadow_local_calibration(raw, case=case, request=request)
    assert measured.raw_response is raw
    assert b"\xe4\xbd\xa0" in raw
    assert measured.response_sha256 != project(case).response_sha256
    assert measured.asr_matches == project(case).asr_matches
    decoded = decode_local_speech_window_response(raw, request)
    cast(dict[str, object], cast(list[object], decoded.asr_native_output)[0])["timestamp"] = [[900, 901]]
    assert project_local_speech_window(decoded).transcript == measured.transcript
    assert project_shadow_local_calibration(raw, case=case, request=request) == measured


def test_endpoint_errors_are_actual_and_gold_is_neither_clipped_nor_replaced() -> None:
    case = local_case()
    anchors = (replace(case.asr_anchors[0], expected_range=TickRange(-430, -330)), case.asr_anchors[1])
    case = replace(case, asr_anchors=anchors)
    measured = project(case)
    match = measured.asr_matches[0]
    assert match.anchor is anchors[0]
    assert match.observation.observed_range == TickRange(-432, -336)
    assert (match.early_tick, match.late_tick, match.absolute_tick) == (6, 0, 6)
    assert measured.asr_matches[1].absolute_tick == 0


@pytest.mark.parametrize("offset", (-96_000, 48_000, 10**18))
def test_observations_use_absolute_source_clock_without_normalizing_origin(offset: int) -> None:
    case = local_case()

    def shifted(interval: TickRange) -> TickRange:
        return TickRange(interval.start_pts + offset, interval.end_pts + offset)

    case = replace(
        case, extraction=replace(case.extraction, source_range=shifted(case.extraction.source_range),
                                 requested_range=shifted(case.extraction.requested_range)),
        asr_anchors=tuple(replace(a, expected_range=shifted(a.expected_range)) for a in case.asr_anchors),
        vad_anchors=tuple(replace(a, expected_range=shifted(a.expected_range)) for a in case.vad_anchors),
    )
    measured = project(case)
    assert measured.asr_matches[0].observation.observed_range.start_pts == -432 + offset
    assert all(match.absolute_tick == 0 for match in measured.asr_matches + measured.vad_matches)


@pytest.mark.parametrize("producer", ("asr", "vad"))
@pytest.mark.parametrize("change", ("missing", "extra", "empty"))
def test_every_anchor_and_observation_must_participate(producer: str, change: str) -> None:
    case = local_case()
    anchors = case.asr_anchors if producer == "asr" else case.vad_anchors
    updated: tuple[CalibrationAnchor, ...]
    if change == "empty":
        updated = ()
    elif change == "missing":
        updated = anchors[:1]
    else:
        updated = anchors + (replace(anchors[-1], anchor_id="extra", expected_range=TickRange(500, 600)),)
    case = replace(case, **{producer + "_anchors": updated})
    with pytest.raises(ShadowLocalCalibrationError, match="one-to-one"):
        project(case)


@pytest.mark.parametrize("producer", ("asr", "vad"))
def test_raw_reordering_is_not_silently_sorted_to_match_gold(producer: str) -> None:
    case = local_case()
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    raw = native_raw(
        request,
        asr=[{"text": "好你", "words": ["好", "你"], "timestamp": [[8, 10], [1, 3]]}] if producer == "asr" else None,
        vad=[{"value": [[12, 15], [0, 6]]}] if producer == "vad" else None,
    )
    with pytest.raises(ShadowLocalCalibrationError, match="replay"):
        project_shadow_local_calibration(raw, case=case, request=request)


@pytest.mark.parametrize("variant", ("silence", "vad-only", "words-without-vad"))
def test_empty_measurements_are_explicit_not_fabricated_matches(variant: str) -> None:
    case = local_case()
    has_words = variant == "words-without-vad"
    has_vad = variant == "vad-only"
    case = replace(case, asr_anchors=case.asr_anchors if has_words else (),
                   vad_anchors=case.vad_anchors if has_vad else ())
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    raw = native_raw(request, asr=None if has_words else [{"text": "", "timestamp": []}],
                     vad=None if has_vad else [{"value": []}])
    if has_words:
        with pytest.raises(ShadowLocalCalibrationError, match="replay"):
            project_shadow_local_calibration(raw, case=case, request=request)
        return
    measured = project_shadow_local_calibration(raw, case=case, request=request)
    assert measured.asr_matches == ()
    assert len(measured.vad_matches) == (2 if has_vad else 0)
    assert measured.transcript.source_outcome is (
        TranscriptSourceOutcome.NO_LEXICAL_CONTENT if has_vad else TranscriptSourceOutcome.NO_SPEECH
    )


@pytest.mark.parametrize("field", ("source_provenance", "blob", "corpus", "native_profile", "service_profile", "model", "policies", "anchors", "extraction"))
def test_coherent_foreign_case_rehash_cannot_consume_original_response(field: str) -> None:
    original = local_case()
    request = build_shadow_local_request(original, max_response_bytes=100_000)
    raw = native_raw(request)
    if field == "source_provenance":
        changed = replace(original, source_provenance_sha256=digest("foreign"))
    elif field == "blob":
        changed = replace(original, source=replace(original.source, blob_id="22345678-1234-5678-1234-567812345678"))
    elif field == "corpus":
        changed = replace(original, source=replace(original.source, corpus_member_reference_sha256=digest("foreign")))
    elif field == "native_profile":
        changed = replace(original, native_profile_identity_sha256=digest("foreign"))
    elif field == "service_profile":
        changed = replace(original, policy=replace(original.policy, service_profile_sha256=digest("foreign")))
    elif field == "model":
        asr, vad = original.producer_identities
        changed = replace(original, producer_identities=(replace(asr, model_sha256=digest("foreign")), vad))
    elif field == "policies":
        changed = replace(original, policies=replace(original.policies, timed_speech_policy_sha256=digest("foreign")))
    elif field == "anchors":
        changed = replace(original, asr_anchors=(replace(original.asr_anchors[0], anchor_id="foreign"), original.asr_anchors[1]))
    else:
        changed = replace(original, extraction=replace(original.extraction, max_decode_frames=101))
    changed_request = build_shadow_local_request(changed, max_response_bytes=100_000)
    assert changed_request.canonical_hash != request.canonical_hash
    with pytest.raises(ShadowLocalCalibrationError, match="derived"):
        project_shadow_local_calibration(raw, case=changed, request=request)
    with pytest.raises(ShadowLocalCalibrationError, match="replay"):
        project_shadow_local_calibration(raw, case=changed, request=changed_request)


@pytest.mark.parametrize("field", ("source_sha256", "spec_sha256", "decoder_identity_sha256", "sample_rate", "channels", "sample_count", "wav_byte_length", "decoded_frames"))
def test_rehashed_foreign_report_cannot_substitute_expected_spec(field: str) -> None:
    case = local_case()
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    mapping = cast(dict[str, object], json.loads(native_raw(request)))
    report = cast(dict[str, object], mapping["extraction_report"])
    report[field] = digest("foreign") if field.endswith("sha256") else 999_999
    raw = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()
    # It is valid fresh JSON with its own real SHA; merely hashing it cannot fix the join.
    assert "sha256:" + hashlib.sha256(raw).hexdigest() != project(case).response_sha256
    with pytest.raises(ShadowLocalCalibrationError, match="replay"):
        project_shadow_local_calibration(raw, case=case, request=request)


def test_response_limit_is_explicit_and_bound_to_request() -> None:
    case = local_case()
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    raw = native_raw(request)
    limited = replace(request, max_response_bytes=len(raw) - 1)
    with pytest.raises(ShadowLocalCalibrationError, match="replay"):
        project_shadow_local_calibration(raw, case=case, request=limited)


@pytest.mark.parametrize("raw", (None, bytearray(b"{}"), "{}", b"", b"{}", b"[]", b"{\"a\":1e999}"))
def test_projection_rejects_nonbytes_and_malformed_raw(raw: object) -> None:
    case = local_case()
    request = build_shadow_local_request(case, max_response_bytes=100_000)
    with pytest.raises(ShadowLocalCalibrationError):
        ShadowLocalCalibrationProjection(case, request, cast(bytes, raw))
