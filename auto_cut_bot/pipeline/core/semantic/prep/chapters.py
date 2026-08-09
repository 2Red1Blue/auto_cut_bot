"""autocut_core.semantic.prep.chapters — Chapter Digest 批处理准备。

从 prepare_story_stages.py 提取 Chapter 相关函数。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


from autocut_core.semantic.prep.utils import batch_payload, write_context


# ── constants ────────────────────────────────────────────────────────

CHAPTER_SEMANTIC_ROLLUP_FIELDS = (
    "character_rollup",
    "relationship_rollup",
    "story_threads",
    "fact_keys",
    "open_question_keys",
)


# ── helpers ──────────────────────────────────────────────────────────

def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    """将完整 Event 字典压缩为只包含语义必需字段的紧凑版本。

    原位置: prepare_story_stages._compact_event (L495, 18L)
    """
    return {
        key: event.get(key)
        for key in (
            "id",
            "episode",
            "source_id",
            "summary",
            "function",
            "character_names",
            "cause",
            "effect",
            "open_question",
            "temporal_mode",
            "candidate_ids",
        )
    }


# ── public API ───────────────────────────────────────────────────────

def prepare_chapters(args: argparse.Namespace) -> Path:
    """准备 Chapter Digest 语义批处理 manifest。

    原位置: prepare_story_stages.prepare_chapters (L426, 67L)
    """
    from autocut_core.io import atomic_write_json, load_jsonl, update_project_stage

    job_root = args.job_root.resolve()
    episodes = load_jsonl(args.episode_digests)
    if not episodes:
        raise ValueError("episode digests are empty")
    episodes.sort(key=lambda item: item["episode"])
    event_cards_arg = getattr(args, "event_cards", None)
    event_cards_path = (
        event_cards_arg.expanduser().resolve()
        if isinstance(event_cards_arg, Path)
        else (job_root / "event-cards.jsonl").resolve()
    )
    if isinstance(event_cards_arg, Path) and not event_cards_path.is_file():
        raise FileNotFoundError(
            f"explicit Chapter Event Cards file is missing: {event_cards_path}"
        )
    events = load_jsonl(event_cards_path) if event_cards_path.is_file() else []
    jobs = []
    context_dir = job_root / "intermediate" / "chapter-contexts"
    output_dir = job_root / "chapter-digest-results"
    for index in range(0, len(episodes), args.episodes_per_chapter):
        group = episodes[index : index + args.episodes_per_chapter]
        first, last = group[0]["episode"], group[-1]["episode"]
        chapter_id = f"chapter-{first:03d}-{last:03d}"
        chapter_episodes = [item["episode"] for item in group]
        chapter_episode_set = set(chapter_episodes)
        chapter_events = [
            _compact_event(item)
            for item in events
            if item.get("episode") in chapter_episode_set
        ]
        context = {
            "schema_version": "1.0",
            "chapter_id": chapter_id,
            "episodes": chapter_episodes,
            "episode_digests": group,
            "event_index": chapter_events,
            "chapter_evidence_contract": {
                "event_cards_available": bool(events),
                "event_cards_are_primary_evidence_when_episode_rollups_empty": True,
                "all_rollup_evidence_ids_must_reference_event_index": bool(events),
                "do_not_leave_semantic_rollups_empty_when_events_support_them": True,
            },
        }
        context_path = context_dir / f"{chapter_id}.json"
        output_path = output_dir / f"{chapter_id}.json"
        write_context(context_path, context, args.max_context_chars)
        jobs.append(
            {
                "id": chapter_id,
                "task": "chapter_digest",
                "stage_version": (
                    "story-first-chapter-digest-v2-event-context"
                ),
                "context_file": str(context_path.resolve()),
                "output": str(output_path.resolve()),
            }
        )
    manifest_path = job_root / "chapter-digest-batch.json"
    atomic_write_json(manifest_path, batch_payload(job_root, args.backend, jobs))
    update_project_stage(
        job_root / "project.json",
        "chapter_digest_jobs",
        "prepared",
        outputs={"batch_manifest": str(manifest_path)},
    )
    return manifest_path