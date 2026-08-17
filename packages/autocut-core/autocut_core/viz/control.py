"""可视化控制面 — 通过控制文件实现暂停/继续/单步前暂停。

控制文件约定 ``{job_root}/.sd-viz/control.json``::

    {"action": "pause" | "resume" | "pause_before",
     "target_stage": str | null,
     "requested_at": ISO-8601 UTC 时间戳}

  - ``pause``: 全局暂停 — 编排器在下一个 Stage 调度前阻塞;
  - ``pause_before``: 仅当 ``target_stage`` 等于即将执行的 Stage 时阻塞;
  - ``resume``: 解除暂停 — 阻塞中的编排器检测到后继续执行,
    并把控制文件改写为无暂停状态 (action=resume) 防止重复触发。

写入端 (观测服务/人工工具) 用 write_control 原子写入控制指令;
编排器在每个 Stage 调度前调用 check_and_wait — 控制文件不存在时
开销仅一次 is_file 检查, 未启用可视化后端时行为与原版完全一致。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from autocut_core.io import atomic_write_json, load_json, utc_now
from autocut_core.logging import fields, get_logger
from autocut_core.viz.events import (
    EV_STAGE_PAUSED,
    EV_STAGE_RESUMED,
    VIZ_DIR_NAME,
    EventEmitter,
)

logger = get_logger(__name__)

__all__ = ["CONTROL_FILENAME", "write_control", "read_control", "check_and_wait"]

CONTROL_FILENAME = "control.json"

# 阻塞等待 resume 的轮询间隔 (秒)
_POLL_INTERVAL_SECONDS = 1.0


def _control_path(job_root: Path) -> Path:
    return Path(job_root) / VIZ_DIR_NAME / CONTROL_FILENAME


def write_control(
    job_root: Path, action: str, target_stage: str | None = None
) -> Path:
    """原子写入控制文件 (复用 io.atomic_write_json), 返回文件路径。"""
    payload = {"action": action, "target_stage": target_stage, "requested_at": utc_now()}
    path = _control_path(job_root)
    atomic_write_json(path, payload)
    return path


def read_control(job_root: Path) -> dict[str, Any] | None:
    """读取控制文件; 不存在/非法/读取失败一律返回 None (视为无控制指令)。"""
    path = _control_path(job_root)
    try:
        if not path.is_file():
            return None
        data = load_json(path)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001 — 控制面异常不得阻断流水线
        logger.warning("viz: 控制文件读取失败 (已忽略): %s", exc)
        return None


def _should_pause(control: dict[str, Any] | None, next_stage: str) -> bool:
    """全局 pause 无条件命中; pause_before 仅 target_stage 匹配时命中。"""
    if not isinstance(control, dict):
        return False
    action = control.get("action")
    if action == "pause":
        return True
    return action == "pause_before" and control.get("target_stage") == next_stage


def check_and_wait(job_root: Path, next_stage: str, emitter: EventEmitter) -> None:
    """Stage 调度前的控制面检查 — 命中暂停时阻塞直到 resume。

    - 控制文件不存在: 一次 is_file 后直接返回 (零额外开销);
    - 命中全局 pause 或 pause_before == next_stage: emit ``stage.paused``,
      随后按 1s 间隔轮询, 直到 action 变为 resume — emit ``stage.resumed``
      并把控制文件改写为无暂停状态;
    - 轮询期间的文件读异常被捕获后继续等待 (不因瞬时 IO 抖动崩溃)。
    """
    path = _control_path(job_root)
    if not path.is_file():
        return

    if not _should_pause(read_control(job_root), next_stage):
        return

    logger.info("viz: 收到暂停指令, 等待 resume", extra=fields(stage=next_stage))
    emitter.emit(EV_STAGE_PAUSED, stage=next_stage)
    while True:
        time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            control = read_control(job_root)
        except Exception:  # noqa: BLE001 — 容错继续等待
            control = None
        if isinstance(control, dict) and control.get("action") == "resume":
            break
    emitter.emit(EV_STAGE_RESUMED, stage=next_stage)
    # 改写为无暂停状态 — 避免后续 Stage 被同一指令重复阻塞;
    # 写失败不阻断: 下一轮 check 读到旧 pause 仍会正常等待
    try:
        write_control(job_root, "resume")
    except Exception as exc:  # noqa: BLE001 — 控制面异常不得阻断流水线
        logger.warning("viz: 控制文件复位失败 (已忽略): %s", exc)
    logger.info("viz: 收到 resume 指令, 继续执行", extra=fields(stage=next_stage))
