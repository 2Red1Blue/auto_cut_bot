"""音频边界策略 — Rule 26 的运行时唯一权威来源。

业务含义: 短剧切片的剪切点必须避开人声区间 (避免切断台词),
通过双轨 VAD (原混音 + demucs 人声轨) 并集门控判定语音区间,
并给出安全边界/渐变回退参数。音频修复、QC、渲染三个下游 Stage
均以此为准。

缓存语义: 任何策略字段变更都会使 policy_version 变化,
进而使下游音频 / QC / 渲染缓存自动失效 (见 version.get_cache_key)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from autocut_core.version import SCHEMA_VERSIONS


@dataclass(frozen=True)
class AudioBoundaryPolicy:
    """Rule 26: 双轨 VAD 门控 + 渐变回退 (fade_fallback) 策略。

    frozen 保证策略在运行时不可被 Stage 篡改。
    """

    # 版本集中在 autocut_core.version 管理
    policy_version: str = SCHEMA_VERSIONS["audio_boundary_policy"]
    demucs_model: str = "htdemucs"
    silero_backend: str = "onnx"

    # 参与语音区间分析的轨道 (并集门控: 任一轨检出语音即视为语音)
    vad_tracks: tuple[str, ...] = ("original_mix", "demucs_vocals")
    speech_interval_policy: str = "union"

    # Silero VAD 参数
    sample_rate: int = 16000
    vad_threshold: float = 0.5
    min_speech_duration_ms: int = 100
    min_silence_duration_ms: int = 350
    speech_pad_ms: int = 120

    # 边界安全参数 (最小安全间隙/头部前导/尾部后摆等)
    minimum_safe_gap_seconds: float = 0.35
    start_lead_seconds: float = 0.15
    end_tail_seconds: float = 0.25
    max_recommendation_shift_seconds: float = 12.0
    source_edge_tolerance_seconds: float = 0.01

    # 渐变回退参数 (无隙可扩展时改用音视频渐变过渡)
    fade_fallback_audio_crossfade_seconds: float = 0.15
    fade_fallback_video_fade_seconds: float = 0.10
    fade_fallback_story_end_fadeout_seconds: float = 0.5

    # 允许通过准入门的边界状态集合
    required_boundary_statuses: tuple[str, ...] = (
        "safe",
        "safe_source_edge",
        "not_applicable_no_audio",
    )

    @classmethod
    def from_json(cls, path: str) -> "AudioBoundaryPolicy":
        """从 JSON 策略文件加载 (字段缺失时用默认值)。"""
        import json

        with open(path, "r") as f:
            data = json.load(f)

        fb = data.get("fade_fallback_policy", {})
        return cls(
            policy_version=data.get("policy_version", "1.4"),
            demucs_model=data.get("demucs_model", "htdemucs"),
            silero_backend=data.get("silero_backend", "onnx"),
            vad_tracks=tuple(data.get("vad_tracks", ["original_mix", "demucs_vocals"])),
            speech_interval_policy=data.get("speech_interval_policy", "union"),
            sample_rate=data.get("sample_rate", 16000),
            vad_threshold=data.get("vad_threshold", 0.5),
            min_speech_duration_ms=data.get("min_speech_duration_ms", 100),
            min_silence_duration_ms=data.get("min_silence_duration_ms", 350),
            speech_pad_ms=data.get("speech_pad_ms", 120),
            minimum_safe_gap_seconds=data.get("minimum_safe_gap_seconds", 0.35),
            start_lead_seconds=data.get("start_lead_seconds", 0.15),
            end_tail_seconds=data.get("end_tail_seconds", 0.25),
            max_recommendation_shift_seconds=data.get(
                "max_recommendation_shift_seconds", 12.0
            ),
            source_edge_tolerance_seconds=data.get(
                "source_edge_tolerance_seconds", 0.01
            ),
            fade_fallback_audio_crossfade_seconds=fb.get(
                "audio_crossfade_seconds", 0.15
            ),
            fade_fallback_video_fade_seconds=fb.get("video_fade_seconds", 0.10),
            fade_fallback_story_end_fadeout_seconds=fb.get(
                "story_end_audio_fadeout_seconds", 0.5
            ),
            required_boundary_statuses=tuple(
                data.get(
                    "required_boundary_statuses",
                    ["safe", "safe_source_edge", "not_applicable_no_audio"],
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """导出为同构字典 (供落盘/下游 Stage 消费)。"""
        return {
            "policy_version": self.policy_version,
            "demucs_model": self.demucs_model,
            "silero_backend": self.silero_backend,
            "vad_tracks": list(self.vad_tracks),
            "speech_interval_policy": self.speech_interval_policy,
            "sample_rate": self.sample_rate,
            "vad_threshold": self.vad_threshold,
            "min_speech_duration_ms": self.min_speech_duration_ms,
            "min_silence_duration_ms": self.min_silence_duration_ms,
            "speech_pad_ms": self.speech_pad_ms,
            "minimum_safe_gap_seconds": self.minimum_safe_gap_seconds,
            "start_lead_seconds": self.start_lead_seconds,
            "end_tail_seconds": self.end_tail_seconds,
            "max_recommendation_shift_seconds": self.max_recommendation_shift_seconds,
            "source_edge_tolerance_seconds": self.source_edge_tolerance_seconds,
            "fade_fallback_policy": {
                "audio_crossfade_seconds": self.fade_fallback_audio_crossfade_seconds,
                "video_fade_seconds": self.fade_fallback_video_fade_seconds,
                "story_end_audio_fadeout_seconds": self.fade_fallback_story_end_fadeout_seconds,
            },
            "required_boundary_statuses": list(self.required_boundary_statuses),
        }
