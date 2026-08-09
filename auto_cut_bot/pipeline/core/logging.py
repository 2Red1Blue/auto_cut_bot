"""统一结构化日志工具 — autocut_core 的唯一日志入口。

职责:
  - ``get_logger(name)``: 获取挂载在 ``autocut_core`` 层级下的
    logger (核心模块、orchestrator、registry、rules 引擎统一使用);
  - ``configure_logging()``: 为 CLI 入口安装结构化 handler
    (时间戳/级别/模块名/结构化字段), 库模式下不安装 handler,
    由宿主应用决定日志去向;
  - ``fields(**kv)``: 构造结构化字段 — 通过 ``extra=fields(...)``
    附加到日志记录, 由 StructuredFormatter 渲染为行尾
    ``key=value`` 键值对 (机器可解析, 不破坏消息正文)。

日志级别可通过环境变量 ``SD_PIPELINE_LOG_LEVEL`` 覆盖
(DEBUG/INFO/WARNING/ERROR), 默认 INFO。
"""

from __future__ import annotations

import logging
import os
import sys
import time

__all__ = [
    "ROOT_LOGGER_NAME",
    "LOG_LEVEL_ENV",
    "StructuredFormatter",
    "get_logger",
    "configure_logging",
    "fields",
]

# 全部核心模块 logger 的统一前缀 — caplog / 宿主应用可按此过滤
ROOT_LOGGER_NAME = "autocut_core"

# 环境变量: 覆盖默认日志级别
LOG_LEVEL_ENV = "SD_PIPELINE_LOG_LEVEL"

# 结构化字段在 LogRecord.__dict__ 中的存放键
_FIELDS_ATTR = "sd_structured_fields"

# 日志行格式: 时间戳 | 级别 | 模块名 | 消息 | 结构化字段
_LINE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
# 不用 %z (time.strftime 对 %z 始终按本地时区计算) — 改为手工拼接 UTC 后缀
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
_UTC_SUFFIX = "+0000"


class StructuredFormatter(logging.Formatter):
    """结构化格式器 — UTC 时间戳 + 行尾 key=value 结构化字段。

    输出示例 (时间戳 | 级别 | 模块名 | 消息 | 结构化字段)::

        2026-08-05T08:30:00+0000 INFO autocut_core.x :: starting stage=s1
    """

    converter = time.gmtime

    def __init__(self) -> None:
        super().__init__(fmt=_LINE_FORMAT, datefmt=_DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = getattr(record, _FIELDS_ATTR, None)
        if extras:
            rendered = " ".join(f"{key}={value}" for key, value in extras.items())
            line = f"{line} {rendered}"
        return line

    def formatTime(
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        """UTC 时间戳 (与 io.utc_now 同一时区基准), 固定 +0000 后缀。"""
        stamp = time.strftime(datefmt or _DATE_FORMAT, time.gmtime(record.created))
        return f"{stamp}{_UTC_SUFFIX}"


def fields(**kwargs: object) -> dict[str, object]:
    """构造结构化字段字典 — 用法: ``logger.info("msg", extra=fields(stage=name))``。"""
    return {_FIELDS_ATTR: kwargs}


def get_logger(name: str) -> logging.Logger:
    """获取统一前缀下的 logger。

    name 已带 ``autocut_core`` 前缀 (如 ``__name__``) 时原样使用,
    否则补前缀 — 保证全部核心日志挂在同一层级, 便于统一配置与过滤。
    """
    if name == ROOT_LOGGER_NAME or name.startswith(f"{ROOT_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def configure_logging(
    level: int | str | None = None,
    *,
    stream=None,
    force: bool = False,
) -> logging.Logger:
    """为核心 logger 安装结构化 handler (CLI 入口调用一次)。

    - level: 显式级别; None 时读 ``SD_PIPELINE_LOG_LEVEL``, 再缺省 INFO;
    - stream: 输出流, 默认 sys.stdout (stdout 在 emit 时动态解析);
    - force: True 时移除既有 handler 重新安装 (幂等默认关闭)。

    handler 只挂在 ``autocut_core`` 根 logger 上, 并保持
    propagate=True — pytest caplog 与宿主应用的 handler 不受影响。
    """
    root = logging.getLogger(ROOT_LOGGER_NAME)
    if getattr(root, "_sd_configured", False) and not force:
        return root

    if level is None:
        level = os.environ.get(LOG_LEVEL_ENV, "INFO")
    if isinstance(level, str):
        level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    #logging.Logger 类本身没有 _sd_configured 属性，这是动态添加的，# type: ignore[attr-defined] 告诉类型检查器忽略这一行的 attr-defined 类别错误，但不影响运行时行为。
    root._sd_configured = True  # type: ignore[attr-defined]
    return root
