"""
语义分析常量定义 - 被两个项目共享使用
"""
from typing import FrozenSet

# 任务类型 → 模型类别映射 (合同锁定, rule 2):
# 多模态任务需要能看视频帧的模型; 其余语义任务用纯文本模型。
MULTIMODAL_TASKS: FrozenSet[str] = frozenset({"vlm_analysis", "window_analysis", "story_video_qc"})
TEXT_TASKS: FrozenSet[str] = frozenset(
    {
        "episode_digest",
        "chapter_digest",
        "series_registry",
        "series_registry_relationship_repair",
        "series_registry_identity_audit",
        "series_assignment",
        "story_catalog",
        "story_script_draft",
        "story_plan_selection",
        "story_plan_orientation_fallback",
    }
)

# DashScope 特定常量
DASHSCOPE_DATA_INSPECTION_HEADER = "X-DashScope-DataInspection"
DASHSCOPE_DATA_INSPECTION_DISABLED = '{"input":"disable","output":"disable"}'
DASHSCOPE_API_KEY_ENV = "QWEN_AI_API_KEY"
DASHSCOPE_DEFAULT_MODEL = "qwen-vl-max"

# Volcengine Ark 特定常量
ARK_API_KEY_ENV = "ARK_API_KEY"

# OpenAI 特定常量
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_DEFAULT_MODEL = "gpt-4o"
