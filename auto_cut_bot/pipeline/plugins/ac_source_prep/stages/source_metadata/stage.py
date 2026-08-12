"""SourceMetadataStage -- DEPRECATED since v5.1: Replaced by global_context. API data now only used for global context injection.

This module is retained for backwards compatibility with older pipeline configurations.
Do not add new features here; use the global_context stage instead.

Original docstring:
Stage 2 in the pipeline (after source_windows, before window_analysis).

职责: 读取 source_manifest → 调用 Platform Metadata API → 规范化/
合并角色数据 → 批量写入 books/subjects/relationships/episodes/
subtitles/shots/subject_episodes/boundaries → 生成边界 → 发布产物。

API 不可用时设置 status='unavailable', 不阻断流水线。

使用 autocut_core.platform.client.PlatformAPIClient 作为 HTTP 传输层,
每个 API 方法返回普通 dict/list, 不可用时返回空 dict/list 不抛异常。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

from autocut_core import (
    ArtifactBus,
    Artifact,
    PipelineConfig,
    Stage,
    StageContract,
    Task,
    get_logger,
    update_project_stage,
)
from autocut_core.db.client import StageDBClient
from autocut_core.errors import ArtifactNotFoundError, ConfigError
from autocut_core.platform.client import PlatformAPIClient
from autocut_core.version import STAGE_VERSIONS

logger = get_logger(__name__)

SOURCE_METADATA_STAGE_VERSION = STAGE_VERSIONS.get(
    "source_metadata", "5.0.0-alpha"
)


# ═══════════════════════════════════════════════════════════════════════════════
# SourceMetadataStage
# ═══════════════════════════════════════════════════════════════════════════════


class SourceMetadataStage(Stage):
    """Stage 2: 从 Platform API 拉取剧集元数据并写入 8 张 DB 表。

    生命周期:
      prepare(bus) → 读 source_manifest, 产出 fetch_metadata 任务
      execute(bus, tasks) → 调 API → 规范化 → 写 DB → 生成边界 → 发布产物
    """

    # ── contract ────────────────────────────────────────────────────────

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="source_metadata",
            input_artifacts=["source_manifest"],
            output_artifacts=["source_metadata"],
            description=(
                "Fetch episode metadata (characters, subtitles, shots) from "
                "Platform API and write to 8 DB tables"
            ),
            db_reads=[],
            db_writes=[
                "books",
                "subjects",
                "relationships",
                "episodes",
                "subtitles",
                "shots",
                "subject_episodes",
                "boundaries",
            ],
        )

    # ── prepare ─────────────────────────────────────────────────────────

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """从 ArtifactBus 读取 source_manifest, 规划 API 拉取任务。"""
        manifest = bus.resolve("source_windows", "source_manifest")
        if manifest is None:
            raise ArtifactNotFoundError(
                "产物 source_windows/source_manifest 未找到 — "
                "请确认 source_windows Stage 已成功执行"
            )
        data = bus.get(manifest)
        # bus.get() 可能返回 {"path": "..."} 引用而非实际数据
        if "sources" not in data and "path" in data:
            from pathlib import Path as _Path
            from autocut_core.io import load_json as _load
            actual_path = _Path(data["path"]).expanduser().resolve()
            if not actual_path.is_file():
                actual_path = (bus._root.parent / data["path"]).expanduser().resolve()
            if not actual_path.is_file():
                actual_path = (bus._root / data["path"]).expanduser().resolve()
            data = _load(actual_path)
        return [Task(type="fetch_metadata", payload={"source_manifest": data})]

    # ── execute ─────────────────────────────────────────────────────────

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """执行: 调 API → 规范化 → 写 8 张表 → 生成边界 → 发布产物。

        流程:
          1. 解析 source_manifest → 提取 episode IDs 与 book 信息
          2. 构建 PlatformAPIClient 与 StageDBClient
          3. 调用 fetch_book_metadata + fetch_episodes (不可用时降级)
          4. 合并角色数据 (跨集去重, 合并 book-level + episode-level)
          5. 批量写入 8 张表
          6. 从字幕/分镜生成边界
          7. 组装并发布 source_metadata 产物
        """
        cfg = self.config
        job_root = cfg.job_root
        if job_root is None:
            raise ConfigError("job_root 未设置")

        task = tasks[0]
        source_manifest = task.payload["source_manifest"]

        # ── 1. 提取 episode IDs 与 book 信息 ──
        sources = source_manifest.get("sources", [])
        if not sources:
            logger.warning("source_manifest 中没有 source — 跳过")
            empty_artifact = bus.put(
                "source_metadata",
                {"status": "empty", "episode_count": 0},
                stage="source_metadata",
            )
            update_project_stage(
                job_root / "project.json",
                "source_metadata",
                "completed",
                outputs={"source_metadata": empty_artifact.sha256},
            )
            return [empty_artifact]

        episode_ids = sorted({s["episode"] for s in sources})
        book_id = _resolve_book_id(cfg, sources)
        book_name = _resolve_book_name(cfg, sources)

        # ── 2. 构建客户端 ──
        api_base_url = _resolve_api_base_url(cfg)
        api_key = _resolve_api_key(cfg)
        client = PlatformAPIClient(base_url=api_base_url, api_key=api_key)
        db = StageDBClient(db_url=cfg.db_url, schema=cfg.db_schema)

        # ── 3. 调用 API (两个端点) ──
        # fetch_book_metadata → book-level: synopsis, genre, tags, characters, relationships
        # fetch_episodes → episode-level: episodes with subtitles, shots, per-episode characters
        api_start = time.monotonic()

        if not client.is_available:
            logger.warning(
                "Platform API 未配置 (base_url=%s, api_key_set=%s) — "
                "status=unavailable, 流水线继续但无 API 数据注入",
                api_base_url,
                bool(api_key),
            )
            book_meta: dict[str, Any] = {}
            episodes_raw: list[dict[str, Any]] = []
            api_status = "unavailable"
        else:
            try:
                book_meta = client.fetch_book_metadata(book_id)
                episodes_raw = client.fetch_episodes(book_id)
                elapsed = time.monotonic() - api_start
                if book_meta or episodes_raw:
                    api_status = "ok"
                    logger.info(
                        "Metadata API: book=%s, %d episodes fetched in %.1fs",
                        book_id,
                        len(episodes_raw),
                        elapsed,
                    )
                else:
                    api_status = "empty"
                    logger.warning(
                        "Metadata API returned empty data for book_id=%s",
                        book_id,
                    )
            except Exception as exc:
                logger.warning(
                    "Platform API fetch failed: %s — status=unavailable",
                    exc,
                )
                book_meta = {}
                episodes_raw = []
                api_status = "unavailable"

        # ── 4. 规范化并写入 DB ──
        stats = _write_all_tables(
            db=db,
            book_id=book_id,
            book_name=book_name,
            total_episodes=len(episode_ids),
            episode_ids=episode_ids,
            book_meta=book_meta,
            episodes_raw=episodes_raw,
            api_status=api_status,
        )

        # ── 5. 生成边界 ──
        boundary_count = _generate_boundaries(
            db=db,
            book_id=book_id,
            episodes_raw=episodes_raw,
        )

        # ── 6. 组装产物 ──
        output = {
            "book_id": book_id,
            "book_name": book_name,
            "total_episodes": len(episode_ids),
            "episode_ids": episode_ids,
            "api_base_url": _redact_url(api_base_url),
            "api_status": api_status,
            "fetched_at": _utc_now_iso(),
            "stats": stats,
            "boundaries_generated": boundary_count,
        }

        artifact = bus.put("source_metadata", output, stage="source_metadata")

        update_project_stage(
            job_root / "project.json",
            "source_metadata",
            "completed",
            outputs={"source_metadata": artifact.sha256},
        )

        return [artifact]


# ═══════════════════════════════════════════════════════════════════════════════
# DB write helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _write_all_tables(
    *,
    db: StageDBClient,
    book_id: str,
    book_name: str,
    total_episodes: int,
    episode_ids: list[int],
    book_meta: dict[str, Any],
    episodes_raw: list[dict[str, Any]],
    api_status: str,
) -> dict[str, int]:
    """将 API 响应写入 8 张表, 返回统计字典。

    写入顺序:
      1. books        — 独立, 先写 (book_meta 提供 synopsis/genre/tags)
      2. subjects     — 独立, 先写 (合并 book-level + episode-level 角色)
      3. episodes     — 独立, 先写
      4. subtitles    — 依赖 episodes
      5. shots        — 依赖 episodes
      6. relationships — 依赖 subjects (book_meta 提供)
      7. subject_episodes — 依赖 subjects + episodes
      8. boundaries   — 由 _generate_boundaries 单独处理
    """
    stats: dict[str, int] = {
        "books_upserted": 0,
        "subjects_upserted": 0,
        "relationships_upserted": 0,
        "episodes_upserted": 0,
        "subtitles_inserted": 0,
        "shots_inserted": 0,
        "subject_episodes_upserted": 0,
    }

    if api_status == "unavailable" or not db.is_available:
        return stats

    # ── Map API response keys to Python-friendly names ──
    # book_meta from batch-content-assets: camelCase keys
    # episodes_raw from batch-episodes-info: list of camelCase dicts

    # ── 1. UPSERT books ──
    book_synopsis = book_meta.get("overallSynopsis") or book_meta.get("overall_synopsis")
    book_genre = book_meta.get("genre")
    book_language = book_meta.get("language", "zh")
    book_tags = _collect_tags(book_meta)

    stats["books_upserted"] = db.upsert_book(
        book_id=book_id,
        book_name=book_name,
        total_episodes=total_episodes,
        source_type="api_script",
        overall_synopsis=book_synopsis,
        genre=book_genre,
        language=book_language,
        tags=book_tags,
    )

    # ── Collect character data from both sources ──
    all_characters: list[dict[str, Any]] = []

    # Book-level characters (CharacterAsset) — full profile
    book_chars = book_meta.get("characters", [])
    if isinstance(book_chars, list):
        for c in book_chars:
            if isinstance(c, dict) and c.get("name"):
                all_characters.append(_normalize_book_character(c))

    # Episode-level characters (CharacterInfo) and other data
    all_episodes: list[dict[str, Any]] = []
    all_subtitles: dict[int, list[dict[str, Any]]] = {}
    all_shots: dict[int, list[dict[str, Any]]] = {}
    all_subject_episodes: list[dict[str, Any]] = []

    # API returns episodes in order with chapterId (e.g. 701432748),
    # but episode_ids uses sequential 1-based IDs (1, 2, 3... 45).
    # Map by position (index), not by chapterId.
    raw_episodes_by_id: dict[int, dict[str, Any]] = {}
    for idx, ep in enumerate(episodes_raw):
        if isinstance(ep, dict):
            eid = idx + 1  # 1-based index matching source_manifest order
            raw_episodes_by_id[eid] = ep

    for ep_id in episode_ids:
        ep_data = raw_episodes_by_id.get(ep_id, {})

        # Episodes table
        all_episodes.append({
            "episode_id": ep_id,
            "summary": ep_data.get("summary"),
            "is_free": ep_data.get("isFree", ep_data.get("is_free", False)),
            "duration": ep_data.get("duration"),
            "source": "api",
        })

        # Episode-level characters
        ep_chars = ep_data.get("characters", [])
        if isinstance(ep_chars, list):
            for c in ep_chars:
                if isinstance(c, dict) and c.get("name"):
                    all_characters.append(_normalize_episode_character(c))

        # Subtitles
        subs = ep_data.get("subtitles", [])
        if isinstance(subs, list) and subs:
            all_subtitles[ep_id] = _normalize_subtitles(subs)

        # Shots
        shot_list = ep_data.get("shots", [])
        if isinstance(shot_list, list) and shot_list:
            all_shots[ep_id] = _normalize_shots(shot_list)

        # Subject-episode links (derived from episode-level characters)
        if isinstance(ep_chars, list):
            for c in ep_chars:
                if isinstance(c, dict) and c.get("name"):
                    all_subject_episodes.append({
                        "subject_name": c["name"],
                        "episode_id": ep_id,
                        "source": "api",
                    })

    # ── 2. UPSERT subjects (dedup by name) ──
    name_to_id: dict[str, int] = {}
    if all_characters:
        deduped = _dedup_characters(all_characters)
        name_to_id = db.upsert_subjects(book_id, deduped, source="api")
        stats["subjects_upserted"] = len(name_to_id)

    # ── 3. UPSERT episodes ──
    stats["episodes_upserted"] = db.upsert_episodes(book_id, all_episodes, source="api")

    # ── 4. INSERT subtitles ──
    for ep_id, segments in all_subtitles.items():
        count = db.insert_subtitles(book_id, ep_id, segments, source="api")
        stats["subtitles_inserted"] += count

    # ── 5. INSERT shots ──
    for ep_id, shot_list in all_shots.items():
        count = db.insert_shots(book_id, ep_id, shot_list)
        stats["shots_inserted"] += count

    # ── 6. UPSERT relationships (from book_meta) ──
    raw_rels = book_meta.get("relationships", [])
    if isinstance(raw_rels, list) and raw_rels:
        normalized_rels = _normalize_relationships(raw_rels)
        resolved_rels = _resolve_relationships(normalized_rels, name_to_id)
        stats["relationships_upserted"] = db.upsert_relationships(
            book_id, resolved_rels, source="api"
        )

    # ── 7. UPSERT subject_episodes ──
    if all_subject_episodes and name_to_id:
        resolved_se = _resolve_subject_episodes(all_subject_episodes, name_to_id)
        stats["subject_episodes_upserted"] = db.upsert_subject_episodes(
            book_id, resolved_se
        )

    return stats


def _generate_boundaries(
    *,
    db: StageDBClient,
    book_id: str,
    episodes_raw: list[dict[str, Any]],
) -> int:
    """从字幕和分镜数据生成边界记录。

    边界生成规则:
      - dialogue: 每个有 speaker 的字幕段 → boundary (event_type='dialogue',
        confidence='high', source_table='subtitles')
      - scene_change: 相邻 shot 之间 scene 字段发生变化 → boundary
        (event_type='scene_change', confidence='high', source_table='shots')
      - highlight: is_highlight=true 的 shot → boundary
        (event_type='highlight', confidence='high', source_table='shots')

    每条 boundary 的 boundary_id 为确定性哈希, 保证幂等。
    """
    if not db.is_available:
        return 0

    boundaries: list[dict[str, Any]] = []

    for ep_data in episodes_raw:
        if not isinstance(ep_data, dict):
            continue
        episode_id = _get_int(ep_data, "episodeId") or _get_int(ep_data, "episode_id")
        if episode_id is None:
            continue

        # ── dialogue boundaries from subtitles ──
        subs = ep_data.get("subtitles", [])
        if isinstance(subs, list):
            for sub in subs:
                if not isinstance(sub, dict):
                    continue
                speaker = (sub.get("speaker") or "").strip()
                if not speaker:
                    continue
                start_time = float(sub.get("start_time", sub.get("startTime", 0)))
                end_time = float(sub.get("end_time", sub.get("endTime", 0)))
                boundary_id = _make_boundary_id(
                    book_id, episode_id, "dialogue", start_time, end_time,
                )
                boundaries.append({
                    "boundary_id": boundary_id,
                    "episode_id": episode_id,
                    "event_type": "dialogue",
                    "start_time": start_time,
                    "end_time": end_time,
                    "description": f"Dialogue: {speaker}",
                    "subjects": [speaker],
                    "source_table": "subtitles",
                    "source_id": boundary_id,
                    "confidence": "high",
                })

        # ── scene_change + highlight boundaries from shots ──
        shot_list = ep_data.get("shots", [])
        if isinstance(shot_list, list):
            for i, shot in enumerate(shot_list):
                if not isinstance(shot, dict):
                    continue

                start_time = float(shot.get("start_time", shot.get("startTime", 0)))
                end_time = float(shot.get("end_time", shot.get("endTime", 0)))
                subjects = shot.get("subjects", [])
                scene = shot.get("scene", "")

                # highlight
                is_highlight = shot.get("is_highlight", shot.get("isHighlight", False))
                if is_highlight:
                    boundary_id = _make_boundary_id(
                        book_id, episode_id, "highlight", start_time, end_time,
                    )
                    highlight_reason = shot.get(
                        "highlight_reason",
                        shot.get("highlightReason", "Highlight shot"),
                    )
                    boundaries.append({
                        "boundary_id": boundary_id,
                        "episode_id": episode_id,
                        "event_type": "highlight",
                        "start_time": start_time,
                        "end_time": end_time,
                        "description": highlight_reason,
                        "subjects": subjects,
                        "source_table": "shots",
                        "source_id": boundary_id,
                        "confidence": "high",
                    })

                # scene_change (compare scene with next shot)
                if i + 1 < len(shot_list):
                    next_shot = shot_list[i + 1]
                    if isinstance(next_shot, dict):
                        next_scene = next_shot.get("scene", "")
                        next_start = float(
                            next_shot.get("start_time", next_shot.get("startTime", 0))
                        )
                        if scene and next_scene and scene != next_scene:
                            boundary_id = _make_boundary_id(
                                book_id, episode_id, "scene_change",
                                end_time, next_start,
                            )
                            boundaries.append({
                                "boundary_id": boundary_id,
                                "episode_id": episode_id,
                                "event_type": "scene_change",
                                "start_time": end_time,
                                "end_time": next_start,
                                "description": f"Scene change: {scene} → {next_scene}",
                                "subjects": [],
                                "source_table": "shots",
                                "source_id": boundary_id,
                                "confidence": "high",
                            })

    if boundaries:
        return db.insert_boundaries(book_id, boundaries)
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# API response normalization — camelCase → snake_case, field mapping
# ═══════════════════════════════════════════════════════════════════════════════


def _normalize_book_character(raw: dict[str, Any]) -> dict[str, Any]:
    """将 CharacterAsset (book-level) 映射为 subjects 表字段。

    API 返回的 camelCase 键:
      name, aliases, persona, personality, traits, tone,
      voiceTimbre, visualFeatures, relationship, role,
      firstEpisode, lastEpisode
    """
    return {
        "name": (raw.get("name") or "").strip(),
        "aliases": raw.get("aliases", []),
        "persona": raw.get("persona"),
        "personality": raw.get("personality", []),
        "traits": raw.get("traits"),
        "tone": raw.get("tone"),
        "voice_timbre": raw.get("voiceTimbre") or raw.get("voice_timbre"),
        "visual_features": raw.get("visualFeatures") or raw.get("visual_features"),
        "relationship": raw.get("relationship"),
        "role": raw.get("role"),
        "first_episode": _get_int(raw, "firstEpisode") or _get_int(raw, "first_episode"),
        "last_episode": _get_int(raw, "lastEpisode") or _get_int(raw, "last_episode"),
        "source": "api",
    }


def _normalize_episode_character(raw: dict[str, Any]) -> dict[str, Any]:
    """将 CharacterInfo (episode-level) 映射为 subjects 表字段。

    相比 book-level, episode-level 字段通常更少, 主要是 name + persona + role。
    """
    return {
        "name": (raw.get("name") or "").strip(),
        "aliases": raw.get("aliases", []),
        "persona": raw.get("persona"),
        "personality": raw.get("personality", []),
        "traits": raw.get("traits"),
        "tone": raw.get("tone"),
        "voice_timbre": raw.get("voiceTimbre") or raw.get("voice_timbre"),
        "visual_features": raw.get("visualFeatures") or raw.get("visual_features"),
        "relationship": raw.get("relationship"),
        "role": raw.get("role"),
        "first_episode": _get_int(raw, "firstEpisode") or _get_int(raw, "first_episode"),
        "last_episode": _get_int(raw, "lastEpisode") or _get_int(raw, "last_episode"),
        "source": "api",
    }


def _normalize_subtitles(raw_subs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将字幕列表 (camelCase) 映射为 subtitles 表字段。

    支持: start_time/startTime, end_time/endTime, speaker, text, tone, emotion。
    """
    result: list[dict[str, Any]] = []
    for s in raw_subs:
        if not isinstance(s, dict):
            continue
        text = s.get("text", "")
        if not text:
            continue
        result.append({
            "start_time": float(s.get("start_time", s.get("startTime", 0))),
            "end_time": float(s.get("end_time", s.get("endTime", 0))),
            "speaker": s.get("speaker"),
            "text": text,
            "tone": s.get("tone"),
            "emotion": s.get("emotion"),
            "group_id": s.get("groupId") or s.get("group_id"),
            "group_tone": s.get("groupTone") or s.get("group_tone"),
            "confidence": s.get("confidence"),
            "cer_estimate": s.get("cerEstimate") or s.get("cer_estimate"),
        })
    return result


def _normalize_shots(raw_shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将分镜列表 (camelCase) 映射为 shots 表字段。

    支持: start_time/startTime, end_time/endTime, scene, subjects,
    actions, is_highlight/isHighlight, highlight_score/highlightScore,
    highlight_reason/highlightReason。
    """
    result: list[dict[str, Any]] = []
    for s in raw_shots:
        if not isinstance(s, dict):
            continue
        result.append({
            "start_time": float(s.get("start_time", s.get("startTime", 0))),
            "end_time": float(s.get("end_time", s.get("endTime", 0))),
            "scene": s.get("scene"),
            "subjects": s.get("subjects", []),
            "actions": s.get("actions"),
            "is_highlight": s.get("is_highlight", s.get("isHighlight", False)),
            "highlight_score": s.get("highlight_score", s.get("highlightScore")),
            "highlight_reason": s.get(
                "highlight_reason", s.get("highlightReason")
            ),
            "related_srt_range": s.get(
                "relatedSrtRange", s.get("related_srt_range")
            ),
            "source": "api",
        })
    return result


def _normalize_relationships(
    raw_rels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将关系列表 (camelCase) 规范化为内部格式。

    API 使用角色名字而非 ID: source → source_name, target → target_name。
    """
    result: list[dict[str, Any]] = []
    for r in raw_rels:
        if not isinstance(r, dict):
            continue
        source = r.get("source") or r.get("sourceCharacterName") or r.get("source_name")
        target = r.get("target") or r.get("targetCharacterName") or r.get("target_name")
        if not source or not target:
            continue
        result.append({
            "source_name": source,
            "target_name": target,
            "description": r.get("description"),
            "data_source": r.get("source", "api"),
        })
    return result


def _collect_tags(book_meta: dict[str, Any]) -> list[str]:
    """从 book_meta 收集 tags: keywords + themeTags 合并去重。"""
    tags: list[str] = []
    seen: set[str] = set()
    for key in ("keywords", "themeTags", "tags"):
        values = book_meta.get(key, [])
        if isinstance(values, list):
            for v in values:
                if isinstance(v, str) and v not in seen:
                    tags.append(v)
                    seen.add(v)
    return tags


# ═══════════════════════════════════════════════════════════════════════════════
# Data normalization helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _dedup_characters(
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 name 去重合并角色数据。

    多个 episode 可能返回同一角色的重复信息; 同名角色合并为一条,
    取第一个非空的 persona/role/aliases, personality 列表合并去重。
    Book-level 角色优先于 episode-level 角色 (前者先处理)。
    """
    merged: dict[str, dict[str, Any]] = {}
    for c in characters:
        name = c.get("name", "").strip()
        if not name:
            continue
        if name not in merged:
            merged[name] = {
                "name": name,
                "aliases": list(c.get("aliases", [])),
                "persona": c.get("persona"),
                "personality": list(c.get("personality", [])),
                "traits": c.get("traits"),
                "tone": c.get("tone"),
                "voice_timbre": c.get("voice_timbre"),
                "visual_features": c.get("visual_features"),
                "role": c.get("role"),
                "first_episode": c.get("first_episode"),
                "last_episode": c.get("last_episode"),
                "source": c.get("source", "api"),
            }
        else:
            existing = merged[name]
            # Merge: first non-empty wins for text fields
            if not existing["persona"] and c.get("persona"):
                existing["persona"] = c["persona"]
            if not existing["role"] and c.get("role"):
                existing["role"] = c["role"]
            if not existing["traits"] and c.get("traits"):
                existing["traits"] = c["traits"]
            if not existing["tone"] and c.get("tone"):
                existing["tone"] = c["tone"]
            if not existing["voice_timbre"] and c.get("voice_timbre"):
                existing["voice_timbre"] = c["voice_timbre"]
            if not existing["visual_features"] and c.get("visual_features"):
                existing["visual_features"] = c["visual_features"]
            # Merge lists
            existing_aliases = set(existing["aliases"])
            for a in c.get("aliases", []):
                if a not in existing_aliases:
                    existing["aliases"].append(a)
                    existing_aliases.add(a)
            existing_personality = set(existing["personality"])
            for p in c.get("personality", []):
                if p not in existing_personality:
                    existing["personality"].append(p)
                    existing_personality.add(p)
            # Episode range: min(first), max(last)
            existing["first_episode"] = _min_int(
                existing["first_episode"], c.get("first_episode")
            )
            existing["last_episode"] = _max_int(
                existing["last_episode"], c.get("last_episode")
            )
    return list(merged.values())


def _resolve_relationships(
    relationships: list[dict[str, Any]],
    name_to_id: dict[str, int],
) -> list[dict[str, Any]]:
    """将 relationship 中的 source/target 名字解析为 subject_id。

    API 响应中使用角色名字而非 ID; 需要先查 subjects 表或使用内存映射
    把名字解析为整数 ID, 否则 relationships UPSERT 会失败。
    """
    resolved: list[dict[str, Any]] = []
    for rel in relationships:
        source_name = rel.get("source_name", "")
        target_name = rel.get("target_name", "")
        source_id = name_to_id.get(source_name)
        target_id = name_to_id.get(target_name)
        if source_id is not None and target_id is not None:
            resolved.append({
                "source_subject_id": source_id,
                "target_subject_id": target_id,
                "description": rel.get("description"),
                "source": rel.get("data_source", "api"),
            })
    return resolved


def _resolve_subject_episodes(
    entries: list[dict[str, Any]],
    name_to_id: dict[str, int],
) -> list[dict[str, Any]]:
    """将 subject_episodes 条目中的 subject_name 解析为 subject_id。"""
    resolved: list[dict[str, Any]] = []
    for entry in entries:
        subject_name = entry.get("subject_name", "")
        subject_id = name_to_id.get(subject_name)
        if subject_id is not None:
            resolved.append({
                "subject_id": subject_id,
                "episode_id": entry["episode_id"],
                "source": entry.get("source", "api"),
            })
    return resolved


# ═══════════════════════════════════════════════════════════════════════════════
# Config / book ID resolution
# ═══════════════════════════════════════════════════════════════════════════════


def _resolve_api_base_url(cfg: PipelineConfig) -> str | None:
    """解析 API base URL: cfg.extra > env METADATA_API_BASE_URL > None。"""
    extra = cfg.extra if hasattr(cfg, "extra") else {}
    return (
        extra.get("metadata_api_base_url", "")
        or os.environ.get("METADATA_API_BASE_URL", "")
        or None
    )


def _resolve_api_key(cfg: PipelineConfig) -> str | None:
    """解析 API key: cfg.extra > env METADATA_API_KEY > None。"""
    extra = cfg.extra if hasattr(cfg, "extra") else {}
    return (
        extra.get("metadata_api_key", "")
        or os.environ.get("METADATA_API_KEY", "")
        or None
    )


def _resolve_book_id(
    cfg: PipelineConfig,
    sources: list[dict[str, Any]],
) -> str:
    """解析 book_id: cfg.extra.book_id > env SD_BOOK_ID > source manifest 推导。

    source_manifest 中 source 的 path 格式通常为 {root}/{book_name}/...,
    取第二个路径段作为 book_id 推导。
    """
    extra = cfg.extra if hasattr(cfg, "extra") else {}
    book_id = extra.get("book_id", "") or os.environ.get("SD_BOOK_ID", "")
    if book_id:
        return book_id

    # 从第一个 source 的 path 推导
    if sources:
        path_str = sources[0].get("path", "") or sources[0].get("url", "")
        if path_str:
            parts = Path(path_str).parts
            if len(parts) >= 2:
                return parts[1]  # {root}/{book_name}/...
        # 回退: 用 episode 列表生成确定性 ID
        eps = sorted({s["episode"] for s in sources})
        if eps:
            return f"book-{eps[0]}-{eps[-1]}"

    return "unknown-book"


def _resolve_book_name(
    cfg: PipelineConfig,
    sources: list[dict[str, Any]],
) -> str:
    """解析 book_name: cfg.extra.book_name > env SD_BOOK_NAME > 推导。"""
    extra = cfg.extra if hasattr(cfg, "extra") else {}
    book_name = extra.get("book_name", "") or os.environ.get("SD_BOOK_NAME", "")
    if book_name:
        return book_name

    if sources:
        path_str = sources[0].get("path", "") or sources[0].get("url", "")
        if path_str:
            parts = Path(path_str).parts
            if len(parts) >= 2:
                return parts[1]
    return "Unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Utility helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_boundary_id(
    book_id: str,
    episode_id: int,
    event_type: str,
    start_time: float,
    end_time: float,
) -> str:
    """生成确定性 boundary_id — SHA256 前 16 位 hex。

    确定性 ID 保证重复执行幂等 (ON CONFLICT DO NOTHING)。
    """
    raw = f"{book_id}:{episode_id}:{event_type}:{start_time:.3f}:{end_time:.3f}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _get_int(data: dict[str, Any], key: str) -> int | None:
    """安全地从 dict 获取整数, 支持 str/int 类型。"""
    value = data.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _min_int(a: Any, b: Any) -> int | None:
    """返回两个值中的最小整数, None 视为无穷大。"""
    vals = [v for v in (a, b) if v is not None]
    if not vals:
        return None
    return int(min(vals))


def _max_int(a: Any, b: Any) -> int | None:
    """返回两个值中的最大整数, None 视为无穷小。"""
    vals = [v for v in (a, b) if v is not None]
    if not vals:
        return None
    return int(max(vals))


def _redact_url(url: str | None) -> str:
    """去除 URL 中的 query/fragment, 避免泄露签名参数。"""
    if not url:
        return ""
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _utc_now_iso() -> str:
    """返回当前 UTC 时间 ISO 8601 字符串。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()