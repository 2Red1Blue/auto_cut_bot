"""语义模块工具函数 — 零耦合叶子节点。

从 semantic_handlers.py 提取的纯工具函数, 无内部依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any



def records_by_id(values: Any) -> dict[str, dict[str, Any]]:
    """按 id 字段索引记录列表, 过滤非法条目。

    原位置: semantic_handlers._records_by_id (L1583, 6L)
    """
    return {
        item["id"]: item
        for item in values or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def diagnostic_path(
    *,
    output_path: Path,
    job_id: str,
    signature: str,
    semantic_attempt: int,
    recorder: Any = None,
) -> Path:
    """构建语义诊断文件的输出路径。

    原位置: semantic_handlers.semantic_diagnostic_path (L3524, 20L)
    """
    from autocut_core.semantic.engine import JobRecorder, _sanitize_path_component

    invocation_id = (
        recorder.invocation_id
        if isinstance(recorder, JobRecorder)
        else f"untracked-{signature[:12]}"
    )
    return (
        output_path.parent
        / ".diagnostics"
        / _sanitize_path_component(job_id or output_path.stem)
        / invocation_id
        / f"semantic-attempt-{semantic_attempt:03d}.json"
    )


# 别名 — 兼容旧名称
semantic_diagnostic_path = diagnostic_path