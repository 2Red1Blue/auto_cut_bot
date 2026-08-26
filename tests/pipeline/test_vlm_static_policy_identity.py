"""Static identities stay separate from per-window VLM evidence identities."""

from __future__ import annotations

import hashlib

import pytest
from autocut_kernel.media import (
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    FramePtsIndexSet,
    MediaKind,
    PTSIndex,
    TickRange,
    TimeBase,
)
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.vlm import (
    ProxyTimelineMap,
    WindowFrameSample,
    WindowManifest,
    WindowProxyBlobRef,
)

from auto_cut_bot.pipeline.source_prep.command import (
    DEFAULT_IDENTITY_SAMPLE_COUNT,
    identity_window_sample_indices,
    identity_window_sampling_policy,
    identity_window_sampling_policy_sha256,
)
from auto_cut_bot.pipeline.vlm.prompt import (
    VLM_PROMPT_TEMPLATE,
    build_vlm_prompt,
    vlm_prompt_template_sha256,
)


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


def _manifest(*, second_frame_hash: str = _hash("f")) -> WindowManifest:
    time_base = TimeBase(1, 1_000)
    context = EvidenceContext(
        "source-001",
        _hash("a"),
        MediaKind.VIDEO,
        "video-clock-0",
        time_base,
        1_000,
        100,
        "test-decoder-v1",
        _hash("7"),
    )
    coverage = Coverage(
        "source-001",
        _hash("a"),
        "video-clock-0",
        time_base,
        1_000,
        1_100,
        CoverageOutcome.COMPLETE,
    )
    ticks = PTSIndex((1_000, 1_010, 1_050, 1_090, 1_100))
    frame_index = FramePtsIndexSet(
        "frame-pts-root-v1",
        context,
        coverage,
        ticks,
        canonical_sha256(list(ticks.ticks)),
    )
    return WindowManifest(
        source_id="source-001",
        source_clock_id="video-clock-0",
        source_sha256=_hash("a"),
        stream_index=0,
        source_time_base=time_base,
        source_range=TickRange(1_000, 1_100),
        core_range=TickRange(1_000, 1_100),
        frame_pts_index_set=frame_index,
        proxy_blob_ref=WindowProxyBlobRef("proxy-001", _hash("b"), 4_096, "video/mp4"),
        preprocess_policy_sha256=_hash("c"),
        window_sampling_policy_sha256=_hash("d"),
        timeline_map=ProxyTimelineMap.translation(
            time_base=time_base,
            proxy_range=TickRange(0, 100),
            source_start_pts=1_000,
            max_source_error_pts=1,
        ),
        frame_samples=(
            WindowFrameSample(1_010, 10, _hash("e")),
            WindowFrameSample(1_050, 50, second_frame_hash),
        ),
    )


def test_prompt_template_identity_is_static_while_window_context_is_dynamic() -> None:
    first_prompt = build_vlm_prompt(_manifest())
    second_prompt = build_vlm_prompt(_manifest(second_frame_hash=_hash("9")))

    assert VLM_PROMPT_TEMPLATE.endswith("窗口证据：")
    assert vlm_prompt_template_sha256() == (
        "sha256:ba81ba735fb3033154e534044f126d5f28b4f03ae37f9192a218e154d5751218"
    )
    assert vlm_prompt_template_sha256() == (
        "sha256:" + hashlib.sha256(VLM_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()
    )
    assert first_prompt.startswith(VLM_PROMPT_TEMPLATE)
    assert second_prompt.startswith(VLM_PROMPT_TEMPLATE)
    assert first_prompt != second_prompt


def test_sampling_policy_identity_excludes_per_window_selection() -> None:
    expected_policy = {
        "algorithm": "uniform-decoded-frame-index-v1",
        "correspondence_certificate": "ffmpeg-copyts-showinfo-pts-equals-ffprobe-index-v1",
        "frame_encoding": "png-image2pipe-v1",
        "sample_count": DEFAULT_IDENTITY_SAMPLE_COUNT,
    }
    selected = identity_window_sample_indices(17)

    assert identity_window_sampling_policy() == expected_policy
    assert identity_window_sampling_policy_sha256() == canonical_sha256(expected_policy)
    assert identity_window_sampling_policy_sha256(3) != identity_window_sampling_policy_sha256()
    assert canonical_sha256({**expected_policy, "selected_indices": list(selected)}) != (
        identity_window_sampling_policy_sha256()
    )


def test_identity_window_sample_indices_match_the_existing_uniform_algorithm() -> None:
    assert identity_window_sample_indices(17) == (0, 2, 4, 6, 8, 10, 12, 14, 16)
    assert identity_window_sample_indices(3, 9) == (0, 1, 2)
    assert identity_window_sample_indices(10, 3) == (0, 4, 9)


@pytest.mark.parametrize("value", [False, True, 0, -1, 1.5, "9"])
def test_sampling_identity_rejects_non_positive_or_non_integer_counts(value: object) -> None:
    with pytest.raises(ValueError, match="sample_count must be positive"):
        identity_window_sampling_policy(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sample_count must be positive"):
        identity_window_sample_indices(3, value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [False, True, 0, -1, 1.5, "3"])
def test_sample_index_identity_rejects_non_positive_or_non_integer_frame_counts(value: object) -> None:
    with pytest.raises(ValueError, match="frame_count must be positive"):
        identity_window_sample_indices(value)  # type: ignore[arg-type]
