from __future__ import annotations

import pytest
from autocut_kernel.media.root_evidence import FramePtsIndexSet
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.vlm import (
    ProxyTimelineMap,
    ProxyTimelineSegment,
    VlmValidationError,
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
    select_core_owner,
)

from . import frame_pts_set


def _hash(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _window(
    *,
    source_range: TickRange,
    core_range: TickRange,
    source_start: int,
    frame_source_pts: int,
    frame_proxy_pts: int,
    exact_frame_pts: FramePtsIndexSet | None = None,
) -> WindowManifest:
    time_base = TimeBase(1, 1_000)
    timeline = ProxyTimelineMap.translation(
        time_base=time_base,
        proxy_range=TickRange(0, source_range.duration_pts),
        source_start_pts=source_start,
    )
    root_frame_pts = exact_frame_pts or frame_pts_set(
        source_id="source-001",
        source_sha256=_hash("a"),
        clock_id="video-clock-0",
        time_base=time_base,
        origin_tick=source_range.start_pts,
        end_tick=source_range.end_pts,
        ticks=(frame_source_pts,),
    )
    return WindowManifest(
        source_id="source-001",
        source_clock_id="video-clock-0",
        source_sha256=_hash("a"),
        stream_index=2,
        source_time_base=time_base,
        source_range=source_range,
        core_range=core_range,
        frame_pts_index_set=root_frame_pts,
        proxy_blob_ref=WindowProxyBlobRef(
            f"proxy-{source_start}",
            _hash("b"),
            4_096,
            "video/mp4",
        ),
        preprocess_policy_sha256=_hash("c"),
        window_sampling_policy_sha256=_hash("e"),
        timeline_map=timeline,
        frame_samples=(WindowFrameSample(frame_source_pts, frame_proxy_pts, _hash("d")),),
    )


def test_non_zero_vfr_frame_samples_remain_exact_and_canonical() -> None:
    time_base = TimeBase(1, 90_000)
    timeline = ProxyTimelineMap.translation(
        time_base=time_base,
        proxy_range=TickRange(0, 9_000),
        source_start_pts=180_000,
    )
    manifest = WindowManifest(
        source_id="source-001",
        source_clock_id="video-clock-3",
        source_sha256=_hash("a"),
        stream_index=3,
        source_time_base=time_base,
        source_range=TickRange(180_000, 189_000),
        core_range=TickRange(181_000, 188_000),
        frame_pts_index_set=frame_pts_set(
            source_id="source-001",
            source_sha256=_hash("a"),
            clock_id="video-clock-3",
            time_base=time_base,
            origin_tick=180_000,
            end_tick=189_000,
            ticks=(180_101, 180_997, 183_411, 188_777),
        ),
        proxy_blob_ref=WindowProxyBlobRef("proxy-vfr", _hash("b"), 8_192, "video/mp4"),
        preprocess_policy_sha256=_hash("c"),
        window_sampling_policy_sha256=_hash("2"),
        timeline_map=timeline,
        frame_samples=(
            WindowFrameSample(180_101, 101, _hash("d")),
            WindowFrameSample(180_997, 997, _hash("e")),
            WindowFrameSample(183_411, 3_411, _hash("f")),
            WindowFrameSample(188_777, 8_777, _hash("1")),
        ),
    )

    assert tuple(item.source_pts for item in manifest.frame_samples) == (180_101, 180_997, 183_411, 188_777)
    assert manifest.window_id == manifest.canonical_hash
    assert len(manifest.frame_by_id) == 4


def test_window_rejects_mapped_vfr_tick_missing_from_exact_root_frame_index() -> None:
    exact_frame_pts = frame_pts_set(
        source_id="source-001",
        source_sha256=_hash("a"),
        clock_id="video-clock-0",
        time_base=TimeBase(1, 1_000),
        origin_tick=100,
        end_tick=250,
        ticks=(100, 191, 249),
    )

    with pytest.raises(VlmValidationError, match="member of the exact FramePtsIndexSet"):
        _window(
            source_range=TickRange(100, 250),
            core_range=TickRange(100, 250),
            source_start=100,
            frame_source_pts=190,
            frame_proxy_pts=90,
            exact_frame_pts=exact_frame_pts,
        )


def test_piecewise_map_propagates_provider_and_mapping_error_conservatively() -> None:
    time_base = TimeBase(1, 1_000)
    timeline = ProxyTimelineMap(
        proxy_time_base=time_base,
        source_time_base=time_base,
        segments=(
            ProxyTimelineSegment(TickRange(0, 10), TickRange(100, 120), 1),
            ProxyTimelineSegment(TickRange(10, 20), TickRange(120, 150), 2),
        ),
        certificate_kind="piecewise_monotonic",
    )

    mapped = timeline.map_interval(TickRange(8, 12), provider_uncertainty_proxy_pts=1)

    assert mapped.coarse_range == TickRange(113, 131)
    assert mapped.mapping_error_bound_source_pts == 2
    assert mapped.provider_uncertainty_proxy_pts == 1
    assert mapped.proxy_time_base == time_base
    assert mapped.source_time_base == time_base


def test_piecewise_map_keeps_proxy_and_source_uncertainty_units_for_different_time_bases() -> None:
    proxy_time_base = TimeBase(1, 1_000)
    source_time_base = TimeBase(1, 90_000)
    timeline = ProxyTimelineMap(
        proxy_time_base=proxy_time_base,
        source_time_base=source_time_base,
        segments=(ProxyTimelineSegment(TickRange(0, 100), TickRange(180_000, 189_000), 90),),
        certificate_kind="piecewise_monotonic",
    )

    mapped = timeline.map_interval(
        TickRange(10, 20),
        provider_uncertainty_proxy_pts=2,
    )

    assert mapped.provider_uncertainty_proxy_pts == 2
    assert mapped.proxy_time_base == proxy_time_base
    assert mapped.mapping_error_bound_source_pts == 90
    assert mapped.source_time_base == source_time_base
    mapping = mapped.to_mapping()
    assert mapping["provider_uncertainty"] == {
        "clock": "proxy",
        "tick": 2,
        "time_base": {"denominator": 1_000, "numerator": 1},
    }


def test_translation_certificate_rejects_scale_or_time_base_change() -> None:
    with pytest.raises(VlmValidationError, match="identical time bases"):
        ProxyTimelineMap(
            proxy_time_base=TimeBase(1, 1_000),
            source_time_base=TimeBase(1, 90_000),
            segments=(ProxyTimelineSegment(TickRange(0, 10), TickRange(100, 110), 0),),
            certificate_kind="translation_certificate",
        )
    with pytest.raises(VlmValidationError, match="equal tick durations"):
        ProxyTimelineMap(
            proxy_time_base=TimeBase(1, 1_000),
            source_time_base=TimeBase(1, 1_000),
            segments=(ProxyTimelineSegment(TickRange(0, 10), TickRange(100, 120), 0),),
            certificate_kind="translation_certificate",
        )


def test_piecewise_map_rejects_proxy_gaps_source_gaps_and_source_reversal() -> None:
    time_base = TimeBase(1, 1_000)
    with pytest.raises(VlmValidationError, match="contiguous"):
        ProxyTimelineMap(
            time_base,
            time_base,
            (
                ProxyTimelineSegment(TickRange(0, 10), TickRange(100, 120), 0),
                ProxyTimelineSegment(TickRange(11, 20), TickRange(120, 140), 0),
            ),
            "piecewise_monotonic",
        )
    with pytest.raises(VlmValidationError, match="source segments"):
        ProxyTimelineMap(
            time_base,
            time_base,
            (
                ProxyTimelineSegment(TickRange(0, 10), TickRange(100, 120), 0),
                ProxyTimelineSegment(TickRange(10, 20), TickRange(121, 140), 0),
            ),
            "piecewise_monotonic",
        )
    with pytest.raises(VlmValidationError, match="source segments"):
        ProxyTimelineMap(
            time_base,
            time_base,
            (
                ProxyTimelineSegment(TickRange(0, 10), TickRange(100, 130), 0),
                ProxyTimelineSegment(TickRange(10, 20), TickRange(120, 140), 0),
            ),
            "piecewise_monotonic",
        )


def test_core_ownership_uses_one_deterministic_lower_midpoint_owner() -> None:
    exact_frame_pts = frame_pts_set(
        source_id="source-001",
        source_sha256=_hash("a"),
        clock_id="video-clock-0",
        time_base=TimeBase(1, 1_000),
        origin_tick=100,
        end_tick=300,
        ticks=(190, 210),
    )
    first = _window(
        source_range=TickRange(100, 250),
        core_range=TickRange(100, 200),
        source_start=100,
        frame_source_pts=190,
        frame_proxy_pts=90,
        exact_frame_pts=exact_frame_pts,
    )
    second = _window(
        source_range=TickRange(150, 300),
        core_range=TickRange(200, 300),
        source_start=150,
        frame_source_pts=210,
        frame_proxy_pts=60,
        exact_frame_pts=exact_frame_pts,
    )

    manifest_set = WindowManifestSet(
        source_id="source-001",
        source_clock_id="video-clock-0",
        source_sha256=_hash("a"),
        stream_index=2,
        source_time_base=TimeBase(1, 1_000),
        declared_source_range=TickRange(100, 300),
        manifests=(first, second),
    )
    owner = select_core_owner(manifest_set, TickRange(190, 211))

    assert owner.window_id == second.window_id
    assert not first.owns_interval(TickRange(190, 211))
    assert second.owns_interval(TickRange(190, 211))


def test_manifest_set_fails_closed_for_gap_overlap_and_wrong_order() -> None:
    exact_frame_pts = frame_pts_set(
        source_id="source-001",
        source_sha256=_hash("a"),
        clock_id="video-clock-0",
        time_base=TimeBase(1, 1_000),
        origin_tick=100,
        end_tick=300,
        ticks=(180, 190, 210),
    )
    first = _window(
        source_range=TickRange(100, 250),
        core_range=TickRange(100, 220),
        source_start=100,
        frame_source_pts=190,
        frame_proxy_pts=90,
        exact_frame_pts=exact_frame_pts,
    )
    second = _window(
        source_range=TickRange(150, 300),
        core_range=TickRange(180, 300),
        source_start=150,
        frame_source_pts=210,
        frame_proxy_pts=60,
        exact_frame_pts=exact_frame_pts,
    )
    common = {
        "source_id": "source-001",
        "source_clock_id": "video-clock-0",
        "source_sha256": _hash("a"),
        "stream_index": 2,
        "source_time_base": TimeBase(1, 1_000),
        "declared_source_range": TickRange(100, 300),
    }
    with pytest.raises(VlmValidationError, match="gap-free"):
        WindowManifestSet(manifests=(first, second), **common)

    gap_first = _window(
        source_range=TickRange(100, 250),
        core_range=TickRange(100, 190),
        source_start=100,
        frame_source_pts=180,
        frame_proxy_pts=80,
        exact_frame_pts=exact_frame_pts,
    )
    gap_second = _window(
        source_range=TickRange(150, 300),
        core_range=TickRange(200, 300),
        source_start=150,
        frame_source_pts=210,
        frame_proxy_pts=60,
        exact_frame_pts=exact_frame_pts,
    )
    with pytest.raises(VlmValidationError, match="gap-free"):
        WindowManifestSet(manifests=(gap_first, gap_second), **common)
    with pytest.raises(VlmValidationError, match="start"):
        WindowManifestSet(manifests=(gap_second, gap_first), **common)
