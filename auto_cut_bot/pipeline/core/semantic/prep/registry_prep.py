"""autocut_core.semantic.prep.registry_prep — Series Registry 批处理准备。

从 prepare_story_stages.py 提取 Registry 相关函数。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


from autocut_core.semantic.prep.chapters import CHAPTER_SEMANTIC_ROLLUP_FIELDS, _compact_event
from autocut_core.semantic.prep.episodes import EPISODE_SEMANTIC_ROLLUP_FIELDS
from autocut_core.semantic.prep.utils import batch_payload, write_context


# ── constants ────────────────────────────────────────────────────────

SEMANTIC_ROLLUP_STARVATION = "SEMANTIC_ROLLUP_STARVATION"
SERIES_REGISTRY_PREFLIGHT_POLICY_VERSION = (
    "series-registry-semantic-rollup-preflight-v1"
)


# ── helpers ──────────────────────────────────────────────────────────

def _rollup_field_counts(
    items: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> dict[str, int]:
    """统计 semantic rollup 字段中非空列表的元素数量。

    原位置: prepare_story_stages._rollup_field_counts (L534, 13L)
    """
    return {
        field: sum(
            len(item.get(field, []) or [])
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get(field, []), list)
        )
        for field in fields
    }


# ── public API ───────────────────────────────────────────────────────

def series_registry_preflight(
    *,
    episodes: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Detect an Event-rich series whose semantic rollups are all empty.

    原位置: prepare_story_stages.series_registry_preflight (L549, 54L)
    """
    episode_counts = _rollup_field_counts(
        episodes, EPISODE_SEMANTIC_ROLLUP_FIELDS
    )
    chapter_counts = _rollup_field_counts(
        chapters, CHAPTER_SEMANTIC_ROLLUP_FIELDS
    )
    episode_total = sum(episode_counts.values())
    chapter_total = sum(chapter_counts.values())
    episode_numbers = {
        item.get("episode")
        for item in episodes
        if isinstance(item, dict) and isinstance(item.get("episode"), int)
    }
    event_episode_numbers = {
        item.get("episode")
        for item in events
        if isinstance(item, dict) and isinstance(item.get("episode"), int)
    }
    event_rich = (
        len(episode_numbers) >= 2
        and len(events) >= len(episode_numbers)
        and len(event_episode_numbers & episode_numbers) >= 2
    )
    blocked = event_rich and episode_total == 0 and chapter_total == 0
    return {
        "schema_version": "1.0",
        "policy_version": SERIES_REGISTRY_PREFLIGHT_POLICY_VERSION,
        "status": "blocked" if blocked else "pass",
        "failure_codes": (
            [SEMANTIC_ROLLUP_STARVATION] if blocked else []
        ),
        "episode_count": len(episode_numbers),
        "chapter_count": len(chapters),
        "event_count": len(events),
        "event_episode_count": len(event_episode_numbers & episode_numbers),
        "event_rich": event_rich,
        "episode_rollup_counts": episode_counts,
        "chapter_rollup_counts": chapter_counts,
        "diagnosis": (
            "Event-rich multi-episode input has zero Episode and Chapter "
            "semantic rollups; regenerate Chapter Digests with Event context "
            "before requesting the Series Registry."
            if blocked
            else None
        ),
    }


def prepare_registry(args: argparse.Namespace) -> Path:
    """准备 Series Registry 语义批处理 manifest。

    原位置: prepare_story_stages.prepare_registry (L605, 83L)
    """
    from autocut_core.io import (
        atomic_write_json,
        json_sha256,
        load_jsonl,
        sha256_file,
        update_project_stage,
    )

    job_root = args.job_root.resolve()
    episodes = load_jsonl(args.episode_digests)
    chapters = load_jsonl(args.chapter_digests)
    events = load_jsonl(args.event_cards)
    preflight = series_registry_preflight(
        episodes=episodes,
        chapters=chapters,
        events=events,
    )
    preflight["inputs"] = {
        "episode_digests": {
            "path": str(args.episode_digests.resolve()),
            "sha256": sha256_file(args.episode_digests),
        },
        "chapter_digests": {
            "path": str(args.chapter_digests.resolve()),
            "sha256": sha256_file(args.chapter_digests),
        },
        "event_cards": {
            "path": str(args.event_cards.resolve()),
            "sha256": sha256_file(args.event_cards),
        },
    }
    preflight_path = job_root / "series-registry-preflight.json"
    atomic_write_json(preflight_path, preflight)
    if preflight["status"] == "blocked":
        raise ValueError(
            f"{SEMANTIC_ROLLUP_STARVATION}: "
            + str(preflight["diagnosis"])
            + f" See {preflight_path}"
        )
    context = {
        "schema_version": "1.0",
        "series_id": "series",
        "chapter_digests": chapters,
        "episode_thread_index": [
            {
                "episode": item.get("episode"),
                "summary": item.get("summary"),
                "story_thread_updates": item.get("story_thread_updates", []),
            }
            for item in episodes
        ],
        "event_index": [_compact_event(event) for event in events],
        "registry_contract": {
            "global_ids_are_stable": True,
            "chapter_local_keys_must_be_normalized": True,
            "story_threads_declare_arc_or_coda": True,
            "thread_beats_are_out_of_scope": True,
            "coverage_is_computed_locally": True,
        },
    }
    context_path = job_root / "intermediate" / "series-registry-context.json"
    output_path = job_root / "series-registry.json"
    write_context(context_path, context, args.max_context_chars)
    manifest_path = job_root / "series-registry-batch.json"
    atomic_write_json(
        manifest_path,
        batch_payload(
            job_root,
            args.backend,
            [
                {
                    "id": "series-registry",
                    "task": "series_registry",
                    "stage_version": (
                        "story-first-series-registry-v9-typed-coda-v1"
                    ),
                    "context_file": str(context_path.resolve()),
                    "output": str(output_path.resolve()),
                }
            ],
        ),
    )
    update_project_stage(
        job_root / "project.json",
        "series_registry_job",
        "prepared",
        outputs={"batch_manifest": str(manifest_path)},
    )
    return manifest_path