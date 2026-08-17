"""SourceScriptSaveTool — 接收 Agent 解析的结构化数据，落库 + 发布产物。

Agent 在自己的上下文中解析完剧本后，调用此 tool 持久化结果。
Stage 做: 验证 → 字幕对齐 → DB 写入 → 产物发布 → 缓存保存。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.runtime.contracts.merge_operator import merge, merge_summary


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Root directory for the pipeline job (absolute path).",
        },
        "episodes": {
            "type": "array",
            "description": "The complete list of parsed episodes from the agent.",
            "items": {"type": "object"},
        },
        "parse_meta": {
            "type": "object",
            "description": "Metadata about the parse: rounds, strategy, total_episodes, etc.",
        },
    },
    "required": ["job_root", "episodes"],
})
class SourceScriptSaveTool(Tool):
    """Save agent-parsed script data to DB and publish artifacts.

    Called after the agent has parsed the script in its own context.
    This tool does NOT call any LLM — it only validates, aligns,
    and persists the data the agent already produced.
    """

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "source_script_save"

    @property
    def description(self) -> str:
        return (
            "Save parsed script episodes to DB and publish artifacts. "
            "Call this AFTER you have finished parsing the script in your "
            "context. Does subtitle alignment, DB writes (scenes, subjects, "
            "shots, subtitles), and artifact publishing."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Validate, align, and persist agent-parsed episodes."""
        from autocut_core import PipelineConfig
        from autocut_core.io import atomic_write_json, sha256_file
        from autocut_core.db.client import StageDBClient

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        episodes = kwargs.get("episodes", [])
        parse_meta = kwargs.get("parse_meta", {})

        if not episodes:
            return ToolResult.error(
                "No episodes provided. Parse the script first, then call "
                "source_script_save with the episodes list."
            )

        cfg = PipelineConfig(job_root=job_root)

        # ── 1. 验证 ──────────────────────────────────────────────────
        validation_errors = _validate_episodes(episodes, parse_meta)
        if validation_errors:
            return ToolResult.error(
                "Validation failed:\n- " + "\n- ".join(validation_errors)
            )

        # ── 2. 提取 book_id ──────────────────────────────────────────
        book_id = _extract_book_id(job_root, cfg)

        # ── 3. 字幕时间对齐 ──────────────────────────────────────────
        db = StageDBClient(db_url=cfg.db_url, schema=cfg.db_schema)
        episodes = _align_with_subtitles(episodes, book_id, db)

        # ── 3.5. 指纹去重 ──────────────────────────────────────────
        dedup_count = _deduplicate_scenes(episodes)

        # ── 4. 写 DB ──────────────────────────────────────────────────
        scene_count = _write_scenes(episodes, book_id, db)
        _write_derived(episodes, book_id, db)

        # ── 5. 发布产物 ──────────────────────────────────────────────
        script_sha = _get_script_sha(job_root)
        output = {
            "schema_version": "1.0",
            "status": "ok",
            "book_id": book_id,
            "source_file_sha": script_sha,
            "episodes_detected": len(episodes),
            "total_scenes": scene_count,
            "alignment_report": _build_alignment_report(episodes),
            "parse_metadata": {
                **parse_meta,
                "strategy": parse_meta.get("strategy", "agent-native"),
                "status": "success",
                "dedup_count": dedup_count,
                "total_episodes": len(episodes),
                "total_scenes": scene_count,
                **_merge_stats(parse_meta),
            },
            "episodes": episodes,
        }

        output_path = job_root / "source_script.json"
        atomic_write_json(output_path, output)

        # ── 6. 缓存 (file-based, agent-native) ──────────────────────────────
        cache_dir = job_root / ".cache" / "source_script"
        cache_dir.mkdir(parents=True, exist_ok=True)
        if script_sha:
            cache_file = cache_dir / f"{script_sha[:16]}.json"
            cache_file.write_text(json.dumps(output, ensure_ascii=False))

        # ── 6.5. 多源数据合并（来源追溯） ─────────────────────────────
        if script_sha:
            api_cache_file = cache_dir / f"{script_sha[:16]}_api.json"
            if api_cache_file.exists():
                api_data = json.loads(api_cache_file.read_text())
                merge_result = merge(output, api_data, "script", "api", "source_script")
                merged_file = cache_dir / f"{script_sha[:16]}_merged.json"
                merged_file.write_text(json.dumps(merge_summary(merge_result), ensure_ascii=False))

        # ── 7. 更新 project.json ─────────────────────────────────────
        _update_project(job_root, output_path)

        return ToolResult(
            "source_script_save completed.\n\n"
            f"Book: {book_id}\n"
            f"Episodes: {len(episodes)}\n"
            f"Scenes: {scene_count}\n"
            f"Cache key: {script_sha[:16] if script_sha else 'N/A'}\n"
            f"Output: {output_path}"
        )


# ── 验证 ──────────────────────────────────────────────────────────────────────


def _validate_episodes(episodes: list[dict], meta: dict) -> list[str]:
    """Validate parsed episodes before saving."""
    errors: list[str] = []

    if not episodes:
        return ["No episodes"]

    # Episode count
    expected = meta.get("total_episodes") or meta.get("expected_count")
    if expected and len(episodes) != expected:
        errors.append(f"Expected {expected} episodes, got {len(episodes)}")

    # Episode number continuity
    ep_nums = sorted([ep["episode_number"] for ep in episodes])
    if ep_nums[0] != 1:
        errors.append(f"First episode is {ep_nums[0]}, expected 1")

    expected_seq = list(range(ep_nums[0], ep_nums[-1] + 1))
    missing = set(expected_seq) - set(ep_nums)
    if missing:
        errors.append(f"Missing episodes: {sorted(missing)}")

    # Scene count distribution
    scene_counts = [len(ep.get("scenes", [])) for ep in episodes]
    if len(scene_counts) > 1 and sum(scene_counts) > 0:
        mean = sum(scene_counts) / len(scene_counts)
        outliers = [
            i for i, c in enumerate(scene_counts)
            if c < 0.3 * mean or c > 3.0 * mean
        ]
        if outliers:
            errors.append(
                f"Episode(s) {outliers} have outlier scene counts: "
                f"{[scene_counts[i] for i in outliers]} (mean={mean:.1f})"
            )

    return errors


# ── 指纹去重 ────────────────────────────────────────────────────────────────────


def _make_fingerprint(episode_number: int, scene: dict) -> str:
    """Generate a content fingerprint for deduplication."""
    raw = f"{episode_number}:{scene.get('scene_id', '')}:{scene.get('location', '')}:"
    dialogues = scene.get("dialogues", [])
    if dialogues and isinstance(dialogues, list):
        first_text = dialogues[0].get("text", "")[:50] if isinstance(dialogues[0], dict) else ""
        raw += first_text
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _deduplicate_scenes(episodes: list[dict]) -> int:
    """Remove duplicate scenes within each episode using fingerprint matching.

    Duplicates are identified by (episode_number, scene_id, location, first
    dialogue text).  Only the first occurrence is kept.
    """
    removed = 0
    for ep in episodes:
        ep_num = ep.get("episode_number", 0)
        scenes = ep.get("scenes", [])
        if not scenes:
            continue
        seen: set[str] = set()
        deduped: list[dict] = []
        for scene in scenes:
            fp = _make_fingerprint(ep_num, scene)
            if fp in seen:
                removed += 1
                continue
            seen.add(fp)
            deduped.append(scene)
        ep["scenes"] = deduped
    return removed


# ── 字幕对齐 ──────────────────────────────────────────────────────────────────


def _align_with_subtitles(
    episodes: list[dict], book_id: str, db: Any
) -> list[dict]:
    """Align scenes with subtitles from DB."""
    if not db.is_available:
        return episodes

    for ep in episodes:
        episode_id = ep["episode_number"]
        subtitles = db.query_subtitles(book_id, episode_id)
        if not subtitles:
            _fallback_time(ep.get("scenes", []))
            continue

        for scene in ep.get("scenes", []):
            dialogues = scene.get("dialogues", [])
            if not dialogues:
                scene["alignment_confidence"] = "none"
                continue

            matched_times = []
            for sub in subtitles:
                sub_text = sub.get("text", "")
                best_score = 0.0
                for d in dialogues:
                    text = d.get("text", "") if isinstance(d, dict) else ""
                    score = _text_similarity(sub_text, text)
                    if score > best_score:
                        best_score = score
                if best_score >= 0.4:
                    matched_times.append((sub["start_time"], sub["end_time"]))

            if matched_times:
                scene["start_time"] = matched_times[0][0]
                scene["end_time"] = matched_times[-1][1]
                scene["alignment_confidence"] = "fuzzy"
                scene["alignment_source"] = "subtitle_matched"
            else:
                scene["alignment_confidence"] = "none"
                scene["alignment_source"] = "none"

        # Check alignment rate
        aligned = sum(
            1 for s in ep.get("scenes", [])
            if s.get("alignment_confidence") not in ("none", None)
        )
        total = len(ep.get("scenes", []))
        if total > 0 and aligned / total < 0.5:
            _fallback_time(ep.get("scenes", []), subtitles)

    return episodes


def _fallback_time(scenes: list[dict], subtitles: list[dict] | None = None) -> None:
    """Fallback: evenly distribute time."""
    if subtitles and len(subtitles) >= 2:
        total_start = subtitles[0]["start_time"]
        total_end = subtitles[-1]["end_time"]
    else:
        total_start = 0.0
        total_end = len(scenes) * 180.0

    duration = total_end - total_start
    scene_duration = duration / len(scenes) if scenes else 0

    for i, scene in enumerate(scenes):
        scene["start_time"] = total_start + i * scene_duration
        scene["end_time"] = total_start + (i + 1) * scene_duration
        scene["alignment_confidence"] = "inferred"
        scene["alignment_source"] = "time_estimated"


def _text_similarity(a: str, b: str) -> float:
    """Normalized text similarity."""
    from difflib import SequenceMatcher

    def _norm(t: str) -> str:
        t = t.lower().strip()
        t = re.sub(r"[^\w一-鿿]", "", t)
        t = re.sub(r"\s+", "", t)
        return t

    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# ── DB 写入 ───────────────────────────────────────────────────────────────────


def _write_scenes(episodes: list[dict], book_id: str, db: Any) -> int:
    """UPSERT scenes to DB."""
    if not db.is_available:
        return 0

    scenes_to_upsert = []
    for ep in episodes:
        ep_id = ep["episode_number"]
        for scene in ep.get("scenes", []):
            scenes_to_upsert.append({
                "scene_id": scene.get("scene_id", f"S{ep_id}E{scene.get('scene_order', 0)}"),
                "book_id": book_id,
                "episode_id": ep_id,
                "scene_order": scene.get("scene_order"),
                "heading": scene.get("heading"),
                "location": scene.get("location"),
                "time_of_day": scene.get("time_of_day"),
                "is_flashback": scene.get("is_flashback", False),
                "characters_present": scene.get("characters_present", []),
                "dialogues": scene.get("dialogues", []),
                "raw_description": scene.get("raw_description"),
                "meta_tags": scene.get("meta_tags", {}),
                "start_time": scene.get("start_time"),
                "end_time": scene.get("end_time"),
                "alignment_confidence": scene.get("alignment_confidence"),
                "alignment_source": scene.get("alignment_source"),
                "source": "script",
            })

    if scenes_to_upsert:
        return db.upsert_scenes(book_id, scenes_to_upsert)
    return 0


def _write_derived(episodes: list[dict], book_id: str, db: Any) -> None:
    """Write derived tables: subjects, subject_episodes, shots, subtitles."""
    if not db.is_available:
        return

    # Extract all unique characters
    char_map: dict[str, dict] = {}
    for ep in episodes:
        ep_num = ep.get("episode_number", 0)
        for scene in ep.get("scenes", []):
            for name in scene.get("characters_present", []):
                if not name or not isinstance(name, str):
                    continue
                if name not in char_map:
                    char_map[name] = {
                        "name": name, "source": "script",
                        "first_episode": ep_num, "last_episode": ep_num,
                    }
                else:
                    e = char_map[name]
                    e["first_episode"] = min(e["first_episode"], ep_num)
                    e["last_episode"] = max(e["last_episode"], ep_num)

    if char_map:
        db.upsert_subjects(book_id, list(char_map.values()))

    # Subject-episode associations
    for ep in episodes:
        ep_num = ep.get("episode_number", 0)
        for scene in ep.get("scenes", []):
            for name in scene.get("characters_present", []):
                if not name or not isinstance(name, str):
                    continue
                sid = db.resolve_subject_id(book_id, name)
                if sid:
                    db.upsert_subject_episodes(book_id, [{
                        "subject_id": sid,
                        "episode_id": ep_num,
                        "appears_in_episode": True,
                        "source": "script",
                    }])

    # Shots (coarse scene boundaries)
    for ep in episodes:
        ep_num = ep.get("episode_number", 0)
        shots = []
        for scene in ep.get("scenes", []):
            shots.append({
                "start_time": scene.get("start_time", 0),
                "end_time": scene.get("end_time", 0),
                "scene": scene.get("location", ""),
                "subjects": scene.get("characters_present", []),
                "source": "script",
            })
        if shots:
            db.insert_shots(book_id, ep_num, shots)

    # Script dialogues → subtitles
    for ep in episodes:
        ep_num = ep.get("episode_number", 0)
        subs = []
        for scene in ep.get("scenes", []):
            for d in scene.get("dialogues", []):
                if not isinstance(d, dict):
                    continue
                subs.append({
                    "start_time": d.get("approx_start", 0),
                    "end_time": d.get("approx_end", 0),
                    "text": d.get("text", ""),
                    "speaker": d.get("character", ""),
                    "source": "script",
                })
        if subs:
            db.insert_subtitles(book_id, ep_num, subs, source="script")


# ── 工具 ──────────────────────────────────────────────────────────────────────


def _extract_book_id(job_root: Path, cfg: Any) -> str:
    """Extract book_id from source_metadata or config."""
    metadata_path = job_root / "source_metadata.json"
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        book_id = metadata.get("book_id")
        if book_id:
            return str(book_id)
    book_id = cfg.extra.get("book_id") if hasattr(cfg, "extra") else None
    if not book_id:
        raise ValueError("book_id not found in source_metadata or config")
    return str(book_id)


def _get_script_sha(job_root: Path) -> str:
    """Get SHA of the script file."""
    for pattern in ["*.txt", "*.docx"]:
        for f in sorted(job_root.glob(pattern)):
            if f.suffix == ".docx":
                from docx import Document
                doc = Document(str(f))
                text = "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
            else:
                text = f.read_text(encoding="utf-8-sig")
            return hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ""


def _build_alignment_report(episodes: list[dict]) -> dict[str, int]:
    """Build alignment statistics."""
    levels = {"exact": 0, "fuzzy": 0, "inferred": 0, "none": 0}
    for ep in episodes:
        for scene in ep.get("scenes", []):
            level = scene.get("alignment_confidence", "none")
            if level in levels:
                levels[level] += 1
    total = sum(levels.values())
    return {
        **levels,
        "total": total,
        "alignment_rate": (levels["exact"] + levels["fuzzy"]) / total if total else 0,
    }


def _merge_stats(parse_meta: dict) -> dict:
    """Extract merge-related statistics for the output metadata."""
    stats: dict[str, Any] = {}
    strategy = parse_meta.get("strategy", "")
    if strategy == "mapreduce":
        chunk_count = parse_meta.get("chunk_count")
        if chunk_count is not None:
            stats["chunk_count"] = chunk_count
    return stats


def _update_project(job_root: Path, output_path: Path) -> None:
    """Update project.json with stage status."""
    from autocut_core.io import sha256_file, update_project_stage

    project_path = job_root / "project.json"
    update_project_stage(
        project_path,
        "source_script",
        "completed",
        outputs={"source_script": sha256_file(output_path)},
    )