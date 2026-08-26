"""Persisted producer-shaped synthetic evidence; never Store/acceptance proof."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.media.root_evidence import (
    AudioSourceOutcome,
    CoverageOutcome,
    EvidenceCompleteness,
    SpeechSourceOutcome,
    TranscriptCompleteness,
    TranscriptSourceOutcome,
)
from autocut_kernel.media.root_evidence_codec import (
    decode_audio_sample_boundary_set,
    decode_coverage,
    decode_evidence_context,
    decode_frame_pts_index_set,
    decode_root_media_evidence_bundle,
    decode_root_media_evidence_bundle_json,
    decode_scene_boundary_set,
    decode_shot_boundary_set,
    decode_speech_activity_set,
    decode_subtitle_cue_set,
    decode_time_base,
    decode_transcript_set,
    decode_visual_validity_set,
)
from autocut_kernel.media.types import MediaValidationError, PTSIndex, canonical_sha256

from tests.media.test_root_evidence import _bundle, _coverage, _no_speech_transcript, _no_speech_vad


def _raw(mapping):
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _paths(value, kind, path=()):
    if type(value) is kind:
        yield path
    if type(value) is dict:
        for key, child in value.items():
            yield from _paths(child, kind, (*path, key))
    elif type(value) is list:
        for index, child in enumerate(value):
            yield from _paths(child, kind, (*path, index))


def _get(value, path):
    for key in path:
        value = value[key]
    return value


def _change(mapping, path, value):
    _get(mapping, path[:-1])[path[-1]] = value


_WIRE = _bundle().to_mapping()


def test_complete_producer_wire_roundtrip_preserves_canonical_bytes_and_hash():
    original = _bundle()
    mapping = original.to_mapping()
    raw = _raw(mapping)
    decoded = decode_root_media_evidence_bundle(mapping)
    assert decoded == original
    assert decoded.canonical_hash == original.canonical_hash
    assert _raw(decoded.to_mapping()) == raw
    assert decode_root_media_evidence_bundle_json(raw, max_bytes=len(raw)) == original


@pytest.mark.parametrize(("field", "decode"), [
    ("frame_pts_index", decode_frame_pts_index_set),
    ("audio_sample_boundaries", decode_audio_sample_boundary_set),
    ("shot_boundaries", decode_shot_boundary_set),
    ("scene_boundaries", decode_scene_boundary_set),
    ("transcript", decode_transcript_set),
    ("speech_activity", decode_speech_activity_set),
    ("visual_validity", decode_visual_validity_set),
    ("subtitle_cues", decode_subtitle_cue_set),
])
def test_public_component_decoders_roundtrip_without_mutating_or_aliasing(field, decode):
    original = getattr(_bundle(), field)
    mapping = original.to_mapping()
    before = deepcopy(mapping)
    decoded = decode(mapping)
    assert decoded == original and mapping == before
    assert decode_evidence_context(mapping["context"]) == original.context
    assert decode_coverage(mapping["coverage"]) == original.coverage
    mapping["context"]["source_id"] = "caller mutation"
    assert decoded == original
    result_mapping = decoded.to_mapping()
    result_mapping["context"]["producer_id"] = "other mutation"
    assert decoded == original
    with pytest.raises(FrozenInstanceError):
        decoded.context = original.context


@pytest.mark.parametrize("path", tuple(_paths(_WIRE, dict)))
@pytest.mark.parametrize("mutation", ("extra", "missing", "null", "array"))
def test_every_object_boundary_is_closed(path, mutation):
    mapping = deepcopy(_WIRE)
    target = _get(mapping, path)
    if mutation == "extra":
        target["claimed_pass"] = True
    elif mutation == "missing":
        del target[next(iter(target))]
    elif not path:
        mapping = None if mutation == "null" else []
    else:
        _change(mapping, path, None if mutation == "null" else [])
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)


@pytest.mark.parametrize("path", tuple(_paths(_WIRE, int)))
@pytest.mark.parametrize("mutation", ("float", "bool", "str", "null"))
def test_every_integer_rejects_coercion_even_when_python_equality_would_pass(path, mutation):
    mapping = deepcopy(_WIRE)
    original = _get(mapping, path)
    value = {"float": float(original), "bool": bool(original), "str": str(original), "null": None}[mutation]
    _change(mapping, path, value)
    if mutation == "null" and path == ("speech_activity", "segments", 0, "confidence_ppm"):
        assert decode_root_media_evidence_bundle(mapping).speech_activity.segments[0].confidence_ppm is None
        return
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)


@pytest.mark.parametrize("path", tuple(_paths(_WIRE, str)))
@pytest.mark.parametrize("value", (None, "", "\ud800", 1))
def test_every_text_field_rejects_non_wire_strings(path, value):
    mapping = deepcopy(_WIRE)
    _change(mapping, path, value)
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)


@pytest.mark.parametrize("path", tuple(_paths(_WIRE, list)))
@pytest.mark.parametrize("kind", ("tuple", "mapping", "null"))
def test_arrays_must_be_actual_json_arrays(path, kind):
    mapping = deepcopy(_WIRE)
    value = {"tuple": tuple(_get(mapping, path)), "mapping": {}, "null": None}[kind]
    _change(mapping, path, value)
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)


@pytest.mark.parametrize("field", ("boundary_touch_left", "boundary_touch_right", "truncated"))
@pytest.mark.parametrize("value", (0, 1, 0.0, None, "false"))
def test_boolean_flags_are_required_exact_booleans(field, value):
    mapping = deepcopy(_WIRE)
    mapping["transcript"][field] = value
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)
    del mapping["transcript"][field]
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)


@pytest.mark.parametrize("path", [
    ("frame_pts_index", "context", "media_kind"),
    ("frame_pts_index", "coverage", "outcome"),
    ("shot_boundaries", "points", 0, "boundary_type"),
    ("shot_boundaries", "points", 0, "method"),
    ("audio_sample_boundaries", "points", 0, "method"),
    ("audio_sample_boundaries", "source_outcome"),
    ("transcript", "source_outcome"),
    ("transcript", "completeness", "sentence"),
    ("speech_activity", "source_outcome"),
    ("visual_validity", "intervals", 0, "classification"),
    ("subtitle_cues", "required_modes", 0),
    ("subtitle_cues", "successful_modes", 0),
    ("subtitle_cues", "source_outcome"),
    ("subtitle_cues", "cues", 0, "kind"),
    ("subtitle_cues", "cues", 0, "detection_mode"),
])
def test_closed_enums_do_not_accept_unknown_strings(path):
    mapping = deepcopy(_WIRE)
    _change(mapping, path, "unsupported")
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)


@pytest.mark.parametrize("path", tuple(
    path for path in _paths(_WIRE, dict) if path and path[-1] == "time_base"
))
def test_every_clock_requires_reduced_rational_not_numerically_equivalent_input(path):
    mapping = deepcopy(_WIRE)
    clock = _get(mapping, path)
    clock["numerator"] *= 2
    clock["denominator"] *= 2
    with pytest.raises(MediaValidationError, match="reduced"):
        decode_root_media_evidence_bundle(mapping)


@pytest.mark.parametrize("value", [
    {"numerator": 0, "denominator": 1}, {"numerator": 1, "denominator": -2},
    {"numerator": True, "denominator": 1}, {"numerator": 1, "denominator": 2.0},
])
def test_public_time_base_decoder_preserves_domain_rejections(value):
    with pytest.raises(MediaValidationError):
        decode_time_base(value)


@pytest.mark.parametrize("path,value", [
    (("source_sha256",), "sha256:" + "a" * 64),
    (("transcript", "words", 0, "source_sha256"), "sha256:" + "a" * 64),
    (("speech_activity", "segments", 0, "clock_id"), "foreign-clock"),
    (("transcript", "coverage", "in_tick"), 1),
    (("transcript", "coverage", "out_tick"), 101),
    (("frame_pts_index", "pts_index_sha256"), "sha256:" + "b" * 64),
    (("shot_boundaries", "frame_pts_index_set_sha256"), "sha256:" + "b" * 64),
    (("scene_boundaries", "points", 0, "tick"), 1),
    (("subtitle_cues", "cues", 0, "timing_error_bound", "in_tick"), -1),
    (("transcript", "sentences", 0, "word_ids"), ["missing-word"]),
    (("transcript", "segments", 0, "sentence_ids"), ["missing-sentence"]),
    (("speech_activity", "segments", 0, "confidence_ppm"), 1_000_001),
])
def test_changed_identity_coverage_and_nested_relations_reject_in_domain(path, value):
    mapping = deepcopy(_WIRE)
    _change(mapping, path, value)
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)


def test_locally_consistent_foreign_audio_clock_rejects_at_bundle_join():
    mapping = deepcopy(_WIRE)
    speech = mapping["speech_activity"]
    speech["context"]["clock_id"] = speech["coverage"]["clock_id"] = "other-clock"
    for segment in speech["segments"]:
        segment["clock_id"] = "other-clock"
    decode_speech_activity_set(speech)
    with pytest.raises(MediaValidationError, match="same source clock"):
        decode_root_media_evidence_bundle(mapping)


@pytest.mark.parametrize("path", [
    ("frame_pts_index", "pts_index", "ticks"), ("audio_sample_boundaries", "points"),
    ("shot_boundaries", "points"), ("scene_boundaries", "points"),
    ("visual_validity", "intervals"), ("subtitle_cues", "required_modes"),
])
@pytest.mark.parametrize("mutation", ("reverse", "duplicate"))
def test_canonical_collections_are_not_silently_sorted_or_deduplicated(path, mutation):
    mapping = deepcopy(_WIRE)
    target = _get(mapping, path)
    if path == ("scene_boundaries", "points"):
        target.append({**deepcopy(target[0]), "boundary_id": "scene-075", "tick": 75})
        decode_root_media_evidence_bundle(mapping)
    assert len(target) >= 2
    if mutation == "reverse":
        target.reverse()
    else:
        target.insert(0, deepcopy(target[0]))
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)


def test_explicit_no_audio_preserves_empty_arrays_without_fabricating_successful_records():
    original = _bundle()
    no_audio = replace(original, audio_sample_boundaries=replace(
        original.audio_sample_boundaries, source_outcome=AudioSourceOutcome.NOT_APPLICABLE, points=(),
    ), transcript=replace(
        original.transcript, source_outcome=TranscriptSourceOutcome.NOT_APPLICABLE,
        completeness=TranscriptCompleteness(*([EvidenceCompleteness.NOT_APPLICABLE] * 3)),
        words=(), sentences=(), segments=(),
    ), speech_activity=replace(
        original.speech_activity, source_outcome=SpeechSourceOutcome.NOT_APPLICABLE, segments=(),
    ))
    decoded = decode_root_media_evidence_bundle(no_audio.to_mapping())
    assert decoded == no_audio
    assert decoded.audio_sample_boundaries.points == decoded.transcript.words == ()
    broken = no_audio.to_mapping()
    broken["transcript"] = original.transcript.to_mapping()
    with pytest.raises(MediaValidationError, match="not_applicable"):
        decode_root_media_evidence_bundle(broken)


def test_no_lexical_content_retains_vad_and_nullable_confidence_without_sentences():
    original = _bundle()
    vad = replace(original.speech_activity, segments=(replace(
        original.speech_activity.segments[0], confidence_ppm=None,
    ),))
    nonlexical = replace(original, transcript=replace(
        original.transcript, source_outcome=TranscriptSourceOutcome.NO_LEXICAL_CONTENT,
        completeness=TranscriptCompleteness(EvidenceCompleteness.COMPLETE,
                                            EvidenceCompleteness.COMPLETE,
                                            EvidenceCompleteness.NOT_APPLICABLE),
        words=(), segments=(), sentences=(),
    ), speech_activity=vad)
    assert decode_root_media_evidence_bundle(nonlexical.to_mapping()) == nonlexical
    assert nonlexical.transcript.sentences == () and len(nonlexical.speech_activity.segments) == 1
    broken = nonlexical.to_mapping()
    del broken["speech_activity"]["segments"][0]["confidence_ppm"]
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(broken)


def test_no_speech_remains_distinct_from_no_audio_and_vad_only():
    original = _bundle()
    quiet = replace(original, transcript=_no_speech_transcript(original.transcript.context),
                    speech_activity=_no_speech_vad(original.speech_activity.context))
    assert decode_root_media_evidence_bundle(quiet.to_mapping()) == quiet


@pytest.mark.parametrize("outcome", (CoverageOutcome.PARTIAL, CoverageOutcome.FAILED))
def test_partial_or_failed_component_decodes_but_cannot_form_complete_root(outcome):
    original = _bundle()
    coverage = _coverage(original.speech_activity.context, outcome)
    speech = replace(original.speech_activity, coverage=coverage,
                     source_outcome=SpeechSourceOutcome.INDETERMINATE, segments=())
    assert decode_coverage(coverage.to_mapping()) == coverage
    assert decode_speech_activity_set(speech.to_mapping()) == speech
    mapping = original.to_mapping()
    mapping["speech_activity"] = speech.to_mapping()
    with pytest.raises(MediaValidationError, match="complete coverage"):
        decode_root_media_evidence_bundle(mapping)


def test_negative_origin_and_large_integer_are_preserved_without_float_or_safeint_conversion():
    frame = _bundle().frame_pts_index
    origin = -(2**54 + 3)
    context = replace(frame.context, origin_tick=origin)
    coverage = replace(frame.coverage, in_tick=origin, out_tick=origin + 100)
    index = PTSIndex((origin, origin + 50, origin + 100))
    expected = replace(frame, context=context, coverage=coverage, pts_index=index,
                       pts_index_sha256=canonical_sha256(list(index.ticks)))
    decoded = decode_frame_pts_index_set(json.loads(_raw(expected.to_mapping())))
    assert decoded == expected
    assert _raw(decoded.to_mapping()) == _raw(expected.to_mapping())


def test_unicode_text_roundtrip_and_manifest_hash_change_are_values_not_authority():
    original = _bundle()
    mapping = original.to_mapping()
    mapping["transcript"]["words"][0]["text"] = "中文 😀"
    mapping["source_manifest_sha256"] = "sha256:" + "9" * 64
    raw = _raw(mapping)
    decoded = decode_root_media_evidence_bundle_json(raw, max_bytes=len(raw))
    assert _raw(decoded.to_mapping()) == raw
    assert decoded.canonical_hash != original.canonical_hash
    # The codec cannot certify this manifest hash or the truth of transcribed text.


@pytest.mark.parametrize("raw", [
    b"", b"\xff", b"{} trailing", b"[]", b"null", b'{"a":1,"a":2}',
    b'{"nested":{"a":1,"a":2}}', b'{"a":NaN}', b'{"a":Infinity}',
    b'{"a":-Infinity}', b'{"a":1.0}', b'{"a":1e0}', b"[" * 2000 + b"]" * 2000,
])
def test_raw_entry_rejects_non_json_duplicates_floats_and_recursive_inputs(raw):
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle_json(raw, max_bytes=100_000)


@pytest.mark.parametrize("path", [
    ("root_media_evidence_bundle_id",),
    ("transcript", "words", 0, "word_id"),
])
def test_duplicate_keys_inside_otherwise_valid_bundle_are_rejected(path):
    raw = _raw(_WIRE)
    field = _raw({path[-1]: _get(_WIRE, path)})[1:-1]
    assert raw.count(field) == 1
    duplicate = raw.replace(field, field + b"," + field)
    with pytest.raises(MediaValidationError, match="duplicate"):
        decode_root_media_evidence_bundle_json(duplicate, max_bytes=len(duplicate))


@pytest.mark.parametrize("limit", (0, -1, True, 100.0, None))
def test_raw_entry_requires_explicit_positive_integer_limit(limit):
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle_json(_raw(_WIRE), max_bytes=limit)


def test_raw_byte_bound_exact_boundary_and_formatted_json():
    raw = _raw(_WIRE)
    with pytest.raises(MediaValidationError, match="byte limit"):
        decode_root_media_evidence_bundle_json(raw, max_bytes=len(raw) - 1)
    assert decode_root_media_evidence_bundle_json(raw, max_bytes=len(raw)) == _bundle()
    pretty = json.dumps(_WIRE, indent=2).encode() + b"\n"
    assert decode_root_media_evidence_bundle_json(pretty, max_bytes=len(pretty)) == _bundle()


def test_python_subclasses_are_not_json_wire_types():
    class String(str):
        pass

    class Dictionary(dict):
        pass

    class Array(list):
        pass

    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(Dictionary(_WIRE))
    mapping = deepcopy(_WIRE)
    mapping["source_id"] = String(mapping["source_id"])
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)
    mapping = deepcopy(_WIRE)
    mapping["transcript"]["words"] = Array(mapping["transcript"]["words"])
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(mapping)
