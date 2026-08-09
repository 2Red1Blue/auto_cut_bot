# ruff: noqa: E501 — 模块 docstring 中的事件契约 JSON 示例为下游观测服务的
# 精确契约, 单行完整展示, 不参与行宽限制
"""可视化事件协议 — 流水线运行事件的 JSONL 落盘契约。

可选启用的可视化后端 (--viz) 把编排器运行期事件以 JSON Lines 格式
追加写入 ``{job_root}/.sd-viz/events.jsonl``, 下游观测服务 (viz server)
按本模块声明的契约消费。核心约束:

  - **绝不阻断流水线**: 全部写失败被吞咽, 降级为 warning 日志;
  - **单行 O_APPEND 追加**: 每事件一行紧凑 JSON, 不 fsync, 追加写入
    天然行原子 (单行远小于 PIPE_BUF), 观测端可随时尾随读取;
  - **seq 续增**: 同一 job_root 重跑时新 Emitter 从既有文件末行
    seq 续增, 保证事件序号全局单调;
  - **run_id**: 每个 Emitter 实例取 uuid4 hex 前 12 位, 区分多次运行。

events.jsonl 每行一个 PipelineEvent JSON 对象, 字段固定为::

    {"seq": int, "ts": ISO-8601 UTC 时间戳, "run_id": str,
     "type": 事件类型常量, "stage": Stage 名或 null, "payload": dict}

各事件类型的精确示例 (下游观测服务按此契约实现)::

    {"seq":1,"ts":"2026-08-05T08:30:00.123456+00:00","run_id":"3f2a9c1d7b40","type":"pipeline.started","stage":null,"payload":{"order":["source_windows","story_approval"],"stages":[{"name":"source_windows","is_human":false,"inputs":[],"outputs":["source_manifest"]},{"name":"story_approval","is_human":true,"inputs":[],"outputs":[]}]}}
    {"seq":2,"ts":"2026-08-05T08:30:01.000001+00:00","run_id":"3f2a9c1d7b40","type":"stage.started","stage":"source_windows","payload":{}}
    {"seq":3,"ts":"2026-08-05T08:30:02.000002+00:00","run_id":"3f2a9c1d7b40","type":"artifact.published","stage":"source_windows","payload":{"name":"source_manifest","sha256":"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08","path":"/job/.bus/9f/9f86d0.json"}}
    {"seq":4,"ts":"2026-08-05T08:30:02.000003+00:00","run_id":"3f2a9c1d7b40","type":"stage.completed","stage":"source_windows","payload":{"artifacts":[{"name":"source_manifest","sha256":"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"}]}}
    {"seq":5,"ts":"2026-08-05T08:30:03.000004+00:00","run_id":"3f2a9c1d7b40","type":"stage.failed","stage":"event_cards","payload":{"error":"boom","error_code":"stage_failed"}}
    {"seq":6,"ts":"2026-08-05T08:30:04.000005+00:00","run_id":"3f2a9c1d7b40","type":"stage.paused","stage":"story_scripts","payload":{}}
    {"seq":7,"ts":"2026-08-05T08:30:05.000006+00:00","run_id":"3f2a9c1d7b40","type":"stage.resumed","stage":"story_scripts","payload":{}}
    {"seq":8,"ts":"2026-08-05T08:30:06.000007+00:00","run_id":"3f2a9c1d7b40","type":"human.waiting","stage":"story_approval","payload":{}}
    {"seq":9,"ts":"2026-08-05T08:30:07.000008+00:00","run_id":"3f2a9c1d7b40","type":"log","stage":"source_windows","payload":{"level":"INFO","message":"stage starting"}}
    {"seq":10,"ts":"2026-08-05T08:30:08.000009+00:00","run_id":"3f2a9c1d7b40","type":"pipeline.completed","stage":null,"payload":{}}

``log`` 事件的 message 截断到 2000 字符; 只桥接带 stage 结构化字段
的日志记录 (见 install_log_handler)。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from autocut_core.io import utc_now
from autocut_core.logging import ROOT_LOGGER_NAME, get_logger

logger = get_logger(__name__)

__all__ = [
    "PipelineEvent",
    "EventEmitter",
    "NullEventEmitter",
    "JsonlEventEmitter",
    "install_log_handler",
    "VIZ_DIR_NAME",
    "EVENTS_FILENAME",
    # 事件类型常量
    "EV_PIPELINE_STARTED",
    "EV_PIPELINE_COMPLETED",
    "EV_STAGE_STARTED",
    "EV_STAGE_COMPLETED",
    "EV_STAGE_FAILED",
    "EV_STAGE_PAUSED",
    "EV_STAGE_RESUMED",
    "EV_HUMAN_WAITING",
    "EV_ARTIFACT_PUBLISHED",
    "EV_LOG",
]

# ── 文件约定 ────────────────────────────────────────────────────────────────

# 可视化后端在 job_root 下的专属目录 (与 project.json 等业务文件隔离)
VIZ_DIR_NAME = ".sd-viz"
EVENTS_FILENAME = "events.jsonl"

# log 事件 message 截断长度 — 防止超长日志撑爆事件流
_LOG_MESSAGE_LIMIT = 2000

# ── 事件类型常量 ────────────────────────────────────────────────────────────

EV_PIPELINE_STARTED = "pipeline.started"
EV_PIPELINE_COMPLETED = "pipeline.completed"
EV_STAGE_STARTED = "stage.started"
EV_STAGE_COMPLETED = "stage.completed"
EV_STAGE_FAILED = "stage.failed"
EV_STAGE_PAUSED = "stage.paused"
EV_STAGE_RESUMED = "stage.resumed"
EV_HUMAN_WAITING = "human.waiting"
EV_ARTIFACT_PUBLISHED = "artifact.published"
EV_LOG = "log"


# ── 事件模型 ────────────────────────────────────────────────────────────────


class PipelineEvent(BaseModel):
    """单条流水线事件 — events.jsonl 的一行 (见模块 docstring 示例)。

    seq 在同一 events.jsonl 内单调递增 (跨运行续增);
    ts 为 ISO-8601 UTC 时间戳 (与 io.utc_now 同基准);
    stage 无 Stage 归属的事件 (如 pipeline.*) 为 None。
    """

    seq: int
    ts: str
    run_id: str
    type: str
    stage: str | None = None
    payload: dict[str, Any]


# ── Emitter 抽象 ────────────────────────────────────────────────────────────


class EventEmitter(ABC):
    """事件发射器抽象 — 编排器插桩点统一依赖此接口。"""

    @abstractmethod
    def emit(self, type: str, *, stage: str | None = None, **payload_fields: Any) -> None:
        """发射一条事件; payload 由关键字参数平铺构成。实现必须自吞异常。"""
        ...


class NullEventEmitter(EventEmitter):
    """空实现 (默认) — 未启用 --viz 时全部事件 no-op, 零开销。"""

    def emit(self, type: str, *, stage: str | None = None, **payload_fields: Any) -> None:
        return None


class JsonlEventEmitter(EventEmitter):
    """JSONL 落盘实现 — 追加写入 ``{job_root}/.sd-viz/events.jsonl``。

    - O_APPEND 单行追加, 不 fsync (事件丢失可接受, 阻断流水线不可接受);
    - seq 类内自增; 构造时若文件已存在, 从末行 seq 续增 (读失败回退 1);
    - run_id 取 uuid4 hex 前 12 位;
    - 任何写/读失败一律 try/except 吞咽, 降级 logger.warning。
    """

    def __init__(self, job_root: Path | str) -> None:
        self._path = Path(job_root) / VIZ_DIR_NAME / EVENTS_FILENAME
        self.run_id: str = uuid.uuid4().hex[:12]
        self._seq: int = self._resume_seq()
        self._lock = threading.Lock()

    # ── 内部 ──────────────────────────────────────────────────────

    def _resume_seq(self) -> int:
        """从既有事件文件末行恢复 seq 起点; 任何异常回退 1。"""
        try:
            if not self._path.is_file():
                return 1
            last_seq = 0
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last_seq = int(json.loads(line).get("seq", 0))
                    except (ValueError, TypeError, AttributeError):
                        continue  # 末行损坏 — 保留上一个可解析的 seq
            return last_seq + 1
        except Exception as exc:  # noqa: BLE001 — 事件层绝不阻断流水线
            logger.warning("viz: 读取既有事件文件失败, seq 从 1 开始: %s", exc)
            return 1

    # ── EventEmitter 接口 ─────────────────────────────────────────

    def emit(self, type: str, *, stage: str | None = None, **payload_fields: Any) -> None:
        try:
            with self._lock:
                event = PipelineEvent(
                    seq=self._seq,
                    ts=utc_now(),
                    run_id=self.run_id,
                    type=type,
                    stage=stage,
                    payload=dict(payload_fields),
                )
                self._seq += 1
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = event.model_dump_json() + "\n"
            # O_APPEND 单行追加不 fsync — 紧凑单行远小于 PIPE_BUF,
            # POSIX 保证追加写入行级原子, 观测端尾随读取安全
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception as exc:  # noqa: BLE001 — 事件层绝不阻断流水线
            logger.warning("viz: 事件写入失败 (已忽略): type=%s err=%s", type, exc)


# ── 日志桥接 ────────────────────────────────────────────────────────────────

# LogRecord 上结构化字段的存放键 — 与 logging._FIELDS_ATTR 保持一致
# (fields(stage=...) 写入的日志才会携带该属性)
_FIELDS_ATTR = "sd_structured_fields"


class _EventLogHandler(logging.Handler):
    """把带 stage 结构化字段的日志记录转为 ``log`` 事件的桥接 handler。

    - 只桥接 ``fields(stage=...)`` 标记过的记录, 避免噪音事件;
    - handler 内全吞异常; 线程局部再入守卫防止
      "写入失败 → warning 日志 → handler → 再写入" 的递归。
    """

    def __init__(self, emitter: EventEmitter) -> None:
        super().__init__()
        self._emitter = emitter
        self._in_emit = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._in_emit, "active", False):
            return
        try:
            self._in_emit.active = True
            extras = getattr(record, _FIELDS_ATTR, None)
            stage = extras.get("stage") if isinstance(extras, dict) else None
            if stage is None:
                return
            self._emitter.emit(
                EV_LOG,
                stage=str(stage),
                level=record.levelname,
                message=record.getMessage()[:_LOG_MESSAGE_LIMIT],
            )
        except Exception:  # noqa: BLE001 — 日志桥接绝不影响主流程
            pass
        finally:
            self._in_emit.active = False


def install_log_handler(emitter: EventEmitter) -> logging.Handler:
    """为 ``autocut_core`` 根 logger 安装事件桥接 handler 并返回它。

    挂载后所有带 stage 结构化字段的日志同步产生 ``log`` 事件;
    调用方负责在不再需要时 ``logger.removeHandler(返回值)`` (测试场景)。
    """
    handler = _EventLogHandler(emitter)
    logging.getLogger(ROOT_LOGGER_NAME).addHandler(handler)
    return handler
