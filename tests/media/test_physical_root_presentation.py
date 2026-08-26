"""Physical-only roots share the exact probe compiler, never speech admission."""

from dataclasses import replace
from typing import cast

import pytest
from autocut_kernel.media.physical_root import PhysicalRootMediaEvidence
from autocut_kernel.media.presentation_evidence_codec import (
    decode_committed_video_to_audio_clock_map_certificate,
)
from autocut_kernel.media.stage4_predecessor import (
    Stage4PredecessorError,
    derive_presentation_timeline_facts,
)

from tests.media.test_physical_presentation_map import _numerical_case


def _physical(root) -> PhysicalRootMediaEvidence:
    return PhysicalRootMediaEvidence(
        "physical-presentation-test", root.source_id, root.source_sha256,
        root.source_manifest_sha256, root.root_input_manifest_sha256,
        root.frame_pts_index, root.shot_boundaries, root.scene_boundaries,
        root.audio_sample_boundaries, root.visual_validity, root.subtitle_cues,
    )


@pytest.mark.parametrize("options", (
    {}, {"gap": (32, 64)}, {"audio_end": 104, "audio_ticks": (0, 8, 32, 64, 96, 104)},
))
def test_physical_compiler_preserves_exact_map_and_binds_distinct_root(options):
    original = _numerical_case(**options)
    root = _physical(original.root)
    probe, certificate = derive_presentation_timeline_facts(
        root, probe=original.probe,
        source_manifest_sha256=original.source_manifest_sha256,
        audio_snap_calibration=original.audio_snap_calibration,
    )
    assert probe is original.probe
    assert certificate == replace(original.certificate, root_evidence_sha256=root.canonical_hash)
    assert certificate.canonical_hash != original.certificate.canonical_hash
    decoded = decode_committed_video_to_audio_clock_map_certificate(certificate.to_mapping())
    decoded.assert_replays_probe(
        probe, root, source_manifest_sha256=original.source_manifest_sha256,
        calibration_binding=original.audio_snap_calibration,
    )
    # Same physical endpoints do not make the aggregate roots interchangeable.
    with pytest.raises(Stage4PredecessorError, match="does not bind"):
        decoded.assert_replays_probe(
            probe, original.root, source_manifest_sha256=original.source_manifest_sha256,
            calibration_binding=original.audio_snap_calibration,
        )


@pytest.mark.parametrize("mutation", ("root", "probe", "calibration", "manifest"))
def test_physical_certificate_retains_all_predecessor_bindings(mutation):
    original = _numerical_case()
    root = _physical(original.root)
    probe, certificate = derive_presentation_timeline_facts(
        root, probe=original.probe,
        source_manifest_sha256=original.source_manifest_sha256,
        audio_snap_calibration=original.audio_snap_calibration,
    )
    calibration = original.audio_snap_calibration
    manifest = original.source_manifest_sha256
    if mutation == "root":
        root = replace(root, physical_root_id="another-physical-root")
    elif mutation == "probe":
        probe = replace(probe, source_blob_byte_length=probe.source_blob_byte_length + 1)
    elif mutation == "calibration":
        calibration = replace(calibration, timing_error_bound_tick=calibration.timing_error_bound_tick + 1)
    else:
        manifest = "sha256:" + "f" * 64
    with pytest.raises(Stage4PredecessorError):
        certificate.assert_replays_probe(
            probe, root, source_manifest_sha256=manifest, calibration_binding=calibration,
        )


def test_physical_compiler_does_not_accept_structural_lookalikes():
    original = _numerical_case()
    with pytest.raises(Stage4PredecessorError, match="exact root"):
        derive_presentation_timeline_facts(
            cast(PhysicalRootMediaEvidence, object()), probe=original.probe,
            source_manifest_sha256=original.source_manifest_sha256,
            audio_snap_calibration=original.audio_snap_calibration,
        )
