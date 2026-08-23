from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autocut_kernel.rendering import build_render_plan, parse_recipe


def _hash() -> str:
    return "sha256:" + hashlib.sha256(b"source").hexdigest()


def _json_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _recipe() -> dict[str, object]:
    pts_index = [0, 1001, 2003, 3000]
    return {
        "source": {"sha256": _hash(), "byte_size": 6},
        "span": {"start_pts": 1001, "end_pts": 2003},
        "timebase": {"numerator": 1, "denominator": 90_000},
        "evidence": {
            "source": {"sha256": _hash(), "byte_size": 6},
            "video_stream": {
                "stream_index": 0,
                "codec_name": "h264",
                "width": 64,
                "height": 48,
                "time_base": {"numerator": 1, "denominator": 90_000},
            },
            "pts_index": pts_index,
            "pts_index_sha256": _json_hash(pts_index),
            "validity_intervals": [{"start_pts": 0, "end_pts": 3000}],
            "ffprobe": {"executable": "ffprobe", "version": "test", "stderr_sha256": _json_hash("")},
            "fixture_id": "fixture-a",
            "fixture_manifest_sha256": _json_hash("manifest"),
            "fixture_sidecar_sha256": _json_hash("sidecar"),
            "fixture_schema_version": 1,
            "evidence_mode": "fixture_ground_truth_v1",
        },
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
    assert "-copyts" in first.argv
    assert first.output_timescale == 90_000
    assert "-an" in first.argv
    assert first.argv[-1] == "asset.mp4"


def test_derives_a_representable_timescale_for_a_non_90k_source_clock() -> None:
    recipe = _recipe()
    timebase = recipe["timebase"]
    evidence = recipe["evidence"]
    assert isinstance(timebase, dict) and isinstance(evidence, dict)
    timebase["denominator"] = 1_001
    video_stream = evidence["video_stream"]
    assert isinstance(video_stream, dict)
    video_timebase = video_stream["time_base"]
    assert isinstance(video_timebase, dict)
    video_timebase["denominator"] = 1_001
    parsed = parse_recipe(recipe, expected_source_sha256=_hash(), profile="test")

    plan = build_render_plan(parsed, source_path=Path("source.mp4"), output_path=Path("asset.mp4"))

    assert plan.output_timescale == 90_090_000
    assert plan.argv[plan.argv.index("-video_track_timescale") + 1] == "90090000"
