"""Focused tests for provider-independent VLM kernel contracts."""

from __future__ import annotations

from autocut_kernel.media.root_evidence import (
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    FramePtsIndexSet,
    MediaKind,
)
from autocut_kernel.media.types import PTSIndex, TimeBase, canonical_sha256


def frame_pts_set(
    *,
    source_id: str,
    source_sha256: str,
    clock_id: str,
    time_base: TimeBase,
    origin_tick: int,
    end_tick: int,
    ticks: tuple[int, ...],
) -> FramePtsIndexSet:
    """Build one complete exact root FramePtsIndexSet for VLM tests."""

    context = EvidenceContext(
        source_id=source_id,
        source_sha256=source_sha256,
        media_kind=MediaKind.VIDEO,
        clock_id=clock_id,
        time_base=time_base,
        origin_tick=origin_tick,
        duration_tick=end_tick - origin_tick,
        producer_id="test-decoder-v1",
        generation_policy_sha256="sha256:" + "7" * 64,
    )
    coverage = Coverage(
        source_id=source_id,
        source_sha256=source_sha256,
        clock_id=clock_id,
        time_base=time_base,
        in_tick=origin_tick,
        out_tick=end_tick,
        outcome=CoverageOutcome.COMPLETE,
    )
    pts_index = PTSIndex(ticks)
    return FramePtsIndexSet(
        frame_pts_index_set_id="frame-pts-root-v1",
        context=context,
        coverage=coverage,
        pts_index=pts_index,
        pts_index_sha256=canonical_sha256(list(pts_index.ticks)),
    )
