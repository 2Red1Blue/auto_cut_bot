"""Pure value/codec tests; no native execution or Store acceptance claim."""

from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest
from autocut_kernel.media.audio_stream_facts import (
    AudioStreamFacts,
    SelectedAudioStreamMetadata,
    decode_audio_stream_facts,
)
from autocut_kernel.media.types import MediaValidationError, TimeBase, canonical_sha256

HASH = "sha256:" + "a" * 64


def _facts() -> AudioStreamFacts:
    metadata = SelectedAudioStreamMetadata(1, TimeBase(1, 1000), -10, 48000, 2)
    return AudioStreamFacts(
        "音频源",
        HASH,
        1,
        "audio-stream-1",
        TimeBase(1, 1000),
        -10,
        100,
        48000,
        2,
        HASH,
        metadata,
        metadata.canonical_hash,
        HASH,
    )


def test_exact_roundtrip_preserves_native_layout_not_time_base_reciprocal():
    facts = _facts()
    assert decode_audio_stream_facts(facts.to_mapping()) == facts
    assert facts.canonical_hash == canonical_sha256(facts.to_mapping())
    assert facts.sample_rate != facts.time_base.denominator
    assert facts.channels == 2 and facts.origin_tick == -10
    with pytest.raises(FrozenInstanceError):
        facts.channels = 1
    with pytest.raises(FrozenInstanceError):
        facts.selected_audio_metadata.channels = 1


@pytest.mark.parametrize(
    "field", ["sample_rate", "channels", "stream_index", "origin_tick", "end_tick"]
)
@pytest.mark.parametrize("invalid", [None, True, 1.0, "1", [], {}])
def test_integer_fields_are_exact_in_direct_and_wire_construction(field, invalid):
    with pytest.raises(MediaValidationError):
        replace(_facts(), **{field: invalid})
    raw = _facts().to_mapping()
    raw[field] = invalid
    with pytest.raises(MediaValidationError):
        decode_audio_stream_facts(raw)


@pytest.mark.parametrize("field", ["sample_rate", "channels"])
@pytest.mark.parametrize("invalid", [0, -1, True, 1.0, "48000", None])
def test_nested_metadata_requires_positive_native_integers(field, invalid):
    with pytest.raises(MediaValidationError):
        replace(_facts().selected_audio_metadata, **{field: invalid})
    raw = _facts().to_mapping()
    cast(dict[str, object], raw["selected_audio_metadata"])[field] = invalid
    with pytest.raises(MediaValidationError):
        decode_audio_stream_facts(raw)


@pytest.mark.parametrize("nested", [False, True])
def test_all_fields_required_and_no_unrecognized_or_self_pass_fields(nested):
    base = _facts().to_mapping()
    node = cast(dict[str, object], base["selected_audio_metadata"]) if nested else base
    for field in tuple(node):
        raw = _facts().to_mapping()
        target = cast(dict[str, object], raw["selected_audio_metadata"]) if nested else raw
        del target[field]
        with pytest.raises(MediaValidationError):
            decode_audio_stream_facts(raw)
    for extra in ("unknown", "accepted", "executed", "pass"):
        raw = _facts().to_mapping()
        target = cast(dict[str, object], raw["selected_audio_metadata"]) if nested else raw
        target[extra] = True
        with pytest.raises(MediaValidationError):
            decode_audio_stream_facts(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", ""),
        ("source_id", "\ud800"),
        ("source_id", True),
        ("source_sha256", HASH.upper()),
        ("clock_id", "audio-stream-2"),
        ("stream_index", -1),
        ("end_tick", -10),
        ("time_base", {"numerator": 1}),
        ("sample_rate", 0),
        ("channels", 0),
        ("probe_execution_sha256", "bad"),
        ("selected_audio_metadata_sha256", "sha256:" + "b" * 64),
        ("selected_audio_metadata", {}),
    ],
)
def test_direct_invalid_facts_fail_closed(field, value):
    with pytest.raises(MediaValidationError):
        replace(_facts(), **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("stream_index", 2),
        ("time_base", TimeBase(1, 48000)),
        ("declared_start_tick", 0),
        ("sample_rate", 44100),
        ("channels", 1),
    ],
)
def test_rehashed_metadata_must_still_match_outer_facts(field, value):
    metadata = replace(_facts().selected_audio_metadata, **{field: value})
    with pytest.raises(MediaValidationError, match="does not bind"):
        replace(
            _facts(),
            selected_audio_metadata=metadata,
            selected_audio_metadata_sha256=metadata.canonical_hash,
        )


@pytest.mark.parametrize("field,value", [("sample_rate", 44100), ("channels", 1)])
def test_native_layout_is_in_new_preimage(field, value):
    original = _facts()
    metadata = replace(original.selected_audio_metadata, **{field: value})
    changed = replace(
        original,
        **{field: value},
        selected_audio_metadata=metadata,
        selected_audio_metadata_sha256=metadata.canonical_hash,
    )
    assert changed.canonical_hash != original.canonical_hash
    assert metadata.canonical_hash != original.selected_audio_metadata_sha256
    assert decode_audio_stream_facts(changed.to_mapping()) == changed


@pytest.mark.parametrize("value", [None, [], True, "{}", 1])
def test_leaf_rejects_non_objects(value):
    with pytest.raises(MediaValidationError):
        decode_audio_stream_facts(value)


@pytest.mark.parametrize(
    "nested,field,value",
    [
        (False, "schema_version", "audio-stream-facts-v2"),
        (True, "schema_version", "unknown"),
        (True, "codec_type", "video"),
        (True, "time_base", {"numerator": True, "denominator": 1000}),
        (False, "time_base", {"numerator": 2, "denominator": 2000}),
    ],
)
def test_schema_and_time_base_are_closed(nested, field, value):
    raw = _facts().to_mapping()
    target = cast(dict[str, object], raw["selected_audio_metadata"]) if nested else raw
    target[field] = value
    with pytest.raises(MediaValidationError):
        decode_audio_stream_facts(raw)
