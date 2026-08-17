"""RegistryStage — Series Registry 准入与修复链。

执行链 (严格顺序依赖, 在语义批次内部依次应用):
    admission → alias_repair → identity_repair →
    reference_repair → relationship_repair → recovery

准入判定新实体能否进入剧集知识库; 修复链逐步消除别名冲突、
身份歧义、引用错误与关系错误; recovery 回收隔离区实体。

输入: chapter_digests (+ episode_digests, event_cards)
输出: series_registry

Post-processing: 对比 VLM highlight candidates 与 API 高光标记，
记录 missed highlights 到 highlight_skill_evolution 表（非阻塞）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import update_project_stage
from autocut_core.stages.ports import LLMPort, get_llm_port
from autocut_core.semantic.prep.registry_prep import prepare_registry
from autocut_core.logging import get_logger

logger = get_logger(__name__)


class RegistryStage(Stage):
    """Series Registry 准入 + 五段修复链。

    输入: chapter_digests (上游 ChapterDigestsStage 产出)
    输出: series_registry (series-registry.json,
          附带 admission / quarantine 产物路径)
    """

    def __init__(self, *args, llm_port: LLMPort | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._llm_port = llm_port

    @property
    def llm_port(self) -> LLMPort:
        if self._llm_port is None:
            self._llm_port = get_llm_port()
        return self._llm_port

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="series_registry",
            input_artifacts=["chapter_digests", "episode_digests", "event_cards"],
            output_artifacts=["series_registry"],
            description="Series Registry 准入与修复链 (admission → 四段 repair → recovery)",
            db_reads=["subject_episodes"],
            db_writes=["highlight_skill_evolution"],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """解析三个上游产物路径 — 章节摘要为主输入,
        集摘要与事件卡为准入/修复链的 CLI 必需参数。"""
        return [Task(type="semantic_batch", payload={
            "episode_digests": self.resolve_artifact_path(
                bus, "episode_digests", "episode_digests"
            ),
            "chapter_digests": self.resolve_artifact_path(
                bus, "chapter_digests", "chapter_digests"
            ),
            "event_cards": self.resolve_artifact_path(bus, "event_cards", "event_cards"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """两步执行: 生成 registry 批次 → 批次内按序应用准入与修复链。"""
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        p = tasks[0].payload

        # 1. 生成 registry 语义批次 (直接 import semantic/prep, 不再通过 wrapper)
        args = argparse.Namespace()
        args.job_root = root
        args.backend = cfg.backend
        args.episode_digests = Path(p["episode_digests"])
        args.chapter_digests = Path(p["chapter_digests"])
        args.event_cards = Path(p["event_cards"])
        args.max_context_chars = 600000
        batch_path = prepare_registry(args)

        # 2. 语义批次执行 (内部按序应用 admission → repair 链 → recovery)
        self.llm_port.run_batch(
            batch_path,
            backend=cfg.backend,
            workers=cfg.workers,
            requests_per_minute=cfg.requests_per_minute,
            semantic_retries=cfg.semantic_retries,
        )

        registry_path = root / "series-registry.json"
        ref = bus.put("series_registry", {
            "path": str(registry_path),
            "registry_admission": str(root / "series-registry-admission.json"),
            "registry_quarantine": str(root / "series-registry-quarantine.json"),
        }, stage="series_registry")
        update_project_stage(root / "project.json", "series_registry", "completed",
                             outputs={"series_registry": str(registry_path)})

        # ── Post-processing: highlight diff (best-effort, non-blocking) ──
        _run_highlight_diff(bus, cfg)

        # ── Post-processing: backfill window_id on evolution records (best-effort) ──
        _backfill_window_ids(bus, cfg)

        # ── Post-processing: merge VLM + API highlight sources (best-effort) ──
        _merge_highlight_sources(bus, cfg)

        return [ref]


# ── Post-processing: highlight diff ────────────────────────────────────────────


def _run_highlight_diff(bus: ArtifactBus, cfg) -> None:
    """对比 VLM highlight candidates 与 API 高光标记。

    从 artifact bus 获取 vlm_analysis 产物，从 DB 查询 API 高光 shots，
    执行 compare_highlights 并将 missed highlights 记录到 DB。

    此步骤为 best-effort，不阻塞主流程。
    """
    try:
        from autocut_core.libs.highlight_evolution import (
            compare_highlights,
            analyze_missed_highlight,
            evolve_highlight_skill,
        )
        from autocut_core.db.client import StageDBClient
        from autocut_core.io import load_jsonl
    except ImportError as exc:
        logger.debug("highlight_diff: 导入失败，跳过: %s", exc)
        return

    # 1. 获取 vlm_analysis 产物
    window_artifact = bus.latest("vlm_analysis") or bus.latest("window_analysis")
    if window_artifact is None:
        logger.debug("highlight_diff: 未找到 vlm_analysis 产物，跳过")
        return

    window_data = bus.get(window_artifact)
    summaries_path = window_data.get("path") if isinstance(window_data, dict) else None
    if not summaries_path:
        logger.debug("highlight_diff: vlm_analysis 产物缺少 path 字段")
        return

    try:
        windows = load_jsonl(Path(summaries_path))
    except Exception as exc:
        logger.warning("highlight_diff: 加载 window summaries 失败: %s", exc)
        return

    if not windows:
        logger.debug("highlight_diff: window summaries 为空")
        return

    # 2. 按 window_id 组织 VLM candidates
    vlm_candidates_by_window: dict[str, list[dict]] = {}
    for w in windows:
        wid = w.get("window_id", "")
        if not wid:
            continue
        candidates = w.get("candidates", [])
        if candidates:
            vlm_candidates_by_window[wid] = candidates

    if not vlm_candidates_by_window:
        logger.debug("highlight_diff: 无 VLM candidates 数据")
        return

    # 3. 查询 API 高光 shots
    db_url = getattr(cfg, "db_url", None)
    db_schema = getattr(cfg, "db_schema", "autocut")
    book_id = getattr(cfg, "book_id", None)

    if not db_url or not book_id:
        logger.debug(
            "highlight_diff: DB 不可用 (db_url=%s, book_id=%s)，跳过 API 高光对比",
            bool(db_url), bool(book_id),
        )
        return

    db = StageDBClient(db_url=db_url, schema=db_schema)
    if not db.is_available:
        logger.debug("highlight_diff: DB 不可用，跳过")
        return

    # 收集所有窗口的 episode/source_id 信息
    api_highlights_by_window: dict[str, list[dict]] = {}
    for w in windows:
        wid = w.get("window_id", "")
        if not wid:
            continue
        episode_id = w.get("episode")
        if episode_id is None:
            continue
        try:
            shots = db.query_shots(book_id, int(episode_id))
        except Exception as exc:
            logger.warning("highlight_diff: 查询 episode %s shots 失败: %s", episode_id, exc)
            continue
        # 筛选 is_highlight=True
        highlight_shots = [s for s in shots if s.get("is_highlight", False)]
        if highlight_shots:
            api_highlights_by_window[wid] = highlight_shots

    if not api_highlights_by_window:
        logger.debug("highlight_diff: 无 API 高光数据")
        return

    # 4. 逐窗口对比
    all_missed_analyses: list[dict] = []
    total_matched = 0
    total_missed = 0
    total_false_positives = 0

    for wid, vlm_candidates in vlm_candidates_by_window.items():
        api_highlights = api_highlights_by_window.get(wid, [])
        diff = compare_highlights(vlm_candidates, api_highlights)

        total_matched += len(diff["matched"])
        total_missed += len(diff["missed"])
        total_false_positives += len(diff["false_positives"])

        for missed in diff["missed"]:
            # 构建窗口上下文
            window_ctx = _build_window_context(wid, windows)
            analysis = analyze_missed_highlight(missed, window_ctx)
            analysis["window_id"] = wid
            analysis["api_highlight"] = missed["api"]
            all_missed_analyses.append(analysis)

    logger.info(
        "highlight_diff: 处理 %d 个窗口, matched=%d, missed=%d, false_positives=%d",
        len(vlm_candidates_by_window),
        total_matched,
        total_missed,
        total_false_positives,
    )

    # 5. 记录 missed highlights 到 DB
    if all_missed_analyses and db.is_available:
        _record_missed_to_db(db, all_missed_analyses)

    # 6. 判断是否需要触发 skill 进化
    evolve_highlight_skill(
        all_missed_analyses,
        db_client=db,
        book_id=book_id,
    )


def _build_window_context(
    window_id: str,
    windows: list[dict],
) -> dict:
    """为指定 window_id 构建分析上下文。"""
    for w in windows:
        if w.get("window_id") == window_id:
            window_meta = w.get("window", {})
            return {
                "window_start": float(window_meta.get("start", 0)),
                "window_end": float(window_meta.get("end", 0)),
                "window_summary": w.get("window_summary", ""),
                "story_beats": w.get("story_beats", []),
                "dialogue_and_text": w.get("dialogue_and_text", []),
                "visual_events": w.get("visual_events", []),
                "timeline_segments": w.get("timeline_segments", []),
            }
    return {}


def _record_missed_to_db(
    db,
    missed_analyses: list[dict],
) -> None:
    """将 missed highlight 分析结果写入 highlight_skill_evolution 表。"""
    import json
    from datetime import datetime, timezone

    try:
        for analysis in missed_analyses:
            cause = analysis.get("cause", "unknown")
            # D 类 (API 误判) 也记录，但标记为不触发进化
            db._execute(
                f"""
                INSERT INTO {db._schema}.highlight_skill_evolution
                    (skill_version, window_id, api_highlight,
                     vlm_miss_reason, skill_update, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    "v1",  # 初始版本，记录未进化前的状态
                    analysis.get("window_id", "unknown"),
                    json.dumps(analysis.get("api_highlight", {}), ensure_ascii=False),
                    analysis.get("reason", ""),
                    json.dumps({
                        "cause": cause,
                        "suggestion": analysis.get("suggestion", ""),
                        "confidence": analysis.get("confidence", 0),
                    }, ensure_ascii=False),
                    datetime.now(timezone.utc),
                ),
            )
        logger.info(
            "highlight_diff: 已记录 %d 条 missed highlight 到 DB",
            len(missed_analyses),
        )
    except Exception as exc:
        logger.warning("highlight_diff: DB 记录失败: %s", exc)


# ── Post-processing: backfill window_id on evolution records ─────────────────────


def _backfill_window_ids(bus: ArtifactBus, cfg) -> None:
    """Backfill window_id on highlight_skill_evolution records with empty window_id.

    Reads VLM window_summaries from the artifact bus, extracts highlight-type
    candidates, and matches them against API highlights stored in
    highlight_skill_evolution where window_id is NULL or empty.

    Matching uses IoU > 0.3 via _time_range_overlap from highlight_evolution.
    Records are matched by episode_id extracted from the stored api_highlight JSONB.

    This is a best-effort, non-blocking step — failures are logged and skipped.
    """
    try:
        from autocut_core.libs.highlight_evolution import _time_range_overlap
        from autocut_core.db.client import StageDBClient
        from autocut_core.io import load_jsonl
    except ImportError as exc:
        logger.debug("backfill_window_ids: 导入失败，跳过: %s", exc)
        return

    # 1. Read VLM window_summaries from the bus
    window_artifact = bus.latest("vlm_analysis") or bus.latest("window_analysis")
    if window_artifact is None:
        logger.debug("backfill_window_ids: 未找到 vlm_analysis 产物，跳过")
        return

    window_data = bus.get(window_artifact)
    summaries_path = window_data.get("path") if isinstance(window_data, dict) else None
    if not summaries_path:
        logger.debug("backfill_window_ids: vlm_analysis 产物缺少 path 字段")
        return

    try:
        windows = load_jsonl(Path(summaries_path))
    except Exception as exc:
        logger.warning("backfill_window_ids: 加载 window summaries 失败: %s", exc)
        return

    if not windows:
        return

    # 2. Build episode -> [(window_id, vlm_highlight_candidate), ...] map
    episode_vlm_map: dict[int, list[tuple[str, dict]]] = {}
    for w in windows:
        episode_id = w.get("episode")
        wid = w.get("window_id", "")
        if episode_id is None or not wid:
            continue
        candidates = w.get("candidates", [])
        highlights = [
            c for c in candidates
            if isinstance(c, dict) and c.get("type") == "highlight"
        ]
        if highlights:
            try:
                ep = int(episode_id)
            except (ValueError, TypeError):
                continue
            if ep not in episode_vlm_map:
                episode_vlm_map[ep] = []
            for h in highlights:
                episode_vlm_map[ep].append((wid, h))

    if not episode_vlm_map:
        logger.debug("backfill_window_ids: 无 VLM highlight candidates")
        return

    # 3. Query DB for records with empty window_id
    db_url = getattr(cfg, "db_url", None)
    db_schema = getattr(cfg, "db_schema", "autocut")
    if not db_url:
        return

    db = StageDBClient(db_url=db_url, schema=db_schema)
    if not db.is_available:
        return

    try:
        empty_records = db.query_highlight_evolution_empty_window()
    except Exception as exc:
        logger.warning("backfill_window_ids: 查询 empty window 记录失败: %s", exc)
        return

    if not empty_records:
        return

    # 4. Match each record against VLM candidates
    import json

    iou_threshold = 0.3
    updated_count = 0

    for record in empty_records:
        record_id = record.get("id")
        api_highlight_raw = record.get("api_highlight")
        if api_highlight_raw is None:
            continue

        # Parse api_highlight JSONB (may be string or already dict)
        if isinstance(api_highlight_raw, str):
            try:
                api_highlight = json.loads(api_highlight_raw)
            except json.JSONDecodeError:
                logger.debug(
                    "backfill_window_ids: record %s JSON 解析失败，跳过", record_id
                )
                continue
        else:
            api_highlight = api_highlight_raw

        api_start = float(api_highlight.get("start_time", 0))
        api_end = float(api_highlight.get("end_time", 0))
        api_episode = api_highlight.get("episode_id")
        if api_end <= api_start or api_episode is None:
            continue

        try:
            api_ep = int(api_episode)
        except (ValueError, TypeError):
            continue

        vlm_pairs = episode_vlm_map.get(api_ep, [])
        if not vlm_pairs:
            continue

        # Find best IoU match among VLM candidates in the same episode
        best_match: tuple[str, dict] | None = None
        best_iou = 0.0
        for wid, vlm in vlm_pairs:
            v_start = float(vlm.get("start", 0))
            v_end = float(vlm.get("end", 0))
            if v_end <= v_start:
                continue
            iou = _time_range_overlap(api_start, api_end, v_start, v_end)
            if iou > best_iou:
                best_iou = iou
                best_match = (wid, vlm)

        if best_match is not None and best_iou > iou_threshold:
            window_id = best_match[0]
            try:
                db.update_highlight_evolution_window_id(record_id, window_id)
                updated_count += 1
            except Exception as exc:
                logger.warning(
                    "backfill_window_ids: 更新 record %s window_id 失败: %s",
                    record_id,
                    exc,
                )

    if updated_count > 0:
        logger.info(
            "backfill_window_ids: 已更新 %d 条记录的 window_id",
            updated_count,
        )


# ── Post-processing: merge VLM + API highlight sources ────────────────────────


def _merge_highlight_sources(bus: ArtifactBus, cfg) -> None:
    """Merge VLM and API highlight sources from shots table.

    Reads VLM highlights (source='vlm', is_highlight=True) and API highlights
    (source='api', is_highlight=True) from the shots table, calls
    merge_vlm_api_highlights() to match them by IoU, then:
      - Updates matched pairs to source='vlm+api'
      - Records API-only (VLM missed) to highlight_skill_evolution

    This is best-effort, non-blocking -- failures are logged as warnings.
    """
    import json

    try:
        from autocut_core.libs.highlight_evolution import merge_vlm_api_highlights
        from autocut_core.db.client import StageDBClient, _t
    except ImportError as exc:
        logger.debug("merge_highlight_sources: 导入失败，跳过: %s", exc)
        return

    db_url = getattr(cfg, "db_url", None)
    db_schema = getattr(cfg, "db_schema", "autocut")
    book_id = getattr(cfg, "book_id", None)

    if not db_url or not book_id:
        logger.debug("merge_highlight_sources: DB 不可用，跳过")
        return

    db = StageDBClient(db_url=db_url, schema=db_schema)
    if not db.is_available:
        logger.debug("merge_highlight_sources: DB 不可用，跳过")
        return

    shots_table = _t(db_schema, "shots")

    # 1. Read VLM highlights from shots table
    try:
        vlm_rows = db._execute(
            f"SELECT * FROM {shots_table} "
            f"WHERE book_id = %s AND is_highlight = true AND source = %s "
            f"ORDER BY episode_id, start_time",
            (book_id, "vlm"),
        )
        vlm_shots = [dict(r) for r in vlm_rows]
    except Exception as exc:
        logger.warning("merge_highlight_sources: 查询 VLM shots 失败: %s", exc)
        return

    # 2. Read API highlights from shots table
    try:
        api_rows = db._execute(
            f"SELECT * FROM {shots_table} "
            f"WHERE book_id = %s AND is_highlight = true AND source = %s "
            f"ORDER BY episode_id, start_time",
            (book_id, "api"),
        )
        api_shots = [dict(r) for r in api_rows]
    except Exception as exc:
        logger.warning("merge_highlight_sources: 查询 API shots 失败: %s", exc)
        return

    if not vlm_shots and not api_shots:
        logger.debug("merge_highlight_sources: 无高光数据")
        return

    # 3. Group by episode_id
    episodes: set[int] = set()
    for s in vlm_shots + api_shots:
        ep = s.get("episode_id")
        if ep is not None:
            episodes.add(int(ep))

    total_matched = 0
    total_vlm_only = 0
    total_api_only = 0

    for episode_id in sorted(episodes):
        ep_vlm_raw = [
            s for s in vlm_shots if int(s.get("episode_id", 0)) == episode_id
        ]
        ep_api_raw = [
            s for s in api_shots if int(s.get("episode_id", 0)) == episode_id
        ]

        if not ep_vlm_raw or not ep_api_raw:
            total_vlm_only += len(ep_vlm_raw)
            total_api_only += len(ep_api_raw)
            continue

        # Normalize VLM keys: merge_vlm_api_highlights expects start/end for VLM
        ep_vlm = [
            {**s, "start": s.get("start_time", 0), "end": s.get("end_time", 0)}
            for s in ep_vlm_raw
        ]

        # 4. Call merge_vlm_api_highlights
        result = merge_vlm_api_highlights(ep_vlm, ep_api_raw)

        total_matched += len(result["merged"])
        total_vlm_only += len(result["vlm_only"])
        total_api_only += len(result["api_only"])

        # 5. Update matched pairs: set source='vlm+api' on the VLM shot
        for match in result["merged"]:
            vlm_shot = match["vlm"]
            shot_id = vlm_shot.get("id")
            if shot_id:
                try:
                    db._execute(
                        f"UPDATE {shots_table} SET source = %s WHERE id = %s",
                        ("vlm+api", shot_id),
                    )
                except Exception as exc:
                    logger.warning(
                        "merge_highlight_sources: 更新 shot %s 失败: %s",
                        shot_id,
                        exc,
                    )

        # 6. Record API-only (VLM missed) to highlight_skill_evolution
        for api_only in result["api_only"]:
            api_shot = api_only["api"]
            try:
                db.record_highlight_evolution(
                    skill_version="v1",
                    window_id=str(episode_id),
                    api_highlight=api_shot,
                    vlm_miss_reason="VLM missed this API highlight",
                    skill_update=json.dumps(
                        {"cause": "vlm_missed", "suggestion": ""},
                        ensure_ascii=False,
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "merge_highlight_sources: 记录 missed highlight 失败: %s", exc
                )

    # 7. Log summary
    logger.info(
        "高光对比: %d matched, %d VLM-only, %d API-only (missed)",
        total_matched,
        total_vlm_only,
        total_api_only,
    )
