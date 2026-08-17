#!/usr/bin/env python3
"""Shared contracts and deterministic helpers for approved Story rendering."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from autocut_core.io import load_json, sha256_file
from autocut_core.schema.compat import validate_task_response


RECIPE_METHOD = "approved-qc-local-render-recipe-v3-cross-episode-tail-extension"
RECIPE_INDEX_METHOD = "approved-qc-local-render-recipe-index-v3-cross-episode-tail-extension"
RENDER_METHOD = "local-ffmpeg-story-render-v3-cross-episode-tail-extension"
MIN_RENDER_DURATION_SECONDS = 300.0
DEFAULT_PROFILE = {
    "name": "delivery",
    "width": 1080,
    "height": 1920,
    "fps": 25,
    "fit": "contain",
    "video_codec": "libx264",
    "video_crf": 18,
    "video_preset": "medium",
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "audio_bitrate_kbps": 192,
    "audio_sample_rate": 48000,
    "audio_channels": 2,
    "faststart": True,
}
DEFAULT_TRANSITION = {
    "type": "black_separator",
    "duration_seconds": 0.35,
    "audio_policy": "silence",
    "fade_out_seconds": 0.18,
    "fade_in_seconds": 0.18,
    "fade_curve": "tri",
}


def rounded(value: float) -> float:
    return round(float(value), 3)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-.")
    return cleaned or "story"


def ordered_blocks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(plan["blocks"], key=lambda item: item["play_order"])


def ordered_plan_clips(plan: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (block, clip)
        for block in ordered_blocks(plan)
        for clip in block["clips"]
    ]


def resolve_local_sources(
    public_manifest_path: Path,
    local_manifest_path: Path,
    required_source_ids: set[str],
) -> dict[str, dict[str, Any]]:
    public_manifest_path = public_manifest_path.expanduser().resolve()
    local_manifest_path = local_manifest_path.expanduser().resolve()
    public_manifest = load_json(public_manifest_path)
    local_manifest = load_json(local_manifest_path)
    public_sources = {
        item["id"]: item
        for item in public_manifest.get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    local_sources = {
        item["id"]: item
        for item in local_manifest.get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    missing_public = sorted(required_source_ids - set(public_sources))
    missing_local = sorted(required_source_ids - set(local_sources))
    if missing_public:
        raise ValueError(f"source_manifest misses Source IDs: {missing_public}")
    if missing_local:
        raise ValueError(f"local Source Manifest misses Source IDs: {missing_local}")
    resolved: dict[str, dict[str, Any]] = {}
    for source_id in sorted(required_source_ids):
        public = public_sources[source_id]
        local = local_sources[source_id]
        if local.get("episode") != public.get("episode"):
            raise ValueError(f"{source_id}: local Source episode differs from source_manifest")
        public_duration = float(public["duration_seconds"])
        local_duration = float(local["duration_seconds"])
        if abs(public_duration - local_duration) > 0.2:
            raise ValueError(
                f"{source_id}: local Source duration differs by more than 0.2s"
            )
        path_value = local.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError(f"{source_id}: local Source has no path")
        path = Path(path_value).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{source_id}: local Source is missing: {path}")
        actual_sha256 = sha256_file(path)
        declared_sha256 = local.get("sha256")
        if (
            not isinstance(declared_sha256, str)
            or len(declared_sha256) != 64
            or declared_sha256 != actual_sha256
        ):
            raise ValueError(f"{source_id}: local Source SHA-256 is stale")
        resolved[source_id] = {
            "source_id": source_id,
            "episode": int(public["episode"]),
            "path": str(path),
            "sha256": actual_sha256,
            "duration_seconds": rounded(local_duration),
        }
    return resolved


def build_render_recipe(
    *,
    plan: dict[str, Any],
    qc_report: dict[str, Any],
    local_sources: dict[str, dict[str, Any]],
    input_fingerprints: dict[str, str],
    profile: dict[str, Any] | None = None,
    transition: dict[str, Any] | None = None,
    human_qc_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = dict(profile or DEFAULT_PROFILE)
    transition = dict(transition or DEFAULT_TRANSITION)
    story_id = plan["story_id"]
    if plan.get("status") != "ready_for_video_qc":
        raise ValueError(f"{story_id}: effective Story Plan is not renderable")
    qc_status = qc_report.get("status")
    if qc_status != "approved":
        if not isinstance(human_qc_review, dict):
            raise ValueError(
                f"{story_id}: Story QC is not approved and has no human render authorization"
            )
        if human_qc_review.get("decision") != "accepted_for_render":
            raise ValueError(f"{story_id}: human QC render authorization is invalid")
        if human_qc_review.get("qc_status") != qc_status:
            raise ValueError(f"{story_id}: human QC authorization status differs from QC")
    if (
        qc_report.get("story_id") != story_id
        or qc_report.get("production_slot") != plan.get("production_slot")
        or qc_report.get("title") != plan.get("title")
    ):
        raise ValueError(f"{story_id}: Story QC identity differs from Story Plan")
    if qc_report.get("input_fingerprints", {}).get(
        "story_plan_sha256"
    ) != input_fingerprints.get("effective_story_plan_sha256"):
        raise ValueError(f"{story_id}: Story QC does not bind the effective Story Plan")
    blocks = ordered_blocks(plan)
    if len(blocks) < 2:
        raise ValueError(f"{story_id}: teaser-to-body rendering requires two Blocks")
    if blocks[0].get("role") != "teaser":
        raise ValueError(f"{story_id}: first Story Block is not teaser")
    if any(block.get("role") == "teaser" for block in blocks[1:]):
        raise ValueError(f"{story_id}: teaser role appears after the opening Block")
    if not blocks[0].get("clips") or not blocks[1].get("clips"):
        raise ValueError(f"{story_id}: teaser or body Block has no Clip")
    plan_pairs = ordered_plan_clips(plan)
    required_sources = {clip["source_id"] for _, clip in plan_pairs}
    missing_sources = sorted(required_sources - set(local_sources))
    if missing_sources:
        raise ValueError(f"{story_id}: missing local render Sources {missing_sources}")
    clips: list[dict[str, Any]] = []
    for block, clip in plan_pairs:
        source = local_sources[clip["source_id"]]
        if clip.get("episode") != source["episode"]:
            raise ValueError(f"{story_id}.{clip['id']}: Source episode mismatch")
        clips.append(
            {
                "id": clip["id"],
                "block_id": block["id"],
                "block_play_order": block["play_order"],
                "block_role": block["role"],
                "clip_order_in_block": block["clips"].index(clip) + 1,
                "source_id": clip["source_id"],
                "episode": clip["episode"],
                "source_start": rounded(clip["source_start"]),
                "source_end": rounded(clip["source_end"]),
                "duration_seconds": rounded(clip["duration_seconds"]),
            }
        )
    teaser_last_id = blocks[0]["clips"][-1]["id"]
    body_first_id = blocks[1]["clips"][0]["id"]
    transition_record = {
        "id": "transition-teaser-to-body",
        "from_clip_id": teaser_last_id,
        "to_clip_id": body_first_id,
        "from_block_id": blocks[0]["id"],
        "to_block_id": blocks[1]["id"],
        "type": transition["type"],
        "duration_seconds": rounded(transition["duration_seconds"]),
        "audio_policy": transition["audio_policy"],
        "fade_out_seconds": rounded(transition["fade_out_seconds"]),
        "fade_in_seconds": rounded(transition["fade_in_seconds"]),
        "fade_curve": transition["fade_curve"],
    }
    base_story_duration = rounded(
        sum(item["duration_seconds"] for item in clips)
        + transition_record["duration_seconds"]
    )
    last_clip = clips[-1]
    last_source = local_sources[last_clip["source_id"]]
    tail_from = rounded(last_clip["source_end"])
    tail_to = rounded(last_source["duration_seconds"])
    editorial_contract = plan.get("editorial_contract", {})
    extension_contract = {}
    if isinstance(editorial_contract, dict) and editorial_contract.get(
        "primary_story_thread_id"
    ):
        policy = editorial_contract.get("duration_extension_policy", {})
        extension_contract = {
            "primary_story_thread_id": editorial_contract[
                "primary_story_thread_id"
            ],
            "same_primary_thread_only": policy.get(
                "same_primary_thread_only", True
            ),
            "must_be_forward_chronological": policy.get(
                "must_be_forward_chronological", True
            ),
            "no_cross_thread_fill": policy.get("no_cross_thread_fill", True),
            "no_duplicate_or_functionless_fill": policy.get(
                "no_duplicate_or_functionless_fill", True
            ),
            "semantic_validation": (
                "tail_extension must remain covered by the approved primary "
                "thread and post-plan Story QC review"
            ),
        }
    tail_extension = {
        "enabled": base_story_duration < MIN_RENDER_DURATION_SECONDS,
        "target_duration_seconds": MIN_RENDER_DURATION_SECONDS,
        "source_id": last_clip["source_id"],
        "episode": int(last_clip["episode"]),
        "from_source_seconds": tail_from,
        "to_source_seconds": tail_from,
        "extended_seconds": 0.0,
        "reached_target": base_story_duration >= MIN_RENDER_DURATION_SECONDS,
        "base_story_duration_seconds": base_story_duration,
        "anchor_clip_id": last_clip["id"],
        "final_source_id": last_clip["source_id"],
        "final_episode": int(last_clip["episode"]),
        "final_to_source_seconds": tail_from,
        "segments": [],
        "policy": (
            "若 Story Plan 不足 300 秒，先从最后一个 Story Clip 的源时间码继续到该集片尾；"
            "若仍不足则按集号连续追加后续集，达到 300 秒后仍播放到达到门槛所在集的片尾；"
            "只允许已批准 primary story thread 的向后顺剪，不重复、不补无功能内容、不跳集。"
        ),
    }
    if extension_contract:
        tail_extension["contract"] = extension_contract
    if tail_extension["enabled"]:
        accumulated_duration = base_story_duration
        tail_clip_order = int(last_clip["clip_order_in_block"]) + 1
        current_source = last_source
        current_episode = int(last_clip["episode"])

        def append_tail_segment(
            source: dict[str, Any],
            *,
            source_start: float,
            source_end: float,
            clip: dict[str, Any] | None = None,
        ) -> None:
            nonlocal accumulated_duration, tail_clip_order
            source_start = rounded(source_start)
            source_end = rounded(source_end)
            segment_duration = rounded(source_end - source_start)
            if segment_duration <= 0.002:
                return
            if clip is None:
                clip_id = f"tail-extension-{source['source_id']}"
                existing_ids = {item["id"] for item in clips}
                suffix = 2
                while clip_id in existing_ids:
                    clip_id = f"tail-extension-{source['source_id']}-{suffix}"
                    suffix += 1
                clip = {
                    "id": clip_id,
                    "block_id": blocks[-1]["id"],
                    "block_play_order": blocks[-1]["play_order"],
                    "block_role": blocks[-1]["role"],
                    "clip_order_in_block": tail_clip_order,
                    "source_id": source["source_id"],
                    "episode": int(source["episode"]),
                    "source_start": source_start,
                    "source_end": source_end,
                    "duration_seconds": segment_duration,
                }
                clips.append(clip)
                tail_clip_order += 1
            else:
                clip["source_end"] = source_end
                clip["duration_seconds"] = rounded(
                    source_end - float(clip["source_start"])
                )
            tail_extension["segments"].append(
                {
                    "sequence": len(tail_extension["segments"]) + 1,
                    "clip_id": clip["id"],
                    "source_id": source["source_id"],
                    "episode": int(source["episode"]),
                    "from_source_seconds": source_start,
                    "to_source_seconds": source_end,
                    "duration_seconds": segment_duration,
                }
            )
            if (
                len(tail_extension["segments"]) == 1
                and source["source_id"] == tail_extension["source_id"]
            ):
                tail_extension["to_source_seconds"] = source_end
            accumulated_duration = rounded(accumulated_duration + segment_duration)
            tail_extension["extended_seconds"] = rounded(
                float(tail_extension["extended_seconds"]) + segment_duration
            )
            tail_extension["final_source_id"] = source["source_id"]
            tail_extension["final_episode"] = int(source["episode"])
            tail_extension["final_to_source_seconds"] = source_end

        # First use the remainder of the current Source. This also mutates the
        # final Story Clip so the render remains one continuous source range.
        append_tail_segment(
            current_source,
            source_start=tail_from,
            source_end=tail_to,
            clip=last_clip,
        )

        # If that episode is too short, append only the immediate next episode,
        # then continue one episode at a time. Never skip an episode or stop at
        # the exact 300s point before the selected episode's physical tail.
        while accumulated_duration < MIN_RENDER_DURATION_SECONDS - 0.002:
            next_episode = current_episode + 1
            candidates = sorted(
                (
                    source
                    for source in local_sources.values()
                    if int(source["episode"]) == next_episode
                ),
                key=lambda source: source["source_id"],
            )
            if not candidates:
                break
            current_source = candidates[0]
            current_episode = next_episode
            append_tail_segment(
                current_source,
                source_start=0.0,
                source_end=float(current_source["duration_seconds"]),
            )

        tail_extension["reached_target"] = (
            accumulated_duration >= MIN_RENDER_DURATION_SECONDS - 0.002
        )
    else:
        # Disabled means the Story Plan itself already reached the target; no
        # synthetic tail metadata is implied.
        tail_extension["reached_target"] = True

    required_sources.update(item["source_id"] for item in clips)
    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    order = 0
    for clip in clips:
        order += 1
        duration = rounded(clip["duration_seconds"])
        timeline.append(
            {
                "order": order,
                "kind": "clip",
                "ref_id": clip["id"],
                "start_seconds": rounded(cursor),
                "end_seconds": rounded(cursor + duration),
                "duration_seconds": duration,
            }
        )
        cursor = rounded(cursor + duration)
        if clip["id"] == teaser_last_id:
            duration = rounded(transition_record["duration_seconds"])
            order += 1
            timeline.append(
                {
                    "order": order,
                    "kind": "transition",
                    "ref_id": transition_record["id"],
                    "start_seconds": rounded(cursor),
                    "end_seconds": rounded(cursor + duration),
                    "duration_seconds": duration,
                }
            )
            cursor = rounded(cursor + duration)
    source_duration = rounded(sum(item["duration_seconds"] for item in clips))
    transition_duration = rounded(transition_record["duration_seconds"])
    recipe = {
        "schema_version": "1.0",
        "method": RECIPE_METHOD,
        "story_id": story_id,
        "title": plan["title"],
        "production_slot": plan["production_slot"],
        "status": "ready_for_render",
        "input_fingerprints": dict(input_fingerprints),
        "render_profile": profile,
        "transition_policy": {
            "teaser_to_body": dict(transition),
            "other_junctions": "hard_cut",
        },
        "sources": [local_sources[source_id] for source_id in sorted(required_sources)],
        "clips": clips,
        "transitions": [transition_record],
        "timeline": timeline,
        "clip_count": len(clips),
        "transition_count": 1,
        "source_duration_seconds": source_duration,
        "transition_duration_seconds": transition_duration,
        "expected_duration_seconds": rounded(cursor),
        "output_filename": (
            f"{int(plan['production_slot']):02d}-{safe_filename(story_id)}.mp4"
        ),
        "tail_extension": tail_extension,
    }
    if human_qc_review is not None:
        recipe["human_qc_review"] = dict(human_qc_review)
    errors = validate_render_recipe(recipe, check_files=False)
    if errors:
        raise ValueError(
            f"{story_id}: locally materialized Render Recipe is invalid: "
            + "; ".join(errors[:30])
        )
    return recipe


def validate_render_recipe(
    recipe: dict[str, Any], *, check_files: bool = True
) -> list[str]:
    errors = list(validate_task_response("story_render_recipe", recipe))
    if errors:
        return errors
    story_id = recipe["story_id"]
    if recipe["render_profile"] != DEFAULT_PROFILE:
        errors.append(f"{story_id}: render profile differs from delivery default")
    expected_transition_policy = {
        "teaser_to_body": DEFAULT_TRANSITION,
        "other_junctions": "hard_cut",
    }
    if recipe["transition_policy"] != expected_transition_policy:
        errors.append(f"{story_id}: transition policy differs from the default")
    sources = {item["source_id"]: item for item in recipe["sources"]}
    clips = {item["id"]: item for item in recipe["clips"]}
    transitions = {item["id"]: item for item in recipe["transitions"]}
    if len(sources) != len(recipe["sources"]):
        errors.append(f"{story_id}: duplicate Source IDs")
    if len(clips) != len(recipe["clips"]):
        errors.append(f"{story_id}: duplicate Clip IDs")
    if len(transitions) != 1 or recipe["transition_count"] != 1:
        errors.append(f"{story_id}: exactly one teaser-to-body transition is required")
    timeline = recipe["timeline"]
    if [item["order"] for item in timeline] != list(range(1, len(timeline) + 1)):
        errors.append(f"{story_id}: timeline order is not contiguous")
    cursor = 0.0
    seen_clips: list[str] = []
    seen_transitions: list[str] = []
    for item in timeline:
        if abs(float(item["start_seconds"]) - cursor) > 0.002:
            errors.append(f"{story_id}: timeline has a gap or overlap at order {item['order']}")
        duration = float(item["duration_seconds"])
        if abs(float(item["end_seconds"]) - (cursor + duration)) > 0.002:
            errors.append(f"{story_id}: timeline duration is inconsistent at order {item['order']}")
        cursor = rounded(cursor + duration)
        if item["kind"] == "clip":
            if item["ref_id"] not in clips:
                errors.append(f"{story_id}: timeline references unknown Clip {item['ref_id']}")
            seen_clips.append(item["ref_id"])
        else:
            if item["ref_id"] not in transitions:
                errors.append(
                    f"{story_id}: timeline references unknown transition {item['ref_id']}"
                )
            seen_transitions.append(item["ref_id"])
    if seen_clips != [item["id"] for item in recipe["clips"]]:
        errors.append(f"{story_id}: timeline does not preserve Clip order")
    if seen_transitions != [item["id"] for item in recipe["transitions"]]:
        errors.append(f"{story_id}: timeline does not contain the transition exactly once")
    if transitions:
        transition = next(iter(transitions.values()))
        transition_positions = [
            index
            for index, item in enumerate(timeline)
            if item["kind"] == "transition"
        ]
        if len(transition_positions) == 1:
            position = transition_positions[0]
            if position == 0 or position == len(timeline) - 1:
                errors.append(f"{story_id}: transition cannot be first or last")
            else:
                left = timeline[position - 1]
                right = timeline[position + 1]
                if (
                    left["kind"] != "clip"
                    or right["kind"] != "clip"
                    or left["ref_id"] != transition["from_clip_id"]
                    or right["ref_id"] != transition["to_clip_id"]
                ):
                    errors.append(f"{story_id}: transition is not between declared Clips")
                elif (
                    clips[left["ref_id"]]["block_role"] != "teaser"
                    or clips[right["ref_id"]]["block_role"] == "teaser"
                ):
                    errors.append(f"{story_id}: transition is not at teaser-to-body boundary")
    source_total = 0.0
    for clip in recipe["clips"]:
        source = sources.get(clip["source_id"])
        if source is None:
            errors.append(f"{story_id}.{clip['id']}: local Source is absent")
            continue
        duration = float(clip["source_end"]) - float(clip["source_start"])
        if duration <= 0 or abs(duration - float(clip["duration_seconds"])) > 0.002:
            errors.append(f"{story_id}.{clip['id']}: Clip duration is inconsistent")
        if float(clip["source_end"]) > float(source["duration_seconds"]) + 0.002:
            errors.append(f"{story_id}.{clip['id']}: Clip exceeds local Source")
        source_total += float(clip["duration_seconds"])
    if abs(source_total - float(recipe["source_duration_seconds"])) > 0.002:
        errors.append(f"{story_id}: source duration total is inconsistent")
    if abs(
        float(recipe["transition_duration_seconds"])
        - float(DEFAULT_TRANSITION["duration_seconds"])
    ) > 0.002:
        errors.append(f"{story_id}: transition duration total is inconsistent")
    if abs(cursor - float(recipe["expected_duration_seconds"])) > 0.002:
        errors.append(f"{story_id}: expected duration differs from timeline")
    tail = recipe["tail_extension"]
    last_clip = recipe["clips"][-1]
    anchor_clip = clips.get(tail["anchor_clip_id"])
    if anchor_clip is None:
        errors.append(f"{story_id}: tail extension anchor Clip is absent")
    if tail["final_source_id"] != last_clip["source_id"]:
        errors.append(f"{story_id}: tail extension final Source differs from last Clip")
    if tail["final_episode"] != last_clip["episode"]:
        errors.append(f"{story_id}: tail extension final episode differs from last Clip")
    final_source = sources.get(tail["final_source_id"])
    if final_source is None:
        errors.append(f"{story_id}: tail extension final Source is absent")
    elif abs(
        float(tail["final_to_source_seconds"])
        - float(last_clip["source_end"])
    ) > 0.002:
        errors.append(f"{story_id}: tail extension final boundary differs from last Clip")

    segments = tail["segments"]
    if [item["sequence"] for item in segments] != list(range(1, len(segments) + 1)):
        errors.append(f"{story_id}: tail extension segment sequence is not contiguous")
    expected_extended = 0.0
    previous_episode: int | None = None
    for index, segment in enumerate(segments):
        source = sources.get(segment["source_id"])
        segment_clip = clips.get(segment["clip_id"])
        if source is None:
            errors.append(
                f"{story_id}: tail extension segment Source is absent: {segment['source_id']}"
            )
            continue
        if segment_clip is None:
            errors.append(
                f"{story_id}: tail extension segment Clip is absent: {segment['clip_id']}"
            )
        if segment["episode"] != source["episode"]:
            errors.append(f"{story_id}: tail extension segment episode differs from Source")
        if previous_episode is not None and segment["episode"] != previous_episode + 1:
            errors.append(f"{story_id}: tail extension skips or repeats an episode")
        previous_episode = int(segment["episode"])
        duration = float(segment["to_source_seconds"]) - float(
            segment["from_source_seconds"]
        )
        if duration <= 0 or abs(duration - float(segment["duration_seconds"])) > 0.002:
            errors.append(f"{story_id}: tail extension segment duration is inconsistent")
        if float(segment["to_source_seconds"]) > float(source["duration_seconds"]) + 0.002:
            errors.append(f"{story_id}: tail extension segment exceeds Source")
        if index > 0 and abs(float(segment["from_source_seconds"])) > 0.002:
            errors.append(f"{story_id}: subsequent tail episode does not start at 0s")
        if abs(float(segment["to_source_seconds"]) - float(source["duration_seconds"])) > 0.002:
            errors.append(f"{story_id}: tail extension segment does not end at episode tail")
        if segment_clip is not None:
            if segment_clip["source_id"] != segment["source_id"]:
                errors.append(f"{story_id}: tail extension segment maps to the wrong Source")
            if float(segment_clip["source_start"]) > float(segment["from_source_seconds"]) + 0.002:
                errors.append(f"{story_id}: tail extension segment starts before its Clip")
            if abs(float(segment_clip["source_end"]) - float(segment["to_source_seconds"])) > 0.002:
                errors.append(f"{story_id}: tail extension segment does not end at its Clip boundary")
        expected_extended += duration
    if segments:
        first = segments[0]
        if first["source_id"] == tail["source_id"] and first["episode"] == tail["episode"]:
            if abs(float(first["from_source_seconds"]) - float(tail["from_source_seconds"])) > 0.002:
                errors.append(f"{story_id}: tail extension first start is inconsistent")
            if abs(float(first["to_source_seconds"]) - float(tail["to_source_seconds"])) > 0.002:
                errors.append(f"{story_id}: tail extension first end is inconsistent")
        elif not (
            abs(float(tail["from_source_seconds"]) - float(tail["to_source_seconds"])) <= 0.002
            and first["episode"] == tail["episode"] + 1
            and abs(float(first["from_source_seconds"])) <= 0.002
        ):
            errors.append(f"{story_id}: tail extension first segment does not follow its anchor Source")
    elif abs(float(tail["from_source_seconds"]) - float(tail["to_source_seconds"])) > 0.002:
        errors.append(f"{story_id}: disabled or unavailable tail has non-zero source range")
    if abs(float(tail["extended_seconds"]) - expected_extended) > 0.002:
        errors.append(f"{story_id}: tail extension duration is inconsistent")
    expected_base = rounded(
        float(recipe["expected_duration_seconds"])
        - float(tail["extended_seconds"])
    )
    if abs(expected_base - float(tail["base_story_duration_seconds"])) > 0.002:
        errors.append(f"{story_id}: tail extension base duration is inconsistent")
    expected_enabled = expected_base < MIN_RENDER_DURATION_SECONDS - 0.002
    if bool(tail["enabled"]) != expected_enabled:
        errors.append(f"{story_id}: tail extension enabled state is inconsistent")
    expected_reached = float(recipe["expected_duration_seconds"]) >= MIN_RENDER_DURATION_SECONDS - 0.002
    if bool(tail["reached_target"]) != expected_reached:
        errors.append(f"{story_id}: tail extension reached_target is inconsistent")
    if check_files:
        for source in recipe["sources"]:
            path = Path(source["path"]).expanduser().resolve()
            if not path.is_file():
                errors.append(f"{story_id}: missing local Source {path}")
            elif sha256_file(path) != source["sha256"]:
                errors.append(f"{story_id}: local Source SHA-256 is stale: {path}")
    for value in (
        recipe["source_duration_seconds"],
        recipe["transition_duration_seconds"],
        recipe["expected_duration_seconds"],
    ):
        if not math.isfinite(float(value)):
            errors.append(f"{story_id}: duration is not finite")
    return errors
