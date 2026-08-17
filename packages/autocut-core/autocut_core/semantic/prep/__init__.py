"""autocut_core.semantic.prep — 语义预准备阶段模块。

从 prepare_story_stages.py 提取的 Episode / Chapter / Registry / Assignment /
Catalog / Script / Plan 准备函数。
"""

from __future__ import annotations

from autocut_core.semantic.prep.utils import (
    _by_ids,
    batch_payload,
    parse_story_treatment_locks,
    write_context,
)
from autocut_core.semantic.prep.episodes import (
    EPISODE_SEMANTIC_ROLLUP_FIELDS,
    FORCE_QWEN_DIGEST_ENV_FLAG,
    _first_non_empty,
    _force_qwen_digest_by_env,
    build_local_episode_digest,
    prepare_episodes,
)
from autocut_core.semantic.prep.chapters import (
    CHAPTER_SEMANTIC_ROLLUP_FIELDS,
    _compact_event,
    prepare_chapters,
)
from autocut_core.semantic.prep.registry_prep import (
    SEMANTIC_ROLLUP_STARVATION,
    SERIES_REGISTRY_PREFLIGHT_POLICY_VERSION,
    _rollup_field_counts,
    prepare_registry,
    series_registry_preflight,
)
from autocut_core.semantic.prep.assignments import (
    prepare_assignments,
)
from autocut_core.semantic.prep.catalog import (
    prepare_catalog,
)

__all__ = [
    # utils
    "_by_ids",
    "batch_payload",
    "parse_story_treatment_locks",
    "write_context",
    # episodes
    "EPISODE_SEMANTIC_ROLLUP_FIELDS",
    "FORCE_QWEN_DIGEST_ENV_FLAG",
    "_first_non_empty",
    "_force_qwen_digest_by_env",
    "build_local_episode_digest",
    "prepare_episodes",
    # chapters
    "CHAPTER_SEMANTIC_ROLLUP_FIELDS",
    "_compact_event",
    "prepare_chapters",
    # registry_prep
    "SEMANTIC_ROLLUP_STARVATION",
    "SERIES_REGISTRY_PREFLIGHT_POLICY_VERSION",
    "_rollup_field_counts",
    "prepare_registry",
    "series_registry_preflight",
    # assignments
    "prepare_assignments",
    # catalog
    "prepare_catalog",
    # scripts — import directly from autocut_core.semantic.prep.scripts
    # plans  — import directly from autocut_core.semantic.prep.plans
]