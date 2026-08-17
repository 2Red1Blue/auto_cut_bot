"""autocut_core.semantic.prep.episodes — Episode Digest 批处理准备。

从 prepare_story_stages.py 提取 Episode 相关函数。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any


from autocut_core.semantic.prep.utils import batch_payload, write_context


# ── constants ────────────────────────────────────────────────────────

FORCE_QWEN_DIGEST_ENV_FLAG = "SHORT_DRAMA_FORCE_QWEN_DIGEST"

EPISODE_SEMANTIC_ROLLUP_FIELDS = (
    "characters",
    "relationships",
    "story_thread_updates",
    "facts",
    "open_questions",
)


def _extract_character_mentions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从事件卡提取角色提及，做本地规则别名预合并（零 LLM）。
    
    - 剥离括号注释（如 "Aurora（回忆）" → "Aurora"）
    - 大小写/空格不敏感匹配
    - 每组取频次最高的名字作为代表，其余计入 variants
    - 返回按 event_count 降序排列
    """
    import re
    from collections import Counter

    # 1. 收集所有名字及频次
    name_freq: Counter[str] = Counter()
    for e in events:
        for name in e.get("character_names", []):
            if name:
                name_freq[name] += 1
    if not name_freq:
        return []

    # 2. 规范化函数：剥离括号注释 + 小写 + 去空格
    def _canon(name: str) -> str:
        return re.sub(r'[（(].*?[）)]', '', name).strip().lower()

    # 3. 按规范化名分组
    groups: dict[str, list[tuple[str, int]]] = {}
    for name, freq in name_freq.items():
        canon = _canon(name)
        groups.setdefault(canon, []).append((name, freq))

    # 4. 每组取频次最高的作为代表名
    mentions: list[dict[str, Any]] = []
    for canon, variants in groups.items():
        variants.sort(key=lambda x: -x[1])
        canonical_name = variants[0][0]
        other_variants = [v[0] for v in variants[1:]]
        mentions.append({
            "name": canonical_name,
            "variants": other_variants,
            "event_count": sum(v[1] for v in variants),
        })

    mentions.sort(key=lambda m: -m["event_count"])
    return mentions


# ── helpers ──────────────────────────────────────────────────────────

def _force_qwen_digest_by_env(environ: dict[str, str] | None = None) -> bool:
    """检查环境变量, 决定是否强制走 Qwen 生成 Digest。

    原位置: prepare_story_stages._force_qwen_digest_by_env (L1054, 4L)
    """
    values = os.environ if environ is None else environ
    value = values.get(FORCE_QWEN_DIGEST_ENV_FLAG, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _first_non_empty(*values: str | None) -> str | None:
    """返回第一个非空字符串, 都不非空时返回 None。

    原位置: prepare_story_stages._first_non_empty (L1060, 3L)
    """
    for candidate in values:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


# ── public API ───────────────────────────────────────────────────────

def build_local_episode_digest(
    *,
    episode: int,
    source_ids: list[str],
    window_ids: list[str],
    windows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build an ``EPISODE_DIGEST_SCHEMA``-compatible digest from a single
    analyzed window. Returns ``None`` when the source data is insufficient
    (empty summary, no events) — caller must fall back to a Qwen call.

    原位置: prepare_story_stages.build_local_episode_digest (L1067, 87L)
    """
    if len(window_ids) != 1:
        return None
    if len(windows) != 1:
        return None
    window = windows[0]
    if not isinstance(window, dict):
        return None
    boundary = window.get("boundary_context") or {}
    segments = window.get("timeline_segments") or []
    first_segment_summary = (
        segments[0].get("summary") if segments and isinstance(segments[0], dict) else None
    )
    last_segment_summary = (
        segments[-1].get("summary")
        if segments and isinstance(segments[-1], dict)
        else None
    )
    opening_state = _first_non_empty(
        boundary.get("start_state"),
        first_segment_summary,
        window.get("window_summary"),
    )
    ending_state = _first_non_empty(
        boundary.get("end_state"),
        last_segment_summary,
        window.get("window_summary"),
    )
    summary = _first_non_empty(window.get("window_summary"))
    if not opening_state or not ending_state or not summary:
        return None
    event_ids = sorted(
        {
            event["id"]
            for event in events
            if isinstance(event, dict) and isinstance(event.get("id"), str)
        }
    )
    if not event_ids:
        # EPISODE_DIGEST_SCHEMA requires NONEMPTY_EVENT_IDS — fall back
        # to Qwen when the local event catalog is empty for this episode.
        return None
    highlight_candidate_ids = sorted(
        c["id"]
        for c in candidates
        if isinstance(c, dict)
        and isinstance(c.get("id"), str)
        and c.get("type") == "highlight"
    )
    hook_candidate_ids = sorted(
        c["id"]
        for c in candidates
        if isinstance(c, dict)
        and isinstance(c.get("id"), str)
        and c.get("type") == "hook"
    )
    return {
        "schema_version": "1.0",
        "episode": episode,
        "source_ids": list(source_ids),
        "window_ids": list(window_ids),
        "opening_state": opening_state,
        "ending_state": ending_state,
        "summary": summary,
        # Single-window digests intentionally leave semantic rollup fields empty:
        # global character / thread / fact normalization is the job of
        # series_registry (which sees all chapters) and chapter_digest.
        "characters": [],
        "relationships": [],
        "event_ids": event_ids,
        "story_thread_updates": [],
        "facts": [],
        "open_questions": [],
        "highlight_candidate_ids": highlight_candidate_ids,
        "hook_candidate_ids": hook_candidate_ids,
        # Phase 1: deterministic signal fields
        "character_mentions": _extract_character_mentions(events),
        "event_summary_signals": {
            "total_events": len(events),
            "has_cause_coverage": (
                sum(1 for e in events if e.get("cause")) / max(len(events), 1)
            ),
            "has_effect_coverage": (
                sum(1 for e in events if e.get("effect")) / max(len(events), 1)
            ),
        },
        "summary_quality": "generated",
    }


def prepare_episodes(args: argparse.Namespace) -> Path:
    """Build episode-digest batch manifest.

    原位置: prepare_story_stages.prepare_episodes (L328, 96L)
    """
    import logging
    logger = logging.getLogger(__name__)
    
    from autocut_core.io import atomic_write_json, load_json, load_jsonl, update_project_stage
    from autocut_core.schema.compat import validate_task_response

    job_root = args.job_root.resolve()
    
    # DEBUG: 打印所有传入的路径
    logger.info("prepare_episodes 输入路径:")
    logger.info("  source_manifest: %s", args.source_manifest)
    logger.info("  window_manifest: %s", args.window_manifest)
    logger.info("  window_summaries: %s", args.window_summaries)
    logger.info("  event_cards: %s", args.event_cards)
    logger.info("  candidate_catalog: %s", args.candidate_catalog)
    
    # 检查文件是否存在
    for name, path in [
        ("source_manifest", args.source_manifest),
        ("window_manifest", args.window_manifest),
        ("window_summaries", args.window_summaries),
        ("event_cards", args.event_cards),
        ("candidate_catalog", args.candidate_catalog),
    ]:
        if path.exists():
            logger.info("  ✓ %s 存在 (%d bytes)", name, path.stat().st_size)
        else:
            logger.error("  ✗ %s 不存在!", name)
    
    source_manifest = load_json(args.source_manifest)
    window_manifest = load_json(args.window_manifest)
    windows = load_jsonl(args.window_summaries)
    logger.info("加载 event_cards: %s", args.event_cards)
    events = load_jsonl(args.event_cards)
    candidate_catalog = load_json(args.candidate_catalog)
    sources = source_manifest.get("sources", [])
    manifest_windows = window_manifest.get("windows", [])
    candidates = candidate_catalog.get("candidates", [])
    jobs = []
    context_dir = job_root / "intermediate" / "episode-contexts"
    output_dir = job_root / "episode-digest-results"
    episodes = sorted(
        {
            int(item["episode"])
            for item in sources
            if isinstance(item, dict) and isinstance(item.get("episode"), int)
        }
    )
    for episode in episodes:
        episode_sources = [
            item for item in sources if item.get("episode") == episode
        ]
        episode_windows = [
            item for item in windows if item.get("episode") == episode
        ]
        manifest_episode_windows = [
            item for item in manifest_windows if item.get("episode") == episode
        ]
        expected_window_ids = [item["id"] for item in manifest_episode_windows]
        actual_window_ids = [item.get("window_id") for item in episode_windows]
        if set(expected_window_ids) != set(actual_window_ids):
            raise ValueError(
                f"episode {episode}: window results do not match window manifest"
            )
        context = {
            "schema_version": "1.0",
            "episode": episode,
            "source_ids": [item["id"] for item in episode_sources],
            "window_ids": expected_window_ids,
            "sources": episode_sources,
            "windows": episode_windows,
            "events": [item for item in events if item.get("episode") == episode],
            "candidates": [
                item for item in candidates if item.get("episode") == episode
            ],
        }
        context_path = context_dir / f"episode-{episode:03d}.json"
        output_path = output_dir / f"episode-{episode:03d}.json"
        write_context(context_path, context, args.max_context_chars)
        # P1-3: single-window episodes get a locally synthesized digest. If
        # the source data lacks a summary or events, fall back to a Qwen call.
        synthetic_digest_written = False
        if (
            len(expected_window_ids) == 1
            and not _force_qwen_digest_by_env()
        ):
            local_digest = build_local_episode_digest(
                episode=episode,
                source_ids=context["source_ids"],
                window_ids=expected_window_ids,
                windows=episode_windows,
                events=context["events"],
                candidates=context["candidates"],
            )
            if local_digest is not None:
                digest_errors = validate_task_response(
                    "episode_digest", local_digest
                )
                if not digest_errors:
                    atomic_write_json(output_path, local_digest)
                    synthetic_digest_written = True
        jobs.append(
            {
                "id": f"episode-digest-{episode:03d}",
                "task": "episode_digest",
                "stage_version": "story-first-episode-digest-v1",
                "context_file": str(context_path.resolve()),
                "output": str(output_path.resolve()),
                **(
                    {"synthetic_digest": True}
                    if synthetic_digest_written
                    else {}
                ),
            }
        )
    manifest_path = job_root / "episode-digest-batch.json"
    atomic_write_json(manifest_path, batch_payload(job_root, args.backend, jobs))
    update_project_stage(
        job_root / "project.json",
        "episode_digest_jobs",
        "prepared",
        outputs={"batch_manifest": str(manifest_path)},
    )
    return manifest_path