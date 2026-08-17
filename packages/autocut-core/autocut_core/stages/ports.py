"""LLMPort — Stage 调用 LLM 的统一抽象接口。

各项目（ac_auto_cut, auto_cut_bot）提供自己的 LLMPort 实现，
在启动时通过 register_llm_port_factory() 注册工厂函数。
Stage 通过 get_llm_port() 获取已注册的实现，共享包不依赖具体项目。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable
import threading


class LLMPort(ABC):
    """Stage 调用 LLM 的统一接口。

    各项目提供自己的 LLMPort 实现并通过 register_llm_port_factory() 注册。
    Stage 通过 get_llm_port() 获取已注册的实现。
    """

    @abstractmethod
    def run_batch(
        self,
        manifest_path: Path,
        *,
        backend: str,
        workers: int | str,
        requests_per_minute: float,
        semantic_retries: int,
        context_injection: dict[str, Any] | None = None,
        job_ids: list[str] | None = None,
    ) -> None:
        """执行批量 LLM 推理。

        Args:
            manifest_path: batch manifest 文件路径
            backend: LLM 后端名称 (e.g. 'qwen', 'doubao')
            workers: 并发数
            requests_per_minute: 速率限制
            semantic_retries: 语义重试次数
            context_injection: 注入到每个窗口的上下文 (global_context, skill 等)
            job_ids: 可选 — 仅执行指定 job ID 子集 (None = 全部)
        """
        ...

    @abstractmethod
    def build_context_injection(
        self,
        stage_name: str,
        config: Any,
        bus: Any,
    ) -> dict[str, Any] | None:
        """构建注入到 LLM prompt 的上下文。

        Args:
            stage_name: 当前 stage 名称 (e.g. 'vlm_analysis')
            config: PipelineConfig 实例
            bus: ArtifactBus 实例

        Returns:
            上下文字典，或 None（不需要注入）
        """
        ...

    def call_llm(
        self,
        prompt: str,
        model: str,
        *,
        messages: list[dict[str, str]] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 131072,
        response_format: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """单次 LLM 调用（非批量）。

        默认抛出 NotImplementedError，子类可选实现。
        """
        raise NotImplementedError("call_llm 未实现 — 子类需覆盖此方法")


# ── LLMPort 工厂注册表 ──────────────────────────────────────────────────
# 各项目在启动时注册自己的 LLMPort 工厂函数。
# 共享包不 import 任何具体项目的模块 — 依赖反转。

_LLM_PORT_FACTORIES: list[Callable[[], LLMPort]] = []
_LLM_PORT_LOCK = threading.Lock()
_LLM_PORT_AUTO_DISCOVERED = False


def register_llm_port_factory(factory: Callable[[], LLMPort]) -> None:
    """注册 LLMPort 工厂函数。

    各项目在启动时调用 (或在模块加载时自动调用):
        register_llm_port_factory(lambda: PipelineLLMPort())

    多次注册按 LIFO 顺序尝试 — 最后注册的优先。
    """
    with _LLM_PORT_LOCK:
        _LLM_PORT_FACTORIES.append(factory)


def get_llm_port() -> LLMPort:
    """获取 LLMPort 实例 — 按注册顺序尝试工厂函数。

    优先使用已注册的工厂; 若均未注册则通过 entry_point
    ``ac_cutflow.llm_port`` 自动发现 (延迟发现, 仅首次调用时触发)。
    均未找到时抛出 RuntimeError。
    """
    global _LLM_PORT_AUTO_DISCOVERED

    # 1. 已注册的工厂 (LIFO — 最后注册的优先)
    for factory in reversed(_LLM_PORT_FACTORIES):
        try:
            return factory()
        except ImportError:
            continue

    # 2. 延迟 entry_point 自动发现 (仅一次)
    if not _LLM_PORT_AUTO_DISCOVERED:
        _LLM_PORT_AUTO_DISCOVERED = True
        try:
            from importlib.metadata import entry_points as _eps
            for ep in _eps().select(group="ac_cutflow.llm_port"):
                factory = ep.load()
                try:
                    return factory()
                except ImportError:
                    continue
        except Exception:
            pass

    raise RuntimeError(
        "未注册 LLMPort 工厂 — 各项目需在启动时调用 "
        "register_llm_port_factory() 或注册 entry_point "
        "'ac_cutflow.llm_port'"
    )