"""autocut_core.semantic.prep.utils — 预准备阶段通用工具函数。

从 prepare_story_stages.py 提取的纯工具函数。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any



def write_context(
    path: Path, value: dict[str, Any], max_chars: int
) -> int:
    """将 context 字典写入 JSON 文件, 并校验大小上限。

    原位置: prepare_story_stages.write_context (L307, 10L)
    """
    import json

    from autocut_core.io import atomic_write_json

    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) > max_chars:
        raise ValueError(
            f"context {path.name} has {len(text)} characters, above {max_chars}"
        )
    atomic_write_json(path, value)
    return len(text)


def batch_payload(job_root: Path, backend: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """构建语义批处理 manifest 的顶层 payload。

    原位置: prepare_story_stages.batch_payload (L319, 7L)
    """
    return {
        "schema_version": "1.0",
        "backend": backend,
        "cache_dir": str((job_root / ".story-cache").resolve()),
        "jobs": jobs,
    }


def _by_ids(records: list[dict[str, Any]], ids: set[str]) -> list[dict[str, Any]]:
    """按 id 集合过滤记录列表。

    原位置: prepare_story_stages._by_ids (L1041, 2L)
    """
    return [item for item in records if item.get("id") in ids]


def parse_story_treatment_locks(values: list[str] | None) -> dict[str, str]:
    """解析重复的 STORY_ID=TREATMENT_OPTION_ID 生成锁。

    原位置: prepare_story_stages.parse_story_treatment_locks (L3421, 22L)
    """
    locks: dict[str, str] = {}
    for raw_value in values or []:
        story_id, separator, option_id = str(raw_value).partition("=")
        story_id = story_id.strip()
        option_id = option_id.strip()
        if not separator or not story_id or not option_id:
            raise ValueError(
                "--lock-treatment-option must use "
                "STORY_ID=TREATMENT_OPTION_ID"
            )
        previous = locks.get(story_id)
        if previous is not None and previous != option_id:
            raise ValueError(
                f"conflicting Treatment generation locks for {story_id}: "
                f"{previous!r} vs {option_id!r}"
            )
        locks[story_id] = option_id
    return locks