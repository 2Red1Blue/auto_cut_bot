"""Pure synthetic clock/fact vectors; no Store acceptance or native decoder."""

from dataclasses import replace
from fractions import Fraction

import pytest
from autocut_kernel.media.audio_stream_facts import AudioStreamFacts, SelectedAudioStreamMetadata
from autocut_kernel.media.local_audio_window import LocalAudioWindowError
from autocut_kernel.media.root_evidence import VisualClassification
from autocut_kernel.media.stage4_predecessor import derive_presentation_timeline_facts
from autocut_kernel.media.timed_evidence import CandidateEvidenceWindow
from autocut_kernel.media.types import TickRange, TimeBase, canonical_sha256
from autocut_kernel.physical_edit.local_audio_window import derive_local_audio_window_spec
from autocut_kernel.physical_edit.presentation_map import ReplayedPresentationMap

from tests.media.test_physical_presentation_map import _numerical_case
from tests.media.test_physical_root_presentation import _physical

HASH = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def _case(start=15, end=60, *, sample_rate=96000, channels=2, **options):
    original = _numerical_case(**options)
    root = _physical(original.root)
    audio = root.audio_sample_boundaries
    clock_id = "audio-stream-1"
    audio = replace(audio, context=replace(audio.context, clock_id=clock_id),
                    coverage=replace(audio.coverage, clock_id=clock_id),
                    points=tuple(replace(point, clock_id=clock_id) for point in audio.points))
    root = replace(root, audio_sample_boundaries=audio)
    probe = replace(original.probe,
                    audio=replace(original.probe.audio, clock_id=clock_id, index_sha256=audio.canonical_hash),
                    audio_sample_boundary_set_sha256=audio.canonical_hash)
    probe, certificate = derive_presentation_timeline_facts(
        root, probe=probe, source_manifest_sha256=original.source_manifest_sha256,
        audio_snap_calibration=original.audio_snap_calibration,
    )
    domain = ReplayedPresentationMap(root, probe, certificate, original.source_manifest_sha256,
                                    original.audio_snap_calibration)
    metadata = SelectedAudioStreamMetadata(1, audio.context.time_base, audio.context.origin_tick, sample_rate, channels)
    facts = AudioStreamFacts(root.source_id, root.source_sha256, 1, clock_id, audio.context.time_base,
                            audio.context.origin_tick, audio.context.end_tick, sample_rate, channels,
                            audio.canonical_hash, metadata, metadata.canonical_hash,
                            canonical_sha256(probe.probe_execution.to_mapping()))
    video = root.frame_pts_index.context
    window = CandidateEvidenceWindow(root.source_id, root.source_sha256, video.clock_id, video.time_base,
                                    TickRange(video.origin_tick, video.end_tick), HASH, HASH, HASH,
                                    root.frame_pts_index.canonical_hash, TickRange(start, end), TickRange(start, end), 0)
    return window, domain, facts


def _derive(window, domain, facts, **overrides):
    options = {"decoder_identity_sha256": HASH, "max_outward_padding_audio_ticks": 100,
               "max_source_bytes": 1000000, "max_decode_frames": 1000,
               "max_frame_bytes": 1000000, "max_pcm_bytes": 1000000}
    options.update(overrides)
    return derive_local_audio_window_spec(window, domain, facts, **options)


def test_different_clocks_measured_rate_stereo_and_deterministic_replay():
    case = _case()
    window, domain, facts = case
    spec = _derive(*case, max_outward_padding_audio_ticks=0)
    assert spec.requested_range == TickRange(8, 32)
    assert spec.sample_rate == 96000 != spec.time_base.denominator
    assert spec.channels == 2 and spec.expected_samples == 48
    assert spec.source_range == TickRange(0, 96) and spec.clock_id == "audio-stream-1"
    assert spec.audio_boundary_set_sha256 == domain.root.audio_sample_boundaries.canonical_hash
    assert spec.source_id == window.source_id and spec.source_sha256 == facts.source_sha256
    assert spec == _derive(*case, max_outward_padding_audio_ticks=0)
    assert spec.canonical_hash == _derive(*case, max_outward_padding_audio_ticks=0).canonical_hash


def test_nearest_outward_actual_boundaries_not_floor_ceil_or_inner_rounding():
    case = _case(16, 119, frame_ticks=(0, 15, 16, 60, 119, 120, 180))
    spec = _derive(*case, max_outward_padding_audio_ticks=1)
    assert spec.requested_range == TickRange(8, 64)
    assert case[1].map_video_tick_bounds(16) == (8, 9)
    assert case[1].map_video_tick_bounds(119) == (63, 64)
    with pytest.raises(LocalAudioWindowError, match="exact outward padding"):
        _derive(*case, max_outward_padding_audio_ticks=0)


@pytest.mark.parametrize("start,end", [(14, 60), (15, 61)])
def test_rational_padding_threshold_includes_fraction_beyond_floor_or_ceil(start, end):
    case = _case(start, end, frame_ticks=(0, 14, 15, 60, 61, 120, 180),
                 audio_ticks=(0, 6, 8, 32, 34, 64, 96))
    spec = _derive(*case, max_outward_padding_audio_ticks=2)
    assert spec.requested_range == TickRange(6 if start == 14 else 8, 34 if end == 61 else 32)
    # Difference from floor(start) or ceil(end) is only one tick. Actual
    # rational outward padding is 22/15 ticks and must exceed a bound of one.
    assert Fraction(22, 15) > 1
    with pytest.raises(LocalAudioWindowError, match="exact outward padding"):
        _derive(*case, max_outward_padding_audio_ticks=1)


def test_negative_fractional_origins_remain_absolute():
    case = _case(-46, 15, video_origin=-90, audio_origin=-48,
                 frame_ticks=(-90, -46, -45, 0, 15, 60, 120, 180),
                 audio_ticks=(-48, -25, -24, 0, 8, 32, 64, 96),
                 visual=((-90, 180, VisualClassification.VALID_CONTENT),))
    spec = _derive(*case, max_outward_padding_audio_ticks=1)
    assert spec.requested_range == TickRange(-25, 8)
    assert spec.source_range == TickRange(-48, 96)
    assert spec.expected_samples == 66
    with pytest.raises(LocalAudioWindowError, match="exact outward padding"):
        _derive(*case, max_outward_padding_audio_ticks=0)


def test_full_video_end_sentinel_need_not_be_a_regular_frame():
    spec = _derive(*_case(0, 180, frame_ticks=(0, 15, 60, 120)), max_outward_padding_audio_ticks=0)
    assert spec.requested_range == TickRange(0, 96)


@pytest.mark.parametrize("start,end", [(60, 120), (15, 180)])
def test_endpoint_mapping_does_not_bridge_internal_gap(start, end):
    case = _case(start, end, gap=(32, 64))
    with pytest.raises(ValueError, match="crosses a gap"):
        _derive(*case)


def test_nearest_outward_endpoint_cannot_stretch_past_video_tail():
    case = _case(15, 180, audio_end=104, audio_ticks=(0, 8, 32, 64, 104))
    with pytest.raises(ValueError, match="single-stream non-overlap"):
        _derive(*case)


def test_short_audio_tail_does_not_cover_later_video_endpoint():
    case = _case(15, 180, audio_end=80, audio_ticks=(0, 8, 32, 64, 80))
    with pytest.raises(ValueError, match="outside common"):
        _derive(*case)


@pytest.mark.parametrize("start,end", [(14, 60), (15, 61)])
def test_candidate_current_boundaries_must_be_actual_video_points(start, end):
    with pytest.raises(ValueError, match="decoded frame"):
        _derive(*_case(start, end))


@pytest.mark.parametrize("field,value", [
    ("source_id", "foreign"), ("source_sha256", OTHER), ("source_clock_id", "foreign"),
    ("source_time_base", TimeBase(1, 90001)), ("source_range", TickRange(0, 179)),
    ("frame_pts_index_set_sha256", OTHER),
])
def test_foreign_candidate_bindings_are_rejected(field, value):
    window, domain, facts = _case()
    with pytest.raises(LocalAudioWindowError, match="exact source video"):
        _derive(replace(window, **{field: value}), domain, facts)


@pytest.mark.parametrize("field,value", [
    ("source_id", "foreign"), ("source_sha256", OTHER), ("end_tick", 97),
    ("audio_sample_boundary_set_sha256", OTHER), ("probe_execution_sha256", OTHER),
])
def test_foreign_facts_index_and_execution_are_rejected(field, value):
    window, domain, facts = _case()
    with pytest.raises(ValueError, match="exact probe evidence"):
        _derive(window, domain, replace(facts, **{field: value}))


def test_consistently_rehashed_wrong_selected_stream_is_rejected():
    window, domain, facts = _case()
    metadata = replace(facts.selected_audio_metadata, stream_index=2)
    foreign = replace(facts, stream_index=2, clock_id="audio-stream-2",
                      selected_audio_metadata=metadata, selected_audio_metadata_sha256=metadata.canonical_hash)
    with pytest.raises(ValueError, match="exact probe evidence"):
        _derive(window, domain, foreign)


def test_recompiled_foreign_probe_still_cannot_use_original_native_facts():
    window, domain, facts = _case()
    execution = replace(domain.probe.probe_execution, normalized_output_sha256=OTHER)
    probe = replace(domain.probe, probe_execution=execution)
    probe, certificate = derive_presentation_timeline_facts(
        domain.root, probe=probe, source_manifest_sha256=domain.source_manifest_sha256,
        audio_snap_calibration=domain.audio_snap_calibration,
    )
    changed = replace(domain, probe=probe, certificate=certificate)
    with pytest.raises(ValueError, match="exact probe evidence"):
        _derive(window, changed, facts)


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("value", [None, {}, object()])
def test_required_values_are_exact_not_lookalikes(position, value):
    args = list(_case())
    args[position] = value
    with pytest.raises(LocalAudioWindowError, match="exact candidate/map/audio"):
        _derive(*args)


@pytest.mark.parametrize("value", [-1, True, 1.0, "1", None])
def test_padding_is_an_explicit_nonnegative_integer(value):
    with pytest.raises(LocalAudioWindowError, match="nonnegative integer"):
        _derive(*_case(), max_outward_padding_audio_ticks=value)


@pytest.mark.parametrize("name", ["max_source_bytes", "max_decode_frames", "max_frame_bytes", "max_pcm_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.0])
def test_resource_limits_are_explicit_strict_positive_integers(name, value):
    with pytest.raises(LocalAudioWindowError, match="positive integer"):
        _derive(*_case(), **{name: value})


def test_existing_spec_rejects_nonintegral_sample_count_and_pcm_overflow():
    with pytest.raises(LocalAudioWindowError, match="integral sample count"):
        _derive(*_case(sample_rate=44100))
    with pytest.raises(LocalAudioWindowError, match="FLOAT PCM"):
        _derive(*_case(), max_pcm_bytes=383)
    assert _derive(*_case(), max_pcm_bytes=384).expected_samples == 48


def test_decoder_hash_validation_and_no_implicit_limits():
    with pytest.raises(ValueError):
        _derive(*_case(), decoder_identity_sha256="wrong")
    with pytest.raises(TypeError):
        derive_local_audio_window_spec(*_case())
