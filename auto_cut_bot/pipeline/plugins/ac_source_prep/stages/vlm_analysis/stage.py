"""VlmAnalysisStage — 批量 VLM 逐窗语义分析 (VLM-first architecture)。

消费 source_windows 产出的 window_batch, 逐窗调用 VLM 分析
视频内容, 汇总为 window_summaries (jsonl), 同时写入 DB。

VLM-first: 不再依赖 source_metadata/source_script/asr_transcript,
VLM 直接从视频提取所有信息。global_context 注入全剧级上下文
(synopsis, themes, character_relationships) 提升 VLM 准确度。

DB 写入 (best-effort, 不阻塞):
  - visual_events → shots 表 (source='vlm')
  - dialogue_and_text → subtitles 表 (source='vlm')
  - character_appearances → subjects 表 (source='vlm')
  - scene_changes → scenes 表 (source='vlm')
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autocut_core import (
    ArtifactBus,
    Artifact,
    Stage,
    StageContract,
    Task,
    get_logger,
)
from autocut_core.backends._base import get_backend
from autocut_core.db.client import StageDBClient
from autocut_core.io import (
    atomic_write_jsonl, load_json, update_project_stage, utc_now,
)
from autocut_core.semantic.batch_orchestrator import run_batch
from autocut_core.semantic.prompt_context import build_global_context_injection

logger = get_logger(__name__)

_VLM_ANALYSIS_TASK = "vlm_analysis"


class VlmAnalysisStage(Stage):
    """对每个窗口调用 VLM 做语义分析并写入 DB (VLM-first, 零辅助输入)。"""

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="vlm_analysis",
            input_artifacts=["source_windows", "global_context", "scene_boundaries"],
            output_artifacts=["window_summaries"],
            description="VLM-first 逐窗视频语义分析 + DB 写入",
            db_reads=["subjects", "global_context"],
            db_writes=["shots", "subjects", "scenes", "subtitles"],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """从 bus 获取 window_batch 路径和 global_context。"""
        ref = bus.latest("source_windows")
        if ref is None:
            raise RuntimeError("上游 source_windows 产物未找到")

        artifacts = bus.get(ref)
        batch_path_str = self._resolve_batch_path(artifacts, bus, ref)
        if not batch_path_str:
            raise RuntimeError("无法找到 window_batch 路径")

        global_ctx = self._read_optional_artifact(bus, "global_context")

        return [
            Task(
                type="semantic_batch",
                payload={
                    "batch_path": str(batch_path_str),
                    "global_context": global_ctx,
                },
            )
        ]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """运行 LLM 推理 → 收集记录 → 写 DB → 发布产物。"""
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        job_root: Path = cfg.job_root
        task = tasks[0]
        batch_path = Path(task.payload["batch_path"])
        global_ctx = task.payload.get("global_context")

        # ── 步骤 1: LLM 推理 ──
        get_backend(cfg.backend)
        logger.info("vlm_analysis: LLM 批处理开始")
        self._run_llm_batch(batch_path, global_ctx)
        logger.info("vlm_analysis: LLM 批处理完成")

        # ── 步骤 2: 收集窗口记录 ──
        manifest = load_json(batch_path)
        records = _collect_window_records(manifest)
        logger.info("vlm_analysis: 收集到 %d 条窗口记录", len(records))

        # ── 步骤 2.5: PySceneDetect 边界修正 ──
        scene_ref = bus.latest("scene_boundaries")
        if scene_ref is not None:
            try:
                from autocut_core.semantic.scene_boundary_fusion import (
                    apply_scene_boundary_fusion,
                )
                scene_data = bus.get(scene_ref)
                corrected_count = 0
                for rec in records:
                    episode_id = str(rec.get("episode", "1"))
                    result = apply_scene_boundary_fusion(rec, scene_data, episode_id)
                    if result is not rec:  # 有修正
                        corrected_count += 1
                logger.info(
                    "vlm_analysis: PySceneDetect 边界修正完成 — %d/%d 窗口已对齐",
                    corrected_count, len(records),
                )
            except Exception as exc:
                logger.warning(
                    "vlm_analysis: PySceneDetect 边界修正失败 (非阻塞): %s", exc,
                )

        # ── 步骤 3: 写入 DB (best-effort, 不阻塞) ──
        book_id = _resolve_book_id(cfg, global_ctx)
        db = StageDBClient(db_url=cfg.db_url, schema=cfg.db_schema)
        if db.is_available and book_id:
            try:
                stats = _write_vlm_output_to_db(db, book_id, records)
                logger.info(
                    "vlm_analysis: DB 写入完成 — shots=%d, subtitles=%d, subjects=%d, scenes=%d",
                    stats["shots"], stats["subtitles"], stats["subjects"], stats["scenes"],
                )
            except Exception as exc:
                logger.warning("vlm_analysis: DB 写入失败 (非阻塞): %s", exc)

        # ── 步骤 4: 落盘产物 ──
        output_path = job_root / "window-summaries.jsonl"
        atomic_write_jsonl(output_path, records)

        ref = bus.put(
            "window_summaries",
            {"path": str(output_path), "backend": cfg.backend},
            stage="vlm_analysis",
            inputs=(
                [bus.latest("source_windows")]
                if bus.latest("source_windows")
                else []
            ),
        )

        update_project_stage(
            job_root / "project.json",
            "vlm_analysis",
            "completed",
            outputs={"window_summaries": ref.sha256},
        )

        return [ref]

    # ── 私有方法 ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_batch_path(
        artifacts: dict[str, Any], bus: ArtifactBus, ref: Artifact
    ) -> str | None:
        """从多种可能的产物结构中解析 window_batch 路径。"""
        if isinstance(artifacts, dict):
            batch = artifacts.get("window_batch", {})
            if isinstance(batch, dict) and batch.get("path"):
                return batch["path"]
            for key in ("window_batch", "batch_path", "output"):
                val = artifacts.get(key)
                if isinstance(val, str) and val:
                    return val
        wb_key = "source_windows/window_batch"
        wb_ref = bus._index.get(wb_key)
        if wb_ref is not None:
            try:
                wb_data = bus.get(wb_ref)
                if isinstance(wb_data, dict) and wb_data.get("path"):
                    return wb_data["path"]
                return wb_ref._path
            except Exception:
                pass
        project = bus.get(ref)
        if isinstance(project, dict):
            stages = project.get("stages", {})
            sw = stages.get("source_windows", {}) if isinstance(stages, dict) else {}
            outputs = sw.get("outputs", {}) if isinstance(sw, dict) else {}
            path = outputs.get("window_batch")
            if isinstance(path, str) and path:
                return path
        return None

    @staticmethod
    def _read_optional_artifact(
        bus: ArtifactBus, stage_name: str
    ) -> dict[str, Any] | None:
        """读取可选的上游产物, 不可用时返回 None。"""
        ref = bus.latest(stage_name)
        if ref is None:
            logger.info("可选数据源 %s 不可用", stage_name)
            return None
        try:
            data = bus.get(ref)
            if isinstance(data, dict):
                status = data.get("status", "")
                if status == "unavailable":
                    logger.info("可选数据源 %s 状态为 unavailable", stage_name)
                    return None
                return data
            return None
        except Exception as exc:
            logger.warning("读取可选数据源 %s 失败: %s", stage_name, exc)
            return None

    def _run_llm_batch(
        self,
        batch_path: Path,
        global_ctx: dict[str, Any] | None = None,
    ) -> None:
        """直接调用语义推理引擎，注入 global_context 到 VLM prompt。"""
        print(f"[{utc_now()}] [vlm_analysis] run_batch({batch_path})")
        context_injection = self._build_context_injection(global_ctx)
        run_batch(
            batch_path,
            backend=self.config.backend,
            workers=self.config.workers,
            requests_per_minute=self.config.requests_per_minute,
            semantic_retries=self.config.semantic_retries,
            context_injection=context_injection,
        )

    def _build_context_injection(
        self,
        global_ctx: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """构建 VLM prompt 上下文注入。

        VLM-first 模式下只注入高置信度全局上下文:
          - global_context: 全剧 synopsis/themes/character_relationships
          - 角色名 (subjects 表, 高置信度)
        """
        cfg = self.config
        db = StageDBClient(db_url=cfg.db_url, schema=cfg.db_schema)
        book_id = cfg.extra.get("book_id", "")

        result: dict[str, Any] = {}

        # 1. global_context artifact 注入
        if global_ctx and isinstance(global_ctx, dict):
            result["global_context"] = {
                "synopsis": global_ctx.get("synopsis", ""),
                "themes": global_ctx.get("themes", []),
                "character_relationships": global_ctx.get("character_relationships", []),
            }

        # 2. 从 DB 加载角色名
        if db.is_available and book_id:
            try:
                subjects = db.query_subjects(book_id)
                if subjects:
                    characters = [
                        {"name": s.get("name", ""), "role": s.get("role", ""), "traits": s.get("traits", "")}
                        for s in subjects if isinstance(s, dict) and s.get("name")
                    ]
                    if characters:
                        result["characters"] = characters
            except Exception as exc:
                logger.warning("加载角色上下文失败: %s", exc)

        # 3. DB global_context 表 fallback
        if not result.get("global_context") and db.is_available and book_id:
            global_context_text = build_global_context_injection(book_id, db)
            if global_context_text:
                result["global_context"] = global_context_text

        return result if result else None


# ═══════════════════════════════════════════════════════════════════════════════
# DB 写入 (best-effort, 不阻塞)
# ═══════════════════════════════════════════════════════════════════════════════


def _write_vlm_output_to_db(
    db: StageDBClient,
    book_id: str,
    records: list[dict[str, Any]],
) -> dict[str, int]:
    """将 VLM window_summaries 按表拆解写入 DB。

    写入策略:
      - visual_events → shots 表 (source='vlm')
      - dialogue_and_text → subtitles 表 (source='vlm')
      - character_appearances → subjects 表 (source='vlm', vlm_verified=True)
      - scene_locations → scenes 表 (source='vlm')

    Returns: {shots, subtitles, subjects, scenes} 写入计数
    """
    stats = {"shots": 0, "subtitles": 0, "subjects": 0, "scenes": 0}

    for record in records:
        if not isinstance(record, dict):
            continue
        ep = record.get("episode", 0)
        window_id = record.get("window_id", "")

        # 0. Ensure episode exists (FK guard: shots/subtitles depend on episodes)
        try:
            db.upsert_episodes(book_id, [{"episode_id": ep}], source="vlm")
        except Exception:
            pass  # FK will surface later if this fails; don't block VLM write loop

        # 1. visual_events → shots
        for event in record.get("visual_events", []):
            if not isinstance(event, dict):
                continue
            start = event.get("start", 0)
            end = event.get("end", 0)
            if start >= end:
                continue
            db.insert_shots(book_id, ep, [{
                "start_time": start,
                "end_time": end,
                "subjects": event.get("characters", []),
                "actions": event.get("description", ""),
                "is_highlight": event.get("is_highlight", False),
                "source": "vlm",
                "vlm_event_type": event.get("event_type", ""),
                "vlm_window_id": window_id,
            }])
            stats["shots"] += 1

        # 2. dialogue_and_text → subtitles
        for d in record.get("dialogue_and_text", []):
            if not isinstance(d, dict):
                continue
            start = d.get("start", 0)
            end = d.get("end", 0)
            if start >= end:
                continue
            text = d.get("text", "")
            if not text:
                continue
            db.insert_subtitles(book_id, ep, [{
                "start_time": start,
                "end_time": end,
                "text": text,
                "speaker": d.get("speaker", ""),
                "tone": d.get("tone", ""),
                "emotion": d.get("emotion", ""),
                "source": "vlm",
                "kind": d.get("kind", "dialogue"),
                "language": d.get("language", "zh"),
            }], source="vlm")
            stats["subtitles"] += 1

        # 3. character_appearances → subjects
        for c in record.get("character_appearances", []):
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            if not name:
                continue
            db.upsert_subjects(book_id, [{
                "name": name,
                "role": c.get("role", ""),
                "first_episode": ep,
                "source": "vlm",
                "vlm_verified": True,
            }], source="vlm")
            stats["subjects"] += 1

        # 4. scene_locations / story_beats → scenes
        for scene in record.get("scene_locations", []):
            if not isinstance(scene, dict):
                continue
            scene_id = f"{window_id}-s{stats['scenes'] + 1:03d}"
            db.upsert_scenes(book_id, [{
                "scene_id": scene_id,
                "episode_id": ep,
                "location": scene.get("location_name", ""),
                "time_of_day": scene.get("time_of_day", ""),
                "raw_description": scene.get("environment", ""),
                "start_time": scene.get("start"),
                "end_time": scene.get("end"),
                "source": "vlm",
                "detected_in_video": True,
                "vlm_verified": True,
            }])
            stats["scenes"] += 1

    return stats


def _resolve_book_id(
    cfg: Any,
    global_ctx: dict[str, Any] | None = None,
) -> str | None:
    """从多个来源解析 book_id: config > global_context > source_manifest。"""
    book_id = cfg.extra.get("book_id", "")
    if book_id:
        return book_id
    if global_ctx and isinstance(global_ctx, dict):
        book_id = global_ctx.get("book_id", "")
        if book_id:
            return book_id
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 内联产物组装
# ═══════════════════════════════════════════════════════════════════════════════


def _collect_window_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """从语义批处理 manifest 中收集窗口分析记录。"""
    records: list[dict[str, Any]] = []
    for job in manifest.get("jobs", []):
        if not isinstance(job, dict) or job.get("task") != _VLM_ANALYSIS_TASK:
            continue
        output = job.get("output")
        if not isinstance(output, str):
            raise ValueError(
                f"vlm_analysis job 缺少 output 字段: {job.get('id', '?')}"
            )
        path = Path(output).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"窗口分析产物缺失: {path}")
        value = load_json(path)
        records.append(value)

    if not records:
        raise ValueError("manifest 中无已完成的 vlm_analysis 输出")

    records.sort(
        key=lambda item: (item["episode"], item["window"]["start"], item["window_id"])
    )
    return records


__all__ = ["VlmAnalysisStage"]
