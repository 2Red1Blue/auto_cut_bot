"""语义模型后端描述符 — 锁定各模型提供商的端点/模型/API key 解析规则。

每个后端是一个 frozen dataclass (provider, endpoint, 模型名, API key 来源),
通过 ``BACKENDS`` 字典注册; ``get_backend(name)`` 负责名称解析与别名兼容。

模型选择是合同锁定项 (SKILL.md rule 2) — 任务类型→模型的映射
(MULTIMODAL_TASKS / TEXT_TASKS) 在此集中定义, 各 Stage 不得自行指定。

base_url 与模型名可通过 ``PipelineConfig`` 覆盖（当前值为默认）:
  - config.backend_base_url / config.api_base → base_url
  - config.backend_multimodal_model / config.backend_text_model / config.model
    → 对应模型名

新增后端: 构造 ``SemanticBackend`` 实例并加入 ``BACKENDS``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Mapping


# DashScope 专属请求头 (SKILL.md rule 2): 关闭输入/输出内容审查
DASHSCOPE_DATA_INSPECTION_HEADER = "X-DashScope-DataInspection"
DASHSCOPE_DATA_INSPECTION_DISABLED = '{"input":"disable","output":"disable"}'

# 任务类型 → 模型类别映射 (合同锁定, rule 2):
# 多模态任务需要能看视频帧的模型; 其余语义任务用纯文本模型。
MULTIMODAL_TASKS = frozenset({"window_analysis", "story_video_qc"})
TEXT_TASKS = frozenset(
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


@dataclass(frozen=True)
class SemanticBackend:
    """不可变的后端描述符 — 模型选择是合同锁定项 (rule 2)。

    frozen 保证运行时不可篡改; 配置覆盖通过 replace() 生成新实例
    (见 apply_config_overrides), 不污染 BACKENDS 注册表。
    """

    name: str
    provider: str
    api_key_env: str          # API key 的环境变量名
    base_url: str
    api_key_config_attr: str = ""  # PipelineConfig 上专属 key 字段名 (空=无专属)
    chat_path: str = "/chat/completions"
    multimodal_model: str = ""  # 多模态任务用模型
    text_model: str = ""        # 纯文本任务用模型
    output_hard_limit: int | None = None  # 输出 token 硬上限 (None=无)
    include_enable_thinking: bool = False  # 请求体是否带 enable_thinking
    rpm_limit: int = 0  # 每分钟请求上限 (0=无限制)
    extra_headers: tuple[tuple[str, str], ...] = ()  # 附加请求头

    def api_key(self, environ: Mapping[str, str] | None = None) -> str | None:
        """从环境变量解析 API key (默认读 os.environ, 可注入测试环境)。"""
        values = os.environ if environ is None else environ
        return values.get(self.api_key_env)

    def resolve_api_key(
        self,
        config: Any = None,
        environ: Mapping[str, str] | None = None,
    ) -> str | None:
        """API key 解析: config 显式配置 > 环境变量 (兼容旧变量名)。

        每个后端自行声明专属 config 字段 (api_key_config_attr),
        新增后端无需修改 resolve_api_key 函数 (OCP 合规)。
        """
        if config is not None:
            # 后端专属 key 字段 (如 qwen_api_key, ark_api_key)
            if self.api_key_config_attr:
                value = getattr(config, self.api_key_config_attr, None)
                if value:
                    return value
            # 通用 key 字段
            value = getattr(config, "api_key", None)
            if value:
                return value
        return self.api_key(environ)

    def model_for_task(self, task: str) -> str:
        """按任务类型返回对应模型名; 未知任务抛 ValueError。"""
        if task in MULTIMODAL_TASKS:
            return self.multimodal_model
        if task in TEXT_TASKS:
            return self.text_model
        raise ValueError(f"unsupported semantic task: {task}")

    def effective_max_tokens(self, requested: int) -> int:
        """请求的 max_tokens 不得超过后端硬上限 (夹取)。"""
        return (
            requested
            if self.output_hard_limit is None
            else min(requested, self.output_hard_limit)
        )

    @property
    def litellm_model(self) -> str:
        """LiteLLM 模型标识符 (文本模型)。

        将 provider 名称映射到 LiteLLM 规范:
          - dashscope-openai-compatible → openai/qwen3.7-max
          - volcengine-ark-openai-compatible → openai/doubao-seed-2-1-pro-260628

        多模态模型选择由 build_request() 在 payload 中处理,
        call_provider 通过 f"openai/{payload['model']}" 自动适配。
        """
        return f"openai/{self.text_model}"

    def http_headers(self, api_key: str) -> dict[str, str]:
        """构造请求头: Bearer 认证 + JSON + 后端专属附加头。"""
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **dict(self.extra_headers),
        }


BACKENDS: dict[str, SemanticBackend] = {
    # 通义千问 (DashScope OpenAI 兼容模式) — 默认后端, 500 RPM
    "qwen": SemanticBackend(
        name="qwen",
        provider="dashscope-openai-compatible",
        api_key_env="QWEN_AI_API_KEY",
        api_key_config_attr="qwen_api_key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        multimodal_model="qwen3.7-plus",
        text_model="qwen3.7-max",
        output_hard_limit=65536,
        include_enable_thinking=True,
        rpm_limit=500,
        extra_headers=(
            (DASHSCOPE_DATA_INSPECTION_HEADER, DASHSCOPE_DATA_INSPECTION_DISABLED),
        ),
    ),
    # 豆包 (火山引擎 Ark OpenAI 兼容模式) — 200 RPM
    "doubao": SemanticBackend(
        name="doubao",
        provider="volcengine-ark-openai-compatible",
        api_key_env="ARK_API_KEY",
        api_key_config_attr="ark_api_key",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        multimodal_model="doubao-seed-2-1-pro-260628",
        text_model="doubao-seed-2-1-pro-260628",
        output_hard_limit=None,
        rpm_limit=200,
    ),
}


def apply_config_overrides(
    backend: SemanticBackend, config: Any
) -> SemanticBackend:
    """用 PipelineConfig 覆盖 base_url / 模型名（保留 BACKENDS 默认值）。"""
    changes: dict[str, Any] = {}
    base_url = getattr(config, "backend_base_url", None) or getattr(
        config, "api_base", None
    )
    if base_url:
        changes["base_url"] = str(base_url)
    generic_model = getattr(config, "model", None)
    multimodal = getattr(config, "backend_multimodal_model", None) or generic_model
    text = getattr(config, "backend_text_model", None) or generic_model
    if multimodal:
        changes["multimodal_model"] = str(multimodal)
    if text:
        changes["text_model"] = str(text)
    return replace(backend, **changes) if changes else backend


def resolve_api_key(
    backend: SemanticBackend,
    config: Any = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """API key 解析 — 委托给 backend.resolve_api_key (OCP 合规)。

    新增后端只需设置 api_key_config_attr 字段, 无需修改此函数。
    """
    return backend.resolve_api_key(config, environ)


def get_backend(
    name: str | None = None, config: Any = None
) -> SemanticBackend:
    """解析后端描述符 — 名称 > 环境变量 SHORT_DRAMA_AI_BACKEND > 默认 qwen。

    支持直接传模型名 (别名表映射到后端名);
    非法名称抛 ValueError; 传入 config 时叠加配置覆盖。
    """
    value = name or os.environ.get("SHORT_DRAMA_AI_BACKEND", "qwen")
    aliases = {
        "qwen3.7-plus": "qwen",
        "qwen3.7-max": "qwen",
        "doubao-seed-2-1-pro-260628": "doubao",
    }
    canonical = aliases.get(value, value)
    try:
        backend = BACKENDS[canonical]
    except KeyError as exc:
        raise ValueError(
            f"backend must be one of {', '.join(sorted(BACKENDS))}; got {value!r}"
        ) from exc
    if config is not None:
        backend = apply_config_overrides(backend, config)
    return backend
