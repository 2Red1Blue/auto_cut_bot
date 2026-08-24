"""Versioned prompt pack for coarse, provenance-bound VLM observations."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import json
from decimal import Decimal

from autocut_kernel.vlm import WindowManifest

VLM_PROMPT_VERSION = "coarse-semantic-evidence-v1"

VLM_RESPONSE_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "observations"],
    "properties": {
        "schema_version": {"const": 1, "type": "integer"},
        "observations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "confidence",
                    "kind",
                    "proxy_interval",
                    "summary",
                    "supporting_frame_ids",
                ],
                "properties": {
                    "confidence": {
                        "type": "string",
                        "pattern": "^(?:0(?:\\.[0-9]+)?|1(?:\\.0+)?)$",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["observation", "change", "relation"],
                    },
                    "proxy_interval": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start_pts", "end_pts", "uncertainty_pts"],
                        "properties": {
                            "start_pts": {"type": "integer"},
                            "end_pts": {"type": "integer"},
                            "uncertainty_pts": {"type": "integer", "minimum": 0},
                        },
                    },
                    "summary": {"type": "string", "minLength": 1, "maxLength": 128},
                    "supporting_frame_ids": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    },
                },
            },
        },
    },
}


def vlm_response_schema_json() -> str:
    """Return the exact compact schema bytes represented as UTF-8 JSON text."""

    return json.dumps(VLM_RESPONSE_SCHEMA, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _seconds_text(tick: int, manifest: WindowManifest) -> str:
    time_base = manifest.timeline_map.proxy_time_base
    seconds = Decimal(tick) * Decimal(time_base.numerator) / Decimal(time_base.denominator)
    return format(seconds.quantize(Decimal("0.001")), "f")


def build_vlm_prompt(manifest: WindowManifest) -> str:
    """Build a manifest-bound prompt without granting the model physical-cut authority."""

    if type(manifest) is not WindowManifest:  # noqa: E721
        raise TypeError("manifest must be an exact WindowManifest")
    frame_anchors = [
        {
            "frame_id": sample.frame_id,
            "proxy_pts": sample.proxy_pts,
            "seconds": _seconds_text(sample.proxy_pts, manifest),
        }
        for sample in manifest.frame_samples
    ]
    prompt_context = {
        "allowed_frame_anchors": frame_anchors,
        "proxy_range": {
            "end_pts_exclusive": manifest.timeline_map.proxy_range.end_pts,
            "start_pts": manifest.timeline_map.proxy_range.start_pts,
        },
        "proxy_time_base": {
            "denominator": manifest.timeline_map.proxy_time_base.denominator,
            "numerator": manifest.timeline_map.proxy_time_base.numerator,
        },
    }
    context_json = json.dumps(
        prompt_context,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        "你是 Story-first 视频叙事语义分析器。只能根据当前窗口的视频画面判断，"
        "不得臆造对白、人物关系、剧情或时间。你的输出只是粗粒度语义候选，绝不是剪辑点。\n"
        "识别 1 到 4 个最重要的可见叙事观察：observation=持续状态，change=明显变化，"
        "relation=画面可直接支持的人物或物体关系。\n"
        "时间必须使用 proxy 时钟的整数 PTS；区间为 [start_pts,end_pts)，必须位于给定范围内。"
        "confidence 必须是 0 到 1 的十进制字符串，禁止 JSON 浮点数。"
        "每条观察必须引用至少一个给定 frame_id，且该帧的 proxy_pts 必须落在该观察区间内。"
        "uncertainty_pts 表示你对时间定位的保守误差，不得为负数。"
        "summary 使用简洁中文，只陈述视频中可见事实。"
        "只输出符合所给 JSON Schema 的单个 JSON 对象，不要 Markdown、解释或代码围栏。\n"
        f"窗口证据：{context_json}"
    )


__all__ = [
    "VLM_PROMPT_VERSION",
    "VLM_RESPONSE_SCHEMA",
    "build_vlm_prompt",
    "vlm_response_schema_json",
]
