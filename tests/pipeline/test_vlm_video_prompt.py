"""V4 video wire is explicit and does not alter the installed V3 protocol."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

from autocut_kernel.media import TickRange
from autocut_kernel.vlm import ProxyTimelineMap

from auto_cut_bot.pipeline.vlm.prompt import VLM_RESPONSE_SCHEMA, vlm_prompt_template_sha256
from auto_cut_bot.pipeline.vlm.video_prompt import (
    VLM_VIDEO_PROMPT_TEMPLATE,
    build_vlm_video_prompt,
    vlm_video_response_schema,
    vlm_video_response_schema_json,
)
from tests.pipeline.test_doubao_vlm_request_factory import _prepared_episode


def test_video_schema_is_new_closed_support_union_and_does_not_mutate_v3() -> None:
    original = copy.deepcopy(VLM_RESPONSE_SCHEMA)
    schema = vlm_video_response_schema()
    root = schema["properties"]
    assert root["schema_version"] == {"type": "integer", "const": 4}
    for name in ("entities", "facts", "events", "candidate_hypotheses"):
        branches = root[name]["items"]["properties"]["support"]["oneOf"]
        video, anchored = branches
        assert video["properties"]["support_kind"]["const"] == "video_observation"
        assert set(video["required"]) == {"support_kind", "interval_ms", "confidence"}
        assert set(anchored["required"]) == {*video["required"], "frame_refs"}
        assert video["additionalProperties"] is anchored["additionalProperties"] is False
    segments = root["continuity"]["properties"]["temporal_segments"]["items"]
    assert set(segments["required"]) == {"mode", "summary", "support"}
    assert json.loads(vlm_video_response_schema_json()) == schema
    schema["properties"]["schema_version"]["const"] = 99
    assert vlm_video_response_schema()["properties"]["schema_version"]["const"] == 4
    assert VLM_RESPONSE_SCHEMA == original
    assert vlm_prompt_template_sha256() == "sha256:ba81ba735fb3033154e534044f126d5f28b4f03ae37f9192a218e154d5751218"


def test_playback_context_uses_short_aliases_and_relative_milliseconds() -> None:
    manifest = _prepared_episode().manifest
    rendered = build_vlm_video_prompt(manifest)
    context = json.loads(rendered[len(VLM_VIDEO_PROMPT_TEMPLATE):])
    assert context["duration_ms_floor"] == 100
    assert context["time_unit"] == "milliseconds_from_attached_video_playback_start"
    assert [item["frame_ref"] for item in context["reference_frames"]] == ["f0001", "f0002"]
    assert [item["time_ms_floor"] for item in context["reference_frames"]] == [sample.proxy_pts for sample in manifest.frame_samples]
    assert all(sample.frame_id not in rendered for sample in manifest.frame_samples)
    assert context["frame_alias_map_sha256"].startswith("sha256:")
    assert "proxy_pts" not in context and "source_pts" not in context


def test_nonzero_proxy_origin_is_not_added_to_model_playback_times() -> None:
    manifest = _prepared_episode().manifest
    shifted = replace(
        manifest,
        timeline_map=ProxyTimelineMap.translation(
            time_base=manifest.timeline_map.proxy_time_base,
            proxy_range=TickRange(500, 600), source_start_pts=manifest.source_range.start_pts,
        ),
        frame_samples=tuple(replace(sample, proxy_pts=sample.proxy_pts + 500) for sample in manifest.frame_samples),
    )
    context = json.loads(build_vlm_video_prompt(shifted)[len(VLM_VIDEO_PROMPT_TEMPLATE):])
    assert context["duration_ms_floor"] == 100
    assert [item["time_ms_floor"] for item in context["reference_frames"]] == [sample.proxy_pts for sample in manifest.frame_samples]


def test_prompt_keeps_video_observations_separate_from_frame_and_cut_proofs() -> None:
    assert "不要为了包含参考帧而扩大事实时间段" in VLM_VIDEO_PROMPT_TEMPLATE
    assert "不是对白切割点" in VLM_VIDEO_PROMPT_TEMPLATE
    assert "不能以误差为由填倒置或越界区间" in VLM_VIDEO_PROMPT_TEMPLATE
    assert "不省略独立事实/必要引用" in VLM_VIDEO_PROMPT_TEMPLATE
    assert "不是独立事实核验或安全许可" in VLM_VIDEO_PROMPT_TEMPLATE
