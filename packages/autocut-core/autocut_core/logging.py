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

# 日志级别可通过环境变量 ``AC_PIPELINE_LOG_LEVEL`` 覆盖
# (DEBUG/INFO/WARNING/ERROR), 默认 INFO。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# 东八区时区
_CST = timezone(timedelta(hours=8))

__all__ = [
    "ROOT_LOGGER_NAME",
    "LOG_LEVEL_ENV",
    "StructuredFormatter",
    "get_logger",
    "configure_logging",
    "configure_file_logging",
    "get_log_directory",
    "fields",
]

# 全部核心模块 logger 的统一前缀 — caplog / 宿主应用可按此过滤
ROOT_LOGGER_NAME = "autocut_core"

# 环境变量: 覆盖默认日志级别
LOG_LEVEL_ENV = "AC_PIPELINE_LOG_LEVEL"

# 环境变量: 日志目录与保留天数
LOG_DIR_ENV = "AC_PIPELINE_LOG_DIR"
LOG_RETENTION_DAYS_ENV = "AC_PIPELINE_LOG_RETENTION_DAYS"

# 结构化字段在 LogRecord.__dict__ 中的存放键
_FIELDS_ATTR = "sd_structured_fields"

# 日志行格式: 时间戳 | 级别 | 模块名 | 消息 | 结构化字段
_LINE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
# 不用 %z (time.strftime 对 %z 始终按本地时区计算) — 改为手工拼接时区后缀
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
_TZ_SUFFIX = "+0800"


class StructuredFormatter(logging.Formatter):
    """结构化格式器 — 东八区 (UTC+8) 时间戳 + 行尾 key=value 结构化字段。

    输出示例 (时间戳 | 级别 | 模块名 | 消息 | 结构化字段)::

        2026-08-05T16:30:00+0800 INFO autocut_core.x :: starting stage=s1
    """

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
        """东八区 (UTC+8) 时间戳, 固定 +0800 后缀。"""
        cst_time = datetime.fromtimestamp(record.created, tz=_CST)
        stamp = cst_time.strftime(datefmt or _DATE_FORMAT)
        return f"{stamp}{_TZ_SUFFIX}"


def fields(**kwargs: object) -> dict[str, object]:
    """构造结构化字段字典 — 用法: ``logger.info("msg", extra=fields(stage=name))``。"""
    return {_FIELDS_ATTR: kwargs}


def get_log_directory() -> Path:
    """获取日志目录 — 环境变量 ``AC_PIPELINE_LOG_DIR`` 或默认 ``./logs``。"""
    log_dir = os.environ.get(LOG_DIR_ENV, "./logs")
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_file_logging(
    log_dir: Path | str | None = None,
    *,
    prefix: str = "pipeline",
    when: str = "midnight",
    interval: int = 1,
    backup_count: int | None = None,
    encoding: str = "utf-8",
) -> logging.Handler:
    """配置文件日志处理器 — 按时间轮转 + 自动归档。

    Args:
        log_dir: 日志目录，默认从 ``AC_PIPELINE_LOG_DIR`` 或 ``./logs``
        prefix: 日志文件名前缀，默认 ``pipeline``
        when: 轮转时间单位（S/M/H/D/W0-W6/midnight），默认 ``midnight``（每天午夜）
        interval: 轮转间隔，默认 1（每天）
        backup_count: 保留的日志文件数量，None 时从 ``AC_PIPELINE_LOG_RETENTION_DAYS`` 读取（默认 30）
        encoding: 文件编码，默认 UTF-8

    Returns:
        配置的 TimedRotatingFileHandler

    日志文件名格式：``{prefix}-{YYYYMMDD}.log``，轮转后自动重命名为
    ``{prefix}-{YYYYMMDD}.log.{YYYY-MM-DD}``，例如：
    - 当前日志：``pipeline-20260811.log``
    - 归档日志：``pipeline-20260811.log.2026-08-11``

    用法示例::

        from autocut_core.logging import configure_file_logging, get_log_directory
        handler = configure_file_logging(get_log_directory())
        logger.addHandler(handler)
    """
    if log_dir is None:
        log_dir = get_log_directory()
    else:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

    if backup_count is None:
        backup_count = int(os.environ.get(LOG_RETENTION_DAYS_ENV, "30"))

    # 生成带日期的日志文件名（东八区日期）
    date_str = datetime.now(_CST).strftime("%Y%m%d")
    log_filename = f"{prefix}-{date_str}.log"
    log_path = log_dir / log_filename

    # 创建 TimedRotatingFileHandler
    handler = TimedRotatingFileHandler(
        filename=str(log_path),
        when=when,
        interval=interval,
        backupCount=backup_count,
        encoding=encoding,
        utc=False,  # 使用本地时间（与 StructuredFormatter 东八区一致）
    )

    # 设置归档文件名格式：保留原始日期 + 归档日期（东八区）
    handler.namer = lambda name: f"{name}.{datetime.now(_CST).strftime('%Y-%m-%d')}"

    handler.setFormatter(StructuredFormatter())

    return handler


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
    file_logging: bool = False,
    log_dir: Path | str | None = None,
) -> logging.Logger:
    """为核心 logger 安装结构化 handler (CLI 入口调用一次)。

    - level: 显式级别; None 时读 ``AC_PIPELINE_LOG_LEVEL``, 再缺省 INFO;
    - stream: 输出流, 默认 sys.stdout (stdout 在 emit 时动态解析);
    - force: True 时移除既有 handler 重新安装 (幂等默认关闭);
    - file_logging: True 时启用文件日志（按天轮转 + 自动归档）;
    - log_dir: 文件日志目录，默认从 ``AC_PIPELINE_LOG_DIR`` 或 ``./logs``。

    handler 只挂在 ``autocut_core`` 根 logger 上, 并保持
    propagate=True — pytest caplog 与宿主应用的 handler 不受影响。

    文件日志特性:
    - 按天轮转（每天午夜生成新文件）
    - 自动归档旧日志（文件名追加日期后缀）
    - 默认保留 30 天（可通过 ``AC_PIPELINE_LOG_RETENTION_DAYS`` 配置）
    - 日志文件名格式：``pipeline-{YYYYMMDD}.log``
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

    # 添加控制台 handler
    stream_handler = logging.StreamHandler(stream or sys.stdout)
    stream_handler.setFormatter(StructuredFormatter())
    root.addHandler(stream_handler)

    # 可选：添加文件 handler（按天轮转）
    if file_logging:
        file_handler = configure_file_logging(log_dir)
        root.addHandler(file_handler)

    root.setLevel(level)
    #logging.Logger 类本身没有 _sd_configured 属性，这是动态添加的，# type: ignore[attr-defined] 告诉类型检查器忽略这一行的 attr-defined 类别错误，但不影响运行时行为。
    root._sd_configured = True  # type: ignore[attr-defined]
    return root
