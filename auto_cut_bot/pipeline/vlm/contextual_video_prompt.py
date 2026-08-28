"""Prompt v7: bounded video observations with an explicit context-pack input.

The wire response remains the registered V4 semantic pack.  This increment
changes only what can assist the model's interpretation, never the evidence
classes or physical-edit authority.
"""

from __future__ import annotations

from autocut_kernel.context_pack import WindowContextPack
from autocut_kernel.vlm import WindowManifest

from .bounded_video_prompt import build_vlm_bounded_video_prompt

VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION = "vlm-semantic-pack-v7-context-assisted"
VLM_CONTEXTUAL_VIDEO_PROMPT_TEMPLATE = (
    "你会得到一段外部剧情辅助。它只帮助理解叙事，不是视频证据。"
    "不得把其中的人名、关系、剧情或主题直接写成已观察到的 entity、fact、event、"
    "candidate 或时间范围；这些字段仍必须由随附视频实际可见、可听的内容支持。"
    "画面与剧情辅助冲突、无法确认人物对应关系或无法确认关系时，保留不确定性，"
    "使用中性 display_label，不要猜测。不得输出 context_ref、API ID、外部接口数据、"
    "ASR、VAD、字幕、shot/highlight 或任何物理剪辑端点。\n"
    "剧情辅助：\n"
)


def build_vlm_contextual_video_prompt(
    manifest: WindowManifest,
    context_pack: WindowContextPack,
) -> str:
    if type(manifest) is not WindowManifest:  # noqa: E721
        raise TypeError("contextual prompt requires an exact WindowManifest")
    if type(context_pack) is not WindowContextPack:  # noqa: E721
        raise TypeError("contextual prompt requires an exact WindowContextPack")
    return (
        build_vlm_bounded_video_prompt(manifest)
        + "\n"
        + VLM_CONTEXTUAL_VIDEO_PROMPT_TEMPLATE
        + context_pack.rendered_context
    )
