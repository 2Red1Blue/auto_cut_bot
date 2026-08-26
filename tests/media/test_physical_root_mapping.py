"""Physical-only roots retain exact A/V mapping, without speech admission."""

from dataclasses import replace

import pytest
from autocut_kernel.media.stage4_predecessor import derive_presentation_timeline_facts
from autocut_kernel.media.types import TickRange
from autocut_kernel.physical_edit.presentation_map import PresentationMapValidationError

from tests.media.test_physical_presentation_map import _numerical_case
from tests.media.test_physical_root_presentation import _physical


def _domain(**options):
    original = _numerical_case(**options)
    root = _physical(original.root)
    probe, certificate = derive_presentation_timeline_facts(
        root, probe=original.probe,
        source_manifest_sha256=original.source_manifest_sha256,
        audio_snap_calibration=original.audio_snap_calibration,
    )
    return replace(original, root=root, probe=probe, certificate=certificate)


def test_physical_root_supports_exact_clock_conversion():
    domain = _domain()
    assert domain.map_video_tick_bounds(15) == (8, 8)
    assert domain.require_av_span_covered(TickRange(15, 180), TickRange(8, 96)) == 0


def test_physical_root_mapping_preserves_gap_rejection():
    domain = _domain(gap=(32, 64))
    assert domain.map_video_tick_bounds(60) == (32, 32)
    assert domain.map_video_tick_bounds(120) == (64, 64)
    with pytest.raises(PresentationMapValidationError, match="crosses a gap"):
        domain.require_av_span_covered(TickRange(60, 120), TickRange(32, 64))


def test_physical_root_mapping_does_not_stretch_audio_tail():
    domain = _domain(audio_end=104, audio_ticks=(0, 8, 32, 64, 96, 104))
    with pytest.raises(PresentationMapValidationError, match="single-stream non-overlap"):
        domain.require_av_span_covered(TickRange(0, 180), TickRange(0, 104))


def test_physical_root_mapping_requires_real_endpoints():
    domain = _domain()
    with pytest.raises(PresentationMapValidationError, match="decoded frame"):
        domain.map_video_tick_bounds(14)
    with pytest.raises(PresentationMapValidationError, match="decoded sample"):
        domain.require_av_span_covered(TickRange(0, 60), TickRange(0, 31))


@pytest.mark.parametrize("field", ("root", "probe", "certificate", "audio_snap_calibration"))
def test_physical_root_mapping_rejects_structural_lookalikes(field):
    with pytest.raises(PresentationMapValidationError, match="exact evidence"):
        replace(_domain(), **{field: object()})


def test_physical_root_mapping_does_not_accept_old_root_certificate():
    with pytest.raises(PresentationMapValidationError, match="does not replay"):
        replace(_domain(), certificate=_numerical_case().certificate)
