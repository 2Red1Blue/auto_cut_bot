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
from autocut_core.db.client import StageDBClient
from autocut_core.io import (
    atomic_write_jsonl, load_json, update_project_stage, utc_now,
)
from autocut_core.stages.ports import LLMPort, get_llm_port

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

    def __init__(self, config: PipelineConfig, llm_port: LLMPort | None = None) -> None:
        super().__init__(config)
        self._llm_port: LLMPort | None = llm_port

    @property
    def llm_port(self) -> LLMPort:
        if self._llm_port is None:
            self._llm_port = get_llm_port()
        return self._llm_port

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
        logger.info("vlm_analysis: LLM 批处理开始")
        context_injection = self.llm_port.build_context_injection(
            self.contract.stage_name, self.config, bus,
        )
        # --window-ids: 仅分析指定窗口 (CLI 透传到 config.window_ids)
        window_ids = getattr(cfg, "window_ids", None)
        if window_ids:
            logger.info(
                "vlm_analysis: 窗口过滤模式 — 仅分析 %d 个窗口: %s",
                len(window_ids), window_ids,
            )
        self.llm_port.run_batch(
            batch_path,
            backend=cfg.backend,
            workers=cfg.workers,
            requests_per_minute=cfg.requests_per_minute,
            semantic_retries=cfg.semantic_retries,
            context_injection=context_injection,
            job_ids=window_ids,
        )
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
                    validate_scene_boundaries,
                )
                scene_data = bus.get(scene_ref)
                
                # 质量检测：在应用 fusion 之前验证场景数据
                validation_result = validate_scene_boundaries(scene_data)
                if not validation_result["valid"]:
                    logger.warning(
                        "vlm_analysis: 场景边界数据质量问题: %s",
                        "; ".join(validation_result["issues"][:5]),
                    )
                    # 统计有效集数
                    valid_episodes = [
                        ep_id for ep_id, quality in validation_result["episodes_quality"].items()
                        if quality["valid"]
                    ]
                    logger.info(
                        "vlm_analysis: %d/%d 集的场景数据有效（注意：将处理所有窗口，不做按集过滤）",
                        len(valid_episodes), len(validation_result["episodes_quality"]),
                    )
                else:
                    logger.info(
                        "vlm_analysis: 场景边界数据质量良好 (%d 集, %d 场景)",
                        validation_result["stats"]["total_episodes"],
                        validation_result["stats"]["total_scenes"],
                    )
                
                # 从 config.yaml 的 fusion 段读取参数
                fusion_cfg = cfg.extra.get("fusion", {})
                fusion_lead_in = float(fusion_cfg.get("lead_in", 0.3))
                fusion_lead_out = float(fusion_cfg.get("lead_out", 0.0))
                fusion_tolerance = float(fusion_cfg.get("tolerance", 0.5))
                audio_max_shift = float(fusion_cfg.get("audio_max_shift", 3.0))

                # 获取静音区间数据（音频门控 fallback）
                silence_data = None
                silence_ref = bus.latest("silence_intervals")
                if silence_ref is not None:
                    try:
                        silence_data = bus.get(silence_ref)
                        if silence_data:
                            ep_count = len(silence_data.get("episodes", {}))
                            logger.info(
                                "vlm_analysis: 已加载静音区间数据 (%d 集, noise=%.0fdB)",
                                ep_count,
                                silence_data.get("noise_db", -30),
                            )
                    except Exception as exc:
                        logger.warning("vlm_analysis: 读取静音区间失败: %s", exc)
                        silence_data = None

                # 获取 VAD 语音区间数据（Demucs+Silero 或 ASR-Anchor 基础区间）
                speech_data = None
                speech_ref = bus.latest("speech_intervals")
                if speech_ref is not None:
                    try:
                        speech_data = bus.get(speech_ref)
                        if speech_data:
                            ep_count = len(speech_data.get("episodes", {}))
                            logger.info(
                                "vlm_analysis: 已加载VAD语音区间数据 (%d 集, detector=%s)",
                                ep_count,
                                speech_data.get("detector", "?"),
                            )
                    except Exception as exc:
                        logger.warning("vlm_analysis: 读取VAD语音区间失败: %s", exc)
                        speech_data = None

                # 获取 ASR anchor results（三层精确 snap 用）
                anchor_data_raw = None
                anchor_results = None
                anchor_ref = bus.latest("asr_anchor_results")
                if anchor_ref is not None:
                    try:
                        anchor_data_raw = bus.get(anchor_ref)
                        if anchor_data_raw and anchor_data_raw.get("episodes"):
                            from autocut_core.audio.asr_anchor import result_from_dict
                            anchor_results = {}
                            for ep_id, ep_dict in anchor_data_raw["episodes"].items():
                                try:
                                    anchor_results[ep_id] = result_from_dict(ep_dict)
                                except Exception as exc:
                                    logger.warning("vlm_analysis: 反序列化 anchor ep=%s 失败: %s", ep_id, exc)
                            logger.info(
                                "vlm_analysis: 已加载ASR anchor数据 (%d 集, model=%s)",
                                len(anchor_results),
                                anchor_data_raw.get("model", "?"),
                            )
                    except Exception as exc:
                        logger.warning("vlm_analysis: 读取ASR anchor数据失败: %s", exc)
                        anchor_results = None

                corrected_count = 0
                audio_skipped_count = 0
                for rec in records:
                    episode_id = str(rec.get("episode", "1"))
                    # 记录 fusion 前的 candidate 时间戳用于比对
                    candidates_before = [
                        (c.get("start"), c.get("end"))
                        for c in rec.get("candidates", [])
                        if isinstance(c, dict) and c.get("type") in ("highlight", "hook", "scene")
                    ]
                    # Get cue_text from VLM event (for ASR cue matching)
                    event_cue_texts = None
                    if anchor_results:
                        event_cue_texts = {}
                        for c in rec.get("candidates", []):
                            if isinstance(c, dict):
                                cid = c.get("id", "")
                                cstart = c.get("start")
                                cue = c.get("first_words") or c.get("cue_text") or c.get("cue")
                                if cue:
                                    event_cue_texts[cid] = cue
                                    if cstart is not None:
                                        event_cue_texts[str(cstart)] = cue

                    apply_scene_boundary_fusion(
                        rec, scene_data, episode_id,
                        snap_tolerance=fusion_tolerance,
                        lead_in=fusion_lead_in,
                        lead_out=fusion_lead_out,
                        silence_intervals=silence_data,
                        speech_intervals=speech_data,
                        audio_max_shift=audio_max_shift,
                        anchor_results=anchor_results,
                        cue_texts=event_cue_texts,
                        fusion_cfg=fusion_cfg,
                    )
                    # 统计被音频门控跳过 snap 的 candidates
                    for c in rec.get("candidates", []):
                        if isinstance(c, dict) and c.get("audio_snap_skipped"):
                            audio_skipped_count += 1
                    # 检查是否有 candidate 被修正 (start/end 与原始值不同)
                    candidates_after = [
                        (c.get("start"), c.get("end"))
                        for c in rec.get("candidates", [])
                        if isinstance(c, dict) and c.get("type") in ("highlight", "hook", "scene")
                    ]
                    if any(
                        abs(b[0] - a[0]) > 0.001 or abs(b[1] - a[1]) > 0.001
                        for b, a in zip(candidates_before, candidates_after)
                        if b[0] is not None and b[1] is not None and a[0] is not None and a[1] is not None
                    ):
                        corrected_count += 1
                logger.info(
                    "vlm_analysis: PySceneDetect 边界修正完成 — %d/%d 窗口已对齐"
                    " (tolerance=%.1f, lead_in=%.1f, lead_out=%.1f, audio_max_shift=%.1f)"
                    "%s",
                    corrected_count, len(records),
                    fusion_tolerance, fusion_lead_in, fusion_lead_out, audio_max_shift,
                    (
                        f", VAD门控: speech={speech_data is not None}, "
                        f"silence={silence_data is not None}, "
                        f"跳过 {audio_skipped_count} 个切点"
                    ) if (silence_data or speech_data) else " (纯视觉模式)",
                )
            except Exception as exc:
                logger.warning(
                    "vlm_analysis: PySceneDetect 边界修正失败 (非阻塞): %s", exc,
                )

        # ── 步骤 2.6: VLM 高光精准场景边界标注 ──
        if scene_ref is not None:
            try:
                from autocut_core.libs.highlight_evolution import (
                    annotate_highlights_with_scene_boundaries,
                )
                # 从 config.yaml 读取 fusion 参数（与 step 2.5 保持一致）
                fusion_cfg = cfg.extra.get("fusion", {})
                fusion_lead_in = float(fusion_cfg.get("lead_in", 0.3))
                fusion_lead_out = float(fusion_cfg.get("lead_out", 0.0))
                fusion_tolerance = float(fusion_cfg.get("tolerance", 0.5))
                
                scene_data = bus.get(scene_ref)
                annotated_db = StageDBClient(db_url=cfg.db_url, schema=cfg.db_schema)
                annotated_book_id = _resolve_book_id(cfg, global_ctx)
                annotated_count = 0
                # 按 episode 收集 vlm_highlight_boundary shots，循环后整批原子替换，
                # 避免同一 episode 被多个 window 重复累积（重跑幂等）。
                ep_boundary_shots: dict[int, list[dict[str, Any]]] = {}
                for rec in records:
                    candidates = rec.get("candidates", [])
                    highlights = [
                        c for c in candidates
                        if isinstance(c, dict) and c.get("type") == "highlight"
                    ]
                    if not highlights:
                        continue
                    episode_id = str(rec.get("episode", "1"))
                    annotated = annotate_highlights_with_scene_boundaries(
                        highlights, scene_data, episode_id,
                        tolerance=fusion_tolerance,
                        lead_in=fusion_lead_in,
                        lead_out=fusion_lead_out,
                        silence_intervals=silence_data,
                        speech_intervals=speech_data,
                        audio_max_shift=audio_max_shift,
                    )
                    if annotated_db.is_available and annotated_book_id:
                        ep = rec.get("episode", 1)
                        window_id = rec.get("window_id", "")
                        for item in annotated:
                            start = item.get("precise_start", item.get("start", 0))
                            end = item.get("precise_end", item.get("end", 0))
                            if start >= end:
                                continue
                            ep_boundary_shots.setdefault(ep, []).append({
                                "book_id": annotated_book_id,
                                "episode_id": ep,
                                "start_time": start,
                                "end_time": end,
                                "subjects": item.get("characters", []),
                                "actions": item.get(
                                    "description", item.get("event", ""),
                                ),
                                "is_highlight": True,
                                "source": "vlm_highlight_boundary",
                                "vlm_event_type": item.get(
                                    "event_type", "highlight",
                                ),
                                "vlm_window_id": window_id,
                            })
                            annotated_count += 1
                # 按 episode 原子替换 vlm_highlight_boundary shots
                for ep, shots in ep_boundary_shots.items():
                    try:
                        annotated_db.replace_shots(
                            annotated_book_id, ep, shots,
                            sources=["vlm_highlight_boundary"],
                        )
                    except Exception as exc:
                        logger.warning(
                            "vlm_analysis: 替换 vlm_highlight_boundary shots 失败 "
                            "[book=%s, ep=%s, n=%d]: %s",
                            annotated_book_id, ep, len(shots), exc,
                        )
                logger.info(
                    "vlm_analysis: VLM 高光场景边界标注完成 — %d 条",
                    annotated_count,
                )
            except Exception as exc:
                logger.warning(
                    "vlm_analysis: VLM 高光场景边界标注失败 (非阻塞): %s", exc,
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


# ═══════════════════════════════════════════════════════════════════════════════
# DB 写入 (best-effort, 不阻塞)
# ═══════════════════════════════════════════════════════════════════════════════

# VLM confidence (str enum: high/medium/low) → DB confidence (real)
_CONFIDENCE_TO_FLOAT: dict[str, float] = {"high": 0.9, "medium": 0.7, "low": 0.4}


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
      - story_beats → story_beats 表 (source='vlm')
      - window_summary → episodes.summary

    Returns: {shots, subtitles, subjects, scenes, story_beats} 写入计数
    """
    stats = {"shots": 0, "subtitles": 0, "subjects": 0, "scenes": 0, "story_beats": 0}
    # 按 episode 收集，循环结束后原子替换（重跑幂等）
    episode_vlm_shots: dict[int, list[dict[str, Any]]] = {}
    episode_beats: dict[int, list[dict[str, Any]]] = {}
    episode_summaries: dict[int, list[str]] = {}

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

        # ── 构建 highlight 时间范围（从 candidates 推导） ──
        highlight_ranges = [
            (c["start"], c["end"])
            for c in record.get("candidates", [])
            if isinstance(c, dict) and c.get("type") == "highlight"
        ]

        # 1. visual_events → shots（先收集，循环后按 episode 原子替换）
        for event in record.get("visual_events", []):
            if not isinstance(event, dict):
                continue
            start = event.get("start", 0)
            end = event.get("end", 0)
            if start >= end:
                continue
            is_highlight = any(
                hs <= start and end <= he for hs, he in highlight_ranges
            )
            episode_vlm_shots.setdefault(ep, []).append({
                "book_id": book_id,
                "episode_id": ep,
                "start_time": start,
                "end_time": end,
                "subjects": event.get("characters", []),
                "actions": event.get("description", ""),
                "is_highlight": is_highlight,
                "source": "vlm",
                "vlm_window_id": window_id,
                "emotion": event.get("emotion", ""),
                "conflict": event.get("conflict", ""),
                "visual_impact": event.get("visual_impact", ""),
            })
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
                "speaker": d.get("speaker_or_source", ""),
                "confidence": _CONFIDENCE_TO_FLOAT.get(
                    str(d.get("confidence", "")), d.get("confidence") if isinstance(d.get("confidence"), (int, float)) else None
                ),
                "source": "vlm",
                "kind": d.get("kind", "dialogue"),
            }], source="vlm")
            stats["subtitles"] += 1

        # 3. character_appearances → subjects
        seen_chars: set[str] = set()
        for c in record.get("character_appearances", []):
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            if not name:
                continue
            seen_chars.add(name)
            db.upsert_subjects(book_id, [{
                "name": name,
                "role": c.get("role", ""),
                "visual_features": c.get("description", ""),
                "first_episode": ep,
                "source": "vlm",
                "vlm_verified": True,
            }], source="vlm")
            stats["subjects"] += 1

        # 3b. 从 visual_events.characters 补充没有 character_appearances 的窗口
        for event in record.get("visual_events", []):
            if not isinstance(event, dict):
                continue
            for char_name in event.get("characters", []):
                if char_name and char_name not in seen_chars:
                    seen_chars.add(char_name)
                    db.upsert_subjects(book_id, [{
                        "name": char_name,
                        "first_episode": ep,
                        "source": "vlm",
                    }], source="vlm")
                    stats["subjects"] += 1

        # 4. scene_locations → scenes
        for scene in record.get("scene_locations", []):
            if not isinstance(scene, dict):
                continue
            scene_id = f"{window_id}-s{stats['scenes'] + 1:03d}"
            db.upsert_scenes(book_id, [{
                "scene_id": scene_id,
                "episode_id": ep,
                "location": scene.get("name", ""),
                "time_of_day": scene.get("time_of_day", ""),
                "raw_description": scene.get("description", ""),
                "start_time": scene.get("start"),
                "end_time": scene.get("end"),
                "source": "vlm",
                "detected_in_video": True,
                "vlm_verified": True,
            }])
            stats["scenes"] += 1

        # 5. story_beats 收集（循环后按 episode 原子替换）
        for beat in record.get("story_beats", []):
            if not isinstance(beat, dict):
                continue
            episode_beats.setdefault(ep, []).append({
                "window_id": window_id,
                "start_time": beat.get("start", 0),
                "end_time": beat.get("end", 0),
                "function": beat.get("function", ""),
                "summary": beat.get("summary", ""),
                "characters": beat.get("characters", []),
                "cause": beat.get("cause", ""),
                "effect": beat.get("effect", ""),
                "open_question": beat.get("open_question", ""),
                "source": "vlm",
            })
            stats["story_beats"] += 1

        # 6. window_summary → episodes.summary（按 episode 聚合）
        ws = record.get("window_summary", "")
        if ws:
            episode_summaries.setdefault(ep, []).append(ws)

    # ── 循环结束，批量刷盘 ──

    # 7. 按 episode 原子替换 vlm shots（DELETE + INSERT 同一事务，重跑幂等）
    for ep, shots in episode_vlm_shots.items():
        try:
            db.replace_shots(book_id, ep, shots, sources=["vlm"])
        except Exception as exc:
            logger.warning(
                "replace_shots 失败 [book=%s, ep=%s, n=%d]: %s",
                book_id,
                ep,
                len(shots),
                exc,
            )

    # 8. 按 episode 原子替换 story_beats
    for ep, beats in episode_beats.items():
        try:
            db.replace_story_beats(book_id, ep, beats, source="vlm")
        except Exception as exc:
            logger.warning(
                "replace_story_beats 失败 [book=%s, ep=%s, n=%d]: %s",
                book_id,
                ep,
                len(beats),
                exc,
            )

    # 9. 写入 episodes.summary
    for ep, summaries in episode_summaries.items():
        try:
            db.upsert_episodes(book_id, [{
                "episode_id": ep,
                "summary": "\n".join(summaries),
            }], source="vlm")
        except Exception as exc:
            logger.warning(
                "upsert_episodes summary 失败 [book=%s, ep=%s]: %s",
                book_id,
                ep,
                exc,
            )

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
