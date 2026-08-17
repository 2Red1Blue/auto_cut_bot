"""GlobalContextStage -- 从 Platform API/剧本 提取全剧级上下文并写入 global_context 表。

Stage 2 in the VLM-first pipeline (after source_windows, before vlm_analysis).

职责: 读取配置 → 调用 Platform API (fetch_book_metadata) →
提取 synopsis/themes/relationships → 写入 global_context 表 → 发布产物。

API 不可用时尝试剧本降级, 剧本不可用时设置 source='unavailable', 不阻断流水线。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autocut_core import (
    Artifact,
    ArtifactBus,
    PipelineConfig,
    Stage,
    StageContract,
    Task,
    get_logger,
    update_project_stage,
)
from autocut_core.db.client import StageDBClient
from autocut_core.errors import ConfigError
from autocut_core.platform.client import PlatformAPIClient
from autocut_core.version import STAGE_VERSIONS

logger = get_logger(__name__)

GLOBAL_CONTEXT_STAGE_VERSION = STAGE_VERSIONS.get(
    "global_context", "5.0.0-alpha"
)


# ═══════════════════════════════════════════════════════════════════════════════
# GlobalContextStage
# ═══════════════════════════════════════════════════════════════════════════════


class GlobalContextStage(Stage):
    """Stage 2: 从 Platform API/剧本 提取全剧级上下文。

    生命周期:
      prepare(bus) → 产出 fetch_global_context 任务
      execute(bus, tasks) → 调 API → 提取 → 写 DB → 发布产物
    """

    # ── contract ────────────────────────────────────────────────────────

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="global_context",
            input_artifacts=["source_windows"],
            output_artifacts=["global_context"],
            description=(
                "Extract series-level context (synopsis, themes, relationships) "
                "from Platform API or script fallback"
            ),
            db_reads=["books"],
            db_writes=["global_context", "subjects", "books", "episodes"],
        )

    # ── prepare ─────────────────────────────────────────────────────────

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """产出 fetch_global_context 任务 — 无需上游产物。"""
        return [Task(type="fetch_global_context", payload={})]

    # ── execute ─────────────────────────────────────────────────────────

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """执行: 调 API → 提取全局上下文 → 写 DB → 发布产物。

        流程:
          1. 解析 book_id (从 config / env)
          2. 构建 PlatformAPIClient 与 StageDBClient
          3. 调用 fetch_book_metadata (不可用时降级)
          4. 提取 synopsis, themes, relationships
          5. 降级到剧本 (如果 API 不可用)
          6. UPSERT global_context 表
          7. 组装并发布 global_context 产物
        """
        cfg = self.config
        job_root = cfg.job_root
        if job_root is None:
            raise ConfigError("job_root 未设置")

        # ── 1. 解析 book_id ──
        book_id = _resolve_book_id(cfg)
        book_name = _resolve_book_name(cfg)

        # ── 2. 构建客户端 ──
        api_base_url = _resolve_api_base_url(cfg)
        api_key = _resolve_api_key(cfg)
        client = PlatformAPIClient(base_url=api_base_url, api_key=api_key)
        db = StageDBClient(db_url=cfg.db_url, schema=cfg.db_schema)

        # ── 3. 调用 API, 提取全局上下文 ──
        api_start = time.monotonic()
        context_data: dict[str, Any] = {
            "synopsis": None,
            "themes": [],
            "relationships": [],
            "source": "unavailable",
        }

        if not client.is_available:
            logger.warning(
                "Platform API 未配置 (base_url=%s, api_key_set=%s) — "
                "source=unavailable, 流水线继续但无全局上下文",
                api_base_url,
                bool(api_key),
            )
        else:
            try:
                book_meta = client.fetch_book_metadata(book_id)
                elapsed = time.monotonic() - api_start
                if book_meta:
                    context_data = _extract_context_from_api(book_meta)
                    context_data["source"] = "api"
                    # 从 API 提取 book_name 覆盖 config fallback
                    if context_data.get("book_name"):
                        book_name = context_data["book_name"]
                    # 同时获取 episode 列表 (FK 约束: shots/subtitles 依赖 episodes 表)
                    try:
                        episodes_raw = client.fetch_episodes(book_id)
                        # 从 fetch_episodes 响应提取 bookName
                        if isinstance(episodes_raw, dict):
                            api_book_name = episodes_raw.get("book_name", "")
                            if api_book_name:
                                book_name = api_book_name
                            context_data["episode_list"] = _extract_episode_list(episodes_raw.get("episodes", []))
                            # 提取 API 高光 shots
                            context_data["highlight_shots"] = _extract_highlight_shots_from_episodes(
                                episodes_raw.get("episodes", []), book_id
                            )
                        else:
                            # 兼容旧格式 (list)
                            context_data["episode_list"] = _extract_episode_list(episodes_raw)
                            context_data["highlight_shots"] = _extract_highlight_shots_from_episodes(
                                episodes_raw, book_id
                            )
                    except Exception as exc:
                        logger.warning("fetch_episodes 失败: %s", exc)
                        context_data["episode_list"] = []
                        context_data["highlight_shots"] = []

                    # ── 3a. 保存 API 高光 shots 到 shots 表（原子替换，重跑幂等）──
                    api_shots = context_data.get("highlight_shots", [])
                    if api_shots:
                        try:
                            saved = db.replace_shots(
                                book_id, 0, api_shots, sources=["api"]
                            )
                            logger.info("写入 API 高光 shots: %d/%d", saved, len(api_shots))
                        except Exception as exc:
                            logger.warning("写入 API 高光 shots 失败: %s", exc)

                    logger.info(
                        "Global context: book=%s, synopsis_len=%d, themes=%d, "
                        "relationships=%d, fetched in %.1fs",
                        book_id,
                        len(context_data.get("synopsis") or ""),
                        len(context_data.get("themes", [])),
                        len(context_data.get("relationships", [])),
                        elapsed,
                    )
                else:
                    context_data["source"] = "empty"
                    logger.warning(
                        "Platform API returned empty data for book_id=%s",
                        book_id,
                    )
            except Exception as exc:
                logger.warning(
                    "Platform API fetch failed: %s — source=unavailable",
                    exc,
                )
                context_data["source"] = "unavailable"

        # ── 4. 降级到剧本 (如果 API 不可用) ──
        if context_data["source"] in ("unavailable", "empty"):
            script_context = _extract_context_from_script(cfg, db, book_id)
            if script_context:
                context_data = script_context
                context_data["source"] = "script"
                logger.info(
                    "Global context: fallback to script for book_id=%s", book_id
                )

        # ── 5. 写入 global_context 表 ──
        db.upsert_global_context(
            book_id,
            synopsis=context_data["synopsis"],
            themes=context_data.get("themes", []),
            relationships=context_data.get("relationships", []),
            source=context_data.get("source", "unavailable"),
        )

        # Write relationships to dedicated table (name→ID 映射)
        if context_data.get("relationships"):
            try:
                subject_names = set()
                for rel in context_data["relationships"]:
                    if isinstance(rel, dict):
                        src = rel.get("source") or rel.get("source_name", "")
                        tgt = rel.get("target") or rel.get("target_name", "")
                        if src: subject_names.add(src)
                        if tgt: subject_names.add(tgt)
                subjects = db.query_subjects(book_id, names=list(subject_names))
                name_to_id = {s.get("name"): s.get("id") for s in (subjects or [])}

                mapped_rels = []
                for rel in context_data["relationships"]:
                    if not isinstance(rel, dict):
                        continue
                    src_name = rel.get("source") or rel.get("source_name", "")
                    tgt_name = rel.get("target") or rel.get("target_name", "")
                    src_id = name_to_id.get(src_name)
                    tgt_id = name_to_id.get(tgt_name)
                    if src_id and tgt_id:
                        mapped_rels.append({
                            "source_subject_id": src_id,
                            "target_subject_id": tgt_id,
                            "description": rel.get("desc") or rel.get("description", ""),
                        })
                if mapped_rels:
                    db.upsert_relationships(book_id, mapped_rels)
                    logger.info("写入 relationships: %d 条", len(mapped_rels))
            except Exception as exc:
                logger.warning("写入 relationships 失败: %s", exc)

        # ── 5a. 写入 subjects 表 (冷启动: vlm_analysis 需要角色名) ──
        # Upsert subjects for ANY source that provides characters (not just "api"),
        # using the returned name→id mapping directly to avoid a re-query.
        _source_label = context_data.get("source", "api")
        _characters = context_data.get("characters") or []
        _subject_name_to_id: dict[str, int] = {}
        if _characters:
            try:
                _subject_name_to_id = db.upsert_subjects(
                    book_id, _characters, source=_source_label,
                )
                logger.info("写入 subjects: %d 个角色 (source=%s)", len(_subject_name_to_id), _source_label)
            except Exception as exc:
                logger.warning("写入 subjects 失败: %s", exc)
                _subject_name_to_id = {}

        # Write subject-episode links using the name→id mapping from upsert_subjects
        # (avoids the "query_subjects returned nothing" 0-row problem when subjects
        # were just written in this same transaction).
        _episode_list = context_data.get("episode_list") or []
        if _episode_list and _characters and _subject_name_to_id:
            try:
                subject_entries = []
                for char in _characters:
                    char_name = char.get("name", "")
                    subj_id = _subject_name_to_id.get(char_name)
                    if not subj_id:
                        continue
                    for ep in _episode_list:
                        ep_id = ep.get("episode_id")
                        if ep_id:
                            subject_entries.append({
                                "subject_id": subj_id,
                                "episode_id": int(ep_id) if ep_id else None,
                                "source": _source_label,
                            })
                if subject_entries:
                    db.upsert_subject_episodes(book_id, subject_entries)
                    logger.info("写入 subject_episodes: %d 条", len(subject_entries))
                else:
                    logger.debug(
                        "subject_episodes: 0 entries (name_to_id empty for %d characters)",
                        len(_characters),
                    )
            except Exception as exc:
                logger.warning("写入 subject_episodes 失败: %s", exc)

        # ── 5b. 写入 books 表 (冷启动: 下游需要 book_name/genre) ──
        if context_data.get("source") == "api":
            try:
                db.upsert_book(
                    book_id=book_id,
                    book_name=book_name,
                    total_episodes=context_data.get("total_episodes"),
                    overall_synopsis=context_data.get("synopsis"),
                    genre=context_data.get("genre"),
                    source_type=context_data.get("source", "vlm_only"),
                    language=getattr(cfg, "asr_language", "zh") or "zh",
                    tags=context_data.get("themes", []),
                )
                logger.info("写入 books: book_name=%s", book_name)
            except Exception as exc:
                logger.warning("写入 books 失败: %s", exc)

        # ── 5c. 写入 episodes 表 (FK 约束: shots/subtitles 依赖此表) ──
        if context_data.get("episode_list"):
            try:
                db.upsert_episodes(book_id, context_data["episode_list"], source=context_data.get("source", "api"))
                logger.info("写入 episodes: %d 集", len(context_data["episode_list"]))
            except Exception as exc:
                logger.warning("写入 episodes 失败: %s", exc)

        # ── 6. 组装产物 ──
        output = {
            "book_id": book_id,
            "book_name": book_name,
            "synopsis": context_data["synopsis"],
            "themes": context_data["themes"],
            "relationships": context_data["relationships"],
            "source": context_data["source"],
            "fetched_at": _utc_now_iso(),
        }
        # Agent-native: API 不可用时附上剧本原文, Agent 自己解析
        if context_data.get("script_raw"):
            output["script_raw"] = context_data["script_raw"]

        artifact = bus.put("global_context", output, stage="global_context")

        update_project_stage(
            job_root / "project.json",
            "global_context",
            "completed",
            outputs={"global_context": artifact.sha256},
        )

        return [artifact]


# ═══════════════════════════════════════════════════════════════════════════════
# API response extraction
# ═══════════════════════════════════════════════════════════════════════════════


def _extract_context_from_api(book_meta: dict[str, Any]) -> dict[str, Any]:
    """从 API 响应中提取 synopsis, themes, relationships, characters。

    API 返回的 camelCase 键:
      overallSynopsis → synopsis
      keywords + themeTags → themes (合并去重)
      relationships → relationships (source, target, desc)
      characters → characters (name, role, aliases)
    """
    synopsis = book_meta.get("overallSynopsis") or book_meta.get("overall_synopsis", "")

    # 提取 bookName (从 batch-episodes-info 响应)
    book_name = book_meta.get("bookName") or book_meta.get("book_name", "")

    # 收集 themes: keywords + themeTags 合并去重

    # 收集 themes: keywords + themeTags 合并去重
    themes: list[str] = []
    seen: set[str] = set()
    for key in ("keywords", "themeTags", "tags"):
        values = book_meta.get(key, [])
        if isinstance(values, list):
            for v in values:
                if isinstance(v, str) and v not in seen:
                    themes.append(v)
                    seen.add(v)

    # 规范化 relationships
    raw_rels = book_meta.get("relationships", [])
    relationships: list[dict[str, Any]] = []
    if isinstance(raw_rels, list):
        for r in raw_rels:
            if not isinstance(r, dict):
                continue
            source = (
                r.get("source")
                or r.get("sourceCharacterName")
                or r.get("source_name")
            )
            target = (
                r.get("target")
                or r.get("targetCharacterName")
                or r.get("target_name")
            )
            if not source or not target:
                continue
            relationships.append({
                "source": source,
                "target": target,
                "desc": r.get("desc") or r.get("description"),
            })

    # 提取角色 (供 subjects 表冷启动)
    chars = book_meta.get("characters", [])
    characters: list[dict[str, Any]] = []
    if isinstance(chars, list):
        for c in chars:
            if not isinstance(c, dict):
                continue
            name = c.get("name", c.get("characterName", ""))
            if not name:
                continue
            characters.append({
                "name": name,
                "role": c.get("role", c.get("characterRole", "")),
                "aliases": c.get("aliases", []),
                "persona": c.get("persona", c.get("personality", "")),
            })

    return {
        "synopsis": synopsis,
        "themes": themes,
        "relationships": relationships,
        "characters": characters,
        "book_name": book_name,
        "genre": book_meta.get("keywords", ""),
        "total_episodes": book_meta.get("totalEpisodes"),
    }


def _extract_episode_list(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 API episodes 响应中提取精简列表 (供 episodes 表写入)。"""
    if not isinstance(episodes, list):
        return []
    result = []
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        result.append({
            "episode_id": ep.get("episodeId", ep.get("episode_id")),
            "chapter_id": ep.get("chapterId", ep.get("chapter_id")),
            "title": ep.get("title", ""),
            "summary": ep.get("summary", ""),
            "is_free": ep.get("isFree", ep.get("is_free", True)),
            "duration": _parse_time_to_seconds(ep.get("duration")),
        })
    return result


def _extract_highlight_shots_from_episodes(
    episodes: list[dict[str, Any]],
    book_id: str,
) -> list[dict[str, Any]]:
    """从 fetch_episodes 响应中提取 API 高光 shots。

    API highlights 在 episodes[].shots[] 中 (is_highlight=True),
    不在 fetch_book_metadata 响应中。

    每个 episode 的 shots 包含:
      start_time, end_time, scene, subjects, is_highlight,
      highlight_score, highlight_reason, related_srt_range

    Returns:
        List of shot dicts ready for db.insert_shots().
    """
    if not isinstance(episodes, list):
        return []

    all_shots: list[dict[str, Any]] = []
    for ep_idx, ep in enumerate(episodes):
        if not isinstance(ep, dict):
            continue
        # API response doesn't include episodeId — use array index + 1
        episode_id = ep.get("episodeId") or ep.get("episode_id") or (ep_idx + 1)
        shots = ep.get("shots", [])
        if not isinstance(shots, list):
            continue
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            # 只提取高光标记的 shot
            if not shot.get("is_highlight"):
                continue

            raw_start = shot.get("start_time") or shot.get("start")
            raw_end = shot.get("end_time") or shot.get("end")

            all_shots.append({
                "book_id": book_id,
                "episode_id": episode_id,
                "start_time": _parse_time_to_seconds(raw_start),
                "end_time": _parse_time_to_seconds(raw_end),
                "scene": shot.get("scene", ""),
                "subjects": shot.get("subjects", []),
                "is_highlight": True,
                "highlight_score": shot.get("highlight_score"),
                "highlight_reason": shot.get("highlight_reason", ""),
                "related_srt_range": shot.get("related_srt_range", ""),
                "source": "api",
            })

    return all_shots


def _parse_time_to_seconds(value: Any) -> float:
    """Convert API time format to seconds float.

    Handles:
      - "HH:MM:SS,mmm" (API returns this, comma-separated milliseconds)
      - "HH:MM:SS.mmm" (variant, dot-separated milliseconds)
      - Already a float/int (pass through)
      - None/empty → 0.0
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return 0.0
    # Normalize comma to dot for "00:00:09,000" → "00:00:09.000"
    cleaned = value.replace(",", ".")
    parts = cleaned.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return float(h) * 3600 + float(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return float(m) * 60 + float(s)
    return float(cleaned) if cleaned else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Script fallback
# ═══════════════════════════════════════════════════════════════════════════════


def _extract_context_from_script(
    cfg: PipelineConfig,
    db: StageDBClient,
    book_id: str,
) -> dict[str, Any] | None:
    """从剧本数据中提取全局上下文 (降级方案)。

    优先级:
      1. books.script_parsed JSONB (已解析的剧本数据)
      2. 直接读取剧本文件, 做轻量提取 (不用 LLM, VLM 自己做详细分析)

    VLM-first: 只需要提取 synopsis、角色名、主题关键词,
    不需要完整的场景级解析 (VLM 从视频直接看画面)。
    """
    if not db.is_available:
        return None

    # ── 优先: books.script_parsed ──
    book = db.query_book(book_id)
    if book:
        script_parsed = book.get("script_parsed")
        if script_parsed:
            if isinstance(script_parsed, str):
                try:
                    script_parsed = json.loads(script_parsed)
                except (json.JSONDecodeError, TypeError):
                    script_parsed = None
            if script_parsed and isinstance(script_parsed, dict):
                synopsis = (
                    script_parsed.get("synopsis")
                    or script_parsed.get("overallSynopsis")
                )
                themes = script_parsed.get("themes") or script_parsed.get("keywords", [])
                if isinstance(themes, str):
                    themes = [themes]
                characters = script_parsed.get("characters", [])
                relationships = _extract_relationships_from_characters(characters)
                if synopsis or themes or relationships:
                    return {
                        "synopsis": synopsis,
                        "themes": themes if isinstance(themes, list) else [],
                        "relationships": relationships,
                    }

    # ── 降级: 读剧本文件, 返回原始文本让 Agent 自己解析 ──
    # Agent-native: Agent 用自己的 LLM 提取上下文, 不调外部 LLM
    script_text = _read_script_file(cfg)
    if script_text:
        return {
            "synopsis": None,
            "themes": [],
            "relationships": [],
            "script_raw": script_text,  # Agent 自己解析
        }
    return None

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Config resolution
# ═══════════════════════════════════════════════════════════════════════════════


def _resolve_config_value(
    cfg: PipelineConfig,
    extra_key: str,
    env_key: str,
    default: Any,
) -> Any:
    """Resolve a config value: cfg.extra.<extra_key> > env <env_key> > default."""
    extra = cfg.extra if hasattr(cfg, "extra") else {}
    return extra.get(extra_key, "") or os.environ.get(env_key, "") or default


def _resolve_api_base_url(cfg: PipelineConfig) -> str | None:
    """解析 API base URL: cfg.extra > env METADATA_API_BASE_URL > None。"""
    return _resolve_config_value(
        cfg, "metadata_api_base_url", "METADATA_API_BASE_URL", None
    )


def _resolve_api_key(cfg: PipelineConfig) -> str | None:
    """解析 API key: cfg.extra > env METADATA_API_KEY > None。"""
    return _resolve_config_value(
        cfg, "metadata_api_key", "METADATA_API_KEY", None
    )


def _resolve_book_id(cfg: PipelineConfig) -> str:
    """解析 book_id: cfg.book_id > cfg.extra.book_id > env AC_BOOK_ID > unknown-book。"""
    # book_id 是 PipelineConfig 的顶层字段
    book_id = getattr(cfg, "book_id", None)
    if book_id:
        return str(book_id)
    extra = cfg.extra if hasattr(cfg, "extra") else {}
    book_id = extra.get("book_id", "") or os.environ.get("AC_BOOK_ID", "")
    if book_id:
        return str(book_id)

    # 尝试从 source_manifest 推导 (source_windows 产物)
    job_root = cfg.job_root
    if job_root:
        from autocut_core.io import load_json

        manifest_path = (
            job_root / "artifacts" / "source_windows" / "source_manifest.json"
        )
        if manifest_path.is_file():
            manifest = load_json(manifest_path)
            sources = manifest.get("sources", [])
            if sources:
                path_str = sources[0].get("path", "") or sources[0].get("url", "")
                if path_str:
                    parts = Path(path_str).parts
                    if len(parts) >= 2:
                        return parts[1]

    return "unknown-book"


def _resolve_book_name(cfg: PipelineConfig) -> str:
    """解析 book_name: cfg.extra.book_name > env AC_BOOK_NAME > Unknown。"""
    return _resolve_config_value(cfg, "book_name", "AC_BOOK_NAME", "Unknown")


def _utc_now_iso() -> str:
    """返回当前 UTC 时间 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# 剧本文件读取 (VLM-first: 只读文件, LLM 解析委托给 source_script.llm_parse)
# ═══════════════════════════════════════════════════════════════════════════════


def _read_script_file(cfg: PipelineConfig) -> str | None:
    """读取剧本文件内容 (纯文本/Markdown/DOCX)。

    搜索: cfg.extra.script_path > job_root/script*.txt > job_root/script*.md
    """
    job_root = cfg.job_root
    if not job_root:
        return None

    root = Path(job_root)
    explicit = cfg.extra.get("script_path") if hasattr(cfg, "extra") else None
    if explicit:
        path = root / explicit if not Path(explicit).is_absolute() else Path(explicit)
        if path.is_file():
            return _read_text_file(path)

    patterns = ["script*.txt", "script*.md", "剧本*.txt", "剧本*.md", "*.script.txt"]
    for pattern in patterns:
        matches = sorted(root.glob(pattern))
        if matches:
            return _read_text_file(matches[0])
    for pattern in patterns:
        matches = sorted(root.glob(f"*/{pattern}"))
        if matches:
            return _read_text_file(matches[0])

    return None


def _read_text_file(path: Path) -> str | None:
    """读取文本文件 (支持 .txt/.md/.docx)。"""
    try:
        if path.suffix == ".docx":
            try:
                from docx import Document
                doc = Document(str(path))
                return "\n".join(p.text for p in doc.paragraphs)
            except ImportError:
                return None
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _extract_relationships_from_characters(
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从角色列表提取关系网 (基于角色关联字段)。"""
    relationships: list[dict[str, Any]] = []
    if not isinstance(characters, list):
        return relationships
    for c in characters:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        rel = c.get("relationship")
        related = c.get("related_to") or c.get("relatedTo")
        if name and rel and related:
            relationships.append({
                "source": name,
                "target": related,
                "desc": rel,
            })
    return relationships
