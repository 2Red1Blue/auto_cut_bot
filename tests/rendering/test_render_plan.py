from __future__ import annotations

import hashlib
from pathlib import Path

from autocut_kernel.rendering import build_render_plan, parse_recipe


def _hash() -> str:
    return "sha256:" + hashlib.sha256(b"source").hexdigest()


def _recipe() -> dict[str, object]:
    return {
        "source": {"sha256": _hash(), "byte_size": 6},
        "span": {"start_pts": 1001, "end_pts": 2003},
        "timebase": {"numerator": 1, "denominator": 90_000},
        "evidence": {"fixture_id": "fixture-a", "evidence_mode": "fixture_ground_truth_v1"},
    }


def test_builds_a_deterministic_exact_pts_video_only_plan() -> None:
    recipe = parse_recipe(_recipe(), expected_source_sha256=_hash(), profile="test")

    first = build_render_plan(recipe, source_path=Path("source.mp4"), output_path=Path("asset.mp4"))
    second = build_render_plan(recipe, source_path=Path("source.mp4"), output_path=Path("asset.mp4"))

    assert first == second
    assert first.filter_graph == "[0:v:0]trim=start_pts=1001:end_pts=2003,setpts=PTS-STARTPTS[v0]"
    assert "-ss" not in first.argv
    assert "-to" not in first.argv
    assert "-c" not in first.argv
    assert "-c:v" in first.argv
    assert "-an" in first.argv
    assert first.argv[-1] == "asset.mp4"
