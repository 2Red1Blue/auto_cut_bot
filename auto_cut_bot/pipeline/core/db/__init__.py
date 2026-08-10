"""autocut_core/db/ — Stage-level DB client for pipeline CRUD operations.

提供:
  - ``StageDBClient``: 按需实例化的 DB 客户端, 覆盖全部 10 张表的 CRUD 操作。
    DB 不可用时所有方法自动降级为 no-op, 不阻塞流水线。

导入: ``from autocut_core.db import StageDBClient``
"""

from __future__ import annotations

from auto_cut_bot.pipeline.core.db.client import StageDBClient

__all__ = ["StageDBClient"]
