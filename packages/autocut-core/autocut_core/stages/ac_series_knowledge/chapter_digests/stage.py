"""ChapterDigestsStage — 逐章语义摘要 (每 ~6 集合一)。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import (
    atomic_write_jsonl, load_json, update_project_stage,
)
from autocut_core.io import collect_digest_records
from autocut_core.semantic.prep.chapters import prepare_chapters
from autocut_core.libs.artifact_validator import fixup_fuzzy_ids_in_value
from autocut_core.stages.ports import LLMPort, get_llm_port

_CHAPTER_DIGEST_TASK = "chapter_digest"


def _replace_short_ids(obj: Any, id_map: dict[str, str]) -> Any:
    """Recursively replace short E01/E02 IDs with full event IDs in-place."""
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str) and item in id_map:
                obj[i] = id_map[item]
            else:
                _replace_short_ids(item, id_map)
    elif isinstance(obj, dict):
        for key, val in obj.items():
            if key.endswith("_ids") or key in {"event_id"}:
                if isinstance(val, list):
                    for i, eid in enumerate(val):
                        if isinstance(eid, str) and eid in id_map:
                            val[i] = id_map[eid]
                elif isinstance(val, str) and val in id_map:
                    obj[key] = id_map[val]
            elif isinstance(val, (dict, list)):
                _replace_short_ids(val, id_map)
    return obj


class ChapterDigestsStage(Stage):
    """逐章语义摘要生成 (每 ~6 集合一)。

    输入: episode_digests (EpisodeDigestsStage 产出)
    输出: chapter_digests
    """

    def __init__(self, *args: Any, llm_port: LLMPort | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._llm_port: LLMPort | None = llm_port

    @property
    def llm_port(self) -> LLMPort:
        if self._llm_port is None:
            self._llm_port = get_llm_port()
        return self._llm_port

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="chapter_digests",
            input_artifacts=["episode_digests"],
            output_artifacts=["chapter_digests"],
            description="逐章语义摘要 (合并多集剧情)",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="semantic_batch", payload={
            "episode_digests": self.resolve_artifact_path(
                bus, "episode_digests", "episode_digests"
            ),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        p = tasks[0].payload

        # Check feature flags for new v2 pipeline
        enable_two_pass = getattr(cfg, "enable_two_pass_chapter", False)
        enable_llm_segmenter = getattr(cfg, "enable_llm_chaptering", True)

        # Load episode digests and events
        from autocut_core.io import load_jsonl, write_json
        episode_digests_path = Path(p["episode_digests"])
        episodes = load_jsonl(episode_digests_path)
        events_path = root / "event-cards.jsonl"
        events = load_jsonl(events_path) if events_path.is_file() else []
        all_event_ids = {ev["id"] for ev in events if isinstance(ev, dict) and isinstance(ev.get("id"), str)}

        if enable_two_pass:
            # === New v2 pipeline: Global segmenter → Two-pass generation → Rolling context ===
            from autocut_core.semantic.prep.global_segmenter import segment_chapters
            from autocut_core.semantic.prep.chapters import _clean_episode_for_chapter, _compact_event, _build_short_id_map, _format_event_dsl
            from autocut_core.semantic.prep.two_pass_chapter import build_pass1_prompt, build_pass2_prompt, merge_chapter_results, update_rolling_context
            import logging
            logger = logging.getLogger(__name__)

            # Step 1: Global chapter segmentation (LLM with heuristic fallback)
            args = argparse.Namespace(
                job_root=root,
                backend=cfg.backend,
                episodes_per_chapter=cfg.episodes_per_chapter,
                chapter_overlap=1,
                enable_dynamic_chaptering=True,
                enable_llm_chaptering=enable_llm_segmenter,
                max_context_chars=600000,
            )
            drama_title = load_json(root / "project.json").get("title", "短剧")
            boundaries, core_threads = segment_chapters(episodes, args, root, llm_port=self.llm_port, drama_title=drama_title)

            # Initialize rolling context with core threads from global segmenter
            rolling_context = {
                "threads": [
                    {"id": t["thread_id"], "name": t["name"], "summary": t.get("description", ""), "status": "introduced"}
                    for t in core_threads
                ],
                "characters": [],
                "relationships": [],
            }

            records = []
            total_fixes = 0

            # Step 2: Process each chapter sequentially (two passes per chapter, rolling context)
            for start_idx, end_idx, chapter_id, chapter_meta in boundaries:
                group = episodes[start_idx:end_idx]
                chapter_episodes = [item["episode"] for item in group]
                chapter_episode_set = set(chapter_episodes)
                chapter_events = [
                    _compact_event(item)
                    for item in events
                    if item.get("episode") in chapter_episode_set
                ]

                # Build short ID map and DSL events
                short_to_full, full_to_short = _build_short_id_map(chapter_events)
                dsl_events = [_format_event_dsl(full_to_short[e["id"]], e) for e in chapter_events]
                cleaned_eps = [_clean_episode_for_chapter(item) for item in group]

                # Pass 1: Plot summary and story threads
                logger.info(f"Generating chapter {chapter_id} pass 1 (plot)...")
                pass1_messages = build_pass1_prompt(
                    chapter_id=chapter_id,
                    episodes=chapter_episodes,
                    chapter_meta=chapter_meta,
                    episode_summaries=cleaned_eps,
                    event_dsl=dsl_events,
                    rolling_context=rolling_context,
                )
                pass1_response = self.llm_port.call_llm(
                    prompt=pass1_messages[-1]["content"],
                    model=getattr(cfg, "backend_model", None) or "qwen-max",
                    messages=pass1_messages,
                    temperature=0.1,
                    max_tokens=4096,
                    response_format={"type": "json_object"},
                    timeout=90.0,
                )
                pass1_content = pass1_response.get("content", "") if isinstance(pass1_response, dict) else str(pass1_response)
                # Strip markdown
                pass1_content = pass1_content.strip()
                if pass1_content.startswith("```json"):
                    pass1_content = pass1_content[7:]
                if pass1_content.startswith("```"):
                    pass1_content = pass1_content[3:]
                if pass1_content.endswith("```"):
                    pass1_content = pass1_content[:-3]
                pass1_result = json.loads(pass1_content.strip())

                # Pass 2: Characters and relationships
                logger.info(f"Generating chapter {chapter_id} pass 2 (entities)...")
                pass2_messages = build_pass2_prompt(
                    chapter_id=chapter_id,
                    chapter_summary=pass1_result.get("summary", ""),
                    event_dsl=dsl_events,
                    rolling_context=rolling_context,
                )
                pass2_response = self.llm_port.call_llm(
                    prompt=pass2_messages[-1]["content"],
                    model=getattr(cfg, "backend_model", None) or "qwen-max",
                    messages=pass2_messages,
                    temperature=0.1,
                    max_tokens=4096,
                    response_format={"type": "json_object"},
                    timeout=90.0,
                )
                pass2_content = pass2_response.get("content", "") if isinstance(pass2_response, dict) else str(pass2_response)
                pass2_content = pass2_content.strip()
                if pass2_content.startswith("```json"):
                    pass2_content = pass2_content[7:]
                if pass2_content.startswith("```"):
                    pass2_content = pass2_content[3:]
                if pass2_content.endswith("```"):
                    pass2_content = pass2_content[:-3]
                pass2_result = json.loads(pass2_content.strip())

                # Merge results and map IDs
                chapter_result = merge_chapter_results(
                    chapter_id=chapter_id,
                    episodes=chapter_episodes,
                    pass1_result=pass1_result,
                    pass2_result=pass2_result,
                    short_id_map=short_to_full,
                    chapter_meta=chapter_meta,
                    all_event_ids=all_event_ids,
                )

                # Apply fuzzy ID fixup
                fixes = []
                total_fixes += fixup_fuzzy_ids_in_value(chapter_result, all_event_ids, fixes)

                # Update rolling context for next chapter
                rolling_context = update_rolling_context(rolling_context, chapter_result)
                records.append(chapter_result)

            if total_fixes > 0:
                print(f"[chapter_digests] Auto-fixed {total_fixes} ID typos via fuzzy matching")

            output = root / "chapter-digests.jsonl"
            atomic_write_jsonl(output, records)
            print(f"[chapter_digests] Two-pass generation completed: {len(records)} chapters written")

        else:
            # === Original v1 pipeline: Batch parallel generation (preserved for compatibility) ===
            args = argparse.Namespace()
            args.job_root = root
            args.backend = cfg.backend
            args.episode_digests = Path(p["episode_digests"])
            args.episodes_per_chapter = cfg.episodes_per_chapter
            args.max_context_chars = 600000
            args.enable_dynamic_chaptering = True
            args.chapter_overlap = 1
            batch_path = prepare_chapters(args)

            # 2. LLM 推理
            self.llm_port.run_batch(
                batch_path,
                backend=cfg.backend,
                workers=cfg.workers,
                requests_per_minute=cfg.requests_per_minute,
                semantic_retries=cfg.semantic_retries,
            )

            # 3. 内联组装 (不再调用 assemble_story_artifacts.py chapters)
            manifest = load_json(batch_path)
            jobs = manifest.get("jobs", [])
            records = collect_digest_records(manifest, _CHAPTER_DIGEST_TASK)
            # Build a map from chapter_id -> short_to_full_id_map from job metadata
            id_maps = {}
            for job in jobs:
                cid = job.get("id")
                id_map = job.get("short_to_full_id_map", {})
                if cid and id_map:
                    id_maps[cid] = id_map
            # Post-process each record: replace short IDs with full IDs and run fuzzy fixup
            fixed_count = 0
            for rec in records:
                cid = rec.get("chapter_id")
                # First replace short E-ids with full event IDs
                if cid in id_maps:
                    _replace_short_ids(rec, id_maps[cid])
                # Then apply fuzzy match fixup for any ID typos
                fixes = []
                fixed_count += fixup_fuzzy_ids_in_value(rec, all_event_ids, fixes)
            if fixed_count > 0:
                print(f"[chapter_digests] Auto-fixed {fixed_count} ID typos via fuzzy matching")
            output = root / "chapter-digests.jsonl"
            atomic_write_jsonl(output, records)

        ref = bus.put("chapter_digests", {"path": str(output)}, stage="chapter_digests")
        update_project_stage(root / "project.json", "chapter_digests", "completed",
                             outputs={"chapter_digests": str(output), "chapter_count": len(records)})
        return [ref]
