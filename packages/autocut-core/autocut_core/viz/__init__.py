"""可视化后端 (可选启用) — 事件协议 + 编排器插桩 + 暂停/继续控制面。

仅在 ``autocut run/stage --viz`` 时激活; 未启用时编排器使用
NullEventEmitter (全 no-op), 且控制面检查仅一次 is_file, 行为与
原版逐字节一致。

子模块:
  - events: 运行事件协议 (PipelineEvent / EventEmitter / JSONL 落盘
    / 日志桥接) — 契约见模块 docstring 中的逐行 JSON 示例;
  - control: 控制文件协议 (pause / resume / pause_before) 与
    Stage 调度前的 check_and_wait 阻塞等待;
  - server: 本地观测服务 (viz 子命令, 延迟导入, 由独立任务实现)。
"""

from autocut_core.viz.control import check_and_wait, read_control, write_control
from autocut_core.viz.events import (
    EventEmitter,
    JsonlEventEmitter,
    NullEventEmitter,
    PipelineEvent,
    install_log_handler,
)

__all__ = [
    "PipelineEvent",
    "EventEmitter",
    "NullEventEmitter",
    "JsonlEventEmitter",
    "install_log_handler",
    "write_control",
    "read_control",
    "check_and_wait",
]
