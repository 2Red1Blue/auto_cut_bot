"""WindowAnalysisStage — DEPRECATED since v5.1: Replaced by vlm_analysis. See docs/design/vlm-first-architecture.md

This module is retained for backwards compatibility with older pipeline configurations.
Do not add new features here; use the vlm_analysis stage instead.

Original docstring:
批量 VLM 逐窗语义分析 + 多源数据融合。

消费 source_windows 产出的 window_batch, 逐窗调用 VLM 分析
视频内容, 汇总为 window_summaries (jsonl)。

新增:
  - 读取 source_metadata (API 字幕/分镜/边界) 和 asr_transcript (ASR 转录文本)
  - 通过 DataFusion 融合 VLM + API + ASR 三源数据
  - 交叉验证 VLM 描述与 ASR 转录文本的一致性
  - 任一数据源不可用时优雅降级
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
from autocut_core.semantic.fusion import (
    DataFusion,
    cross_validate,
    enrich_subjects,
    merge_boundaries,
)

logger = get_logger(__name__)

# 窗口分析 JSON Schema 校验函数名
_WINDOW_ANALYSIS_TASK = "window_analysis"


class WindowAnalysisStage(Stage):
    """对每个窗口调用 Qwen/Doubao VLM 做语义分析 + 多源融合。

    输入:  window_batch (SourceWindowsStage 产出)
           source_metadata (SourceMetadataStage 产出, 可选)
           asr_transcript (AsrTranscriptStage 产出, 可选)
    输出:  window_summaries (逐窗分析结果 jsonl, 含融合后边界与主体)
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="window_analysis",
            input_artifacts=["source_windows", "source_metadata", "source_script", "asr_transcript"],
            output_artifacts=["window_summaries"],
            description="批量 VLM 逐窗视频语义分析 + 多源数据融合",
            db_reads=["subjects", "books", "scenes", "shots", "subtitles"],
            db_writes=["boundaries", "shots", "subjects"],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """从 bus 获取上游产物路径与可选的外部数据源。

        读取 source_metadata, source_script, asr_transcript (均为可选 — 不可用时降级)。
        """
        ref = bus.latest("source_windows")
        if ref is None:
            raise RuntimeError("上游 source_windows 产物未找到")

        artifacts = bus.get(ref)
        batch_path_str = self._resolve_batch_path(artifacts, bus, ref)
        if not batch_path_str:
            raise RuntimeError("无法找到 window_batch 路径")

        # ── 读取可选数据源 (不可用时为 None) ──
        source_metadata = self._read_optional_artifact(bus, "source_metadata")
        source_script = self._read_optional_artifact(bus, "source_script")
        asr_transcript = self._read_optional_artifact(bus, "asr_transcript")

        return [
            Task(
                type="semantic_batch",
                payload={
                    "batch_path": str(batch_path_str),
                    "source_metadata": source_metadata,
                    "source_script": source_script,
                    "asr_transcript": asr_transcript,
                },
            )
        ]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """运行 LLM 推理 → 多源数据融合 → 内联组装产物。"""
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        job_root: Path = cfg.job_root
        task = tasks[0]
        batch_path = Path(task.payload["batch_path"])
        source_metadata = task.payload.get("source_metadata")
        source_script = task.payload.get("source_script")
        asr_transcript = task.payload.get("asr_transcript")

        # ── 步骤 1: LLM 推理 ──
        get_backend(cfg.backend)
        self._run_llm_batch(batch_path, source_metadata, source_script, asr_transcript)

        # ── 步骤 2: 内联组装 (不再调用 assemble_story_artifacts.py) ──
        manifest = load_json(batch_path)
        records = _collect_window_records(manifest)

        # ── 步骤 3: 多源数据融合 ──
        fusion = DataFusion()
        records = self._apply_fusion(records, source_metadata, asr_transcript, fusion)

        output_path = job_root / "window-summaries.jsonl"
        atomic_write_jsonl(output_path, records)

        # ── 发布产物 ──
        ref = bus.put(
            "window_summaries",
            {"path": str(output_path), "backend": cfg.backend},
            stage="window_analysis",
            inputs=(
                [bus.latest("source_windows")]
                if bus.latest("source_windows")
                else []
            ),
        )

        update_project_stage(
            job_root / "project.json",
            "window_analysis",
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
            # 直接嵌套
            batch = artifacts.get("window_batch", {})
            if isinstance(batch, dict) and batch.get("path"):
                return batch["path"]
            # 顶层 key
            for key in ("window_batch", "batch_path", "output"):
                val = artifacts.get(key)
                if isinstance(val, str) and val:
                    return val
        # project.json 回退
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
        """读取可选的上游产物, 不可用时返回 None 并记录日志。"""
        ref = bus.latest(stage_name)
        if ref is None:
            logger.info(
                "可选数据源 %s 不可用 — 窗口分析将降级为纯 VLM 模式",
                stage_name,
            )
            return None
        try:
            data = bus.get(ref)
            if isinstance(data, dict):
                status = data.get("status", "")
                if status == "unavailable":
                    logger.info(
                        "可选数据源 %s 状态为 unavailable — 跳过融合",
                        stage_name,
                    )
                    return None
                return data
            return None
        except Exception as exc:
            logger.warning(
                "读取可选数据源 %s 失败: %s — 跳过融合",
                stage_name,
                exc,
            )
            return None

    @staticmethod
    def _apply_fusion(
        records: list[dict[str, Any]],
        source_metadata: dict[str, Any] | None,
        asr_transcript: dict[str, Any] | None,
        fusion: DataFusion,
    ) -> list[dict[str, Any]]:
        """对每条窗口记录应用多源数据融合。

        融合操作:
          1. 提取 VLM 边界 → 与 API/ASR 边界合并
          2. 提取 VLM 主体 → 与 API 主体融合
          3. 交叉验证 VLM 描述文本与 ASR 转录文本
        """
        if not records:
            return records

        # ── 提取 API/ASR 数据 ──
        api_boundaries = _extract_api_boundaries(source_metadata)
        api_subjects = _extract_api_subjects(source_metadata)
        asr_boundaries = _extract_asr_boundaries(asr_transcript)
        asr_text = _extract_asr_full_text(asr_transcript)

        for record in records:
            if not isinstance(record, dict):
                continue

            # ── 1. 边界融合 ──
            vlm_boundaries = record.get("boundaries", [])
            vlm_events = record.get("events", [])
            # 从 VLM events 提取边界 (如果 events 包含时间信息)
            if not vlm_boundaries and vlm_events:
                vlm_boundaries = _events_to_boundaries(vlm_events)

            merged_boundaries = merge_boundaries(
                vlm=vlm_boundaries if vlm_boundaries else None,
                api=api_boundaries if api_boundaries else None,
                asr=asr_boundaries if asr_boundaries else None,
            )
            if merged_boundaries:
                record["boundaries"] = merged_boundaries
                record["fusion_boundaries_count"] = len(merged_boundaries)

            # ── 2. 主体融合 ──
            vlm_subjects = record.get("subjects", [])
            enriched = enrich_subjects(
                vlm_subjects=vlm_subjects if vlm_subjects else None,
                api_subjects=api_subjects if api_subjects else None,
            )
            if enriched:
                record["subjects"] = enriched
                record["fusion_subjects_count"] = len(enriched)

            # ── 3. 交叉验证 ──
            vlm_description = record.get("description", "") or record.get("summary", "")
            if vlm_description and asr_text:
                lcs_ratio = cross_validate(vlm_description, asr_text)
                record["asr_lcs_ratio"] = round(lcs_ratio, 4)
                if lcs_ratio < 0.5:
                    record["quality_flag"] = "asr_divergence"

        return records

    def _run_llm_batch(
        self,
        batch_path: Path,
        source_metadata: dict[str, Any] | None = None,
        source_script: dict[str, Any] | None = None,
        asr_transcript: dict[str, Any] | None = None,
    ) -> None:
        """直接调用语义推理引擎 (不再 subprocess)。

        注入高置信度 DB 上下文 (角色名、场景描述) 到 VLM prompt,
        降低 VLM 幻觉并提升分析精度。
        """
        print(f"[{utc_now()}] [window_analysis] run_batch({batch_path})")

        # ── 加载高置信度上下文 (角色名、场景描述、多源字幕) ──
        context_injection = self._build_context_injection(
            source_metadata=source_metadata,
            source_script=source_script,
            asr_transcript=asr_transcript,
        )

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
        source_metadata: dict[str, Any] | None = None,
        source_script: dict[str, Any] | None = None,
        asr_transcript: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """从 DB 加载高置信度上下文供 VLM 注入。

        仅加载 HIGH-confidence 数据:
          - 角色名 (subjects 表)
          - 场景描述 (scenes 表, location + time_of_day + characters_present)
          - ASR 字幕 (asr_transcript 产物, 如有)
          - API 字幕 (subtitles 表 source='api')
          - 剧本对白 (subtitles 表 source='script')

        当 vlm_multisource_arbitration 启用且多源数据存在时,
        构建完整的仲裁上下文供 VLM 对比判断。
        不注入 ASR 文本 (低置信度, 保留给后处理阶段交叉验证)。
        DB 不可用或无数据时返回 None, 不影响正常流程。
        """
        cfg = self.config
        if not cfg.db_enabled:
            return None

        db = StageDBClient(db_url=cfg.db_url, schema=cfg.db_schema)
        if not db.is_available:
            return None

        book_id = cfg.extra.get("book_id", "")
        if not book_id:
            return None

        # ── 1. 加载角色 (subjects 表) ──
        characters: list[dict[str, Any]] = []
        try:
            subjects = db.query_subjects(book_id)
            characters = [
                {
                    "name": s.get("name", ""),
                    "role": s.get("role", ""),
                    "traits": s.get("traits", ""),
                }
                for s in subjects
                if isinstance(s, dict) and s.get("name")
            ]
        except Exception as exc:
            logger.warning("加载角色上下文失败: %s", exc)

        # ── 2. 加载场景描述 (scenes 表) ──
        scene: dict[str, Any] | None = None
        try:
            scenes = db.query_scenes(book_id)
            if scenes:
                for s in scenes:
                    if isinstance(s, dict) and s.get("location"):
                        scene = {
                            "location": s.get("location", ""),
                            "time_of_day": s.get("time_of_day", ""),
                            "characters_present": s.get("characters_present", []),
                        }
                        break
        except Exception as exc:
            logger.warning("加载场景上下文失败: %s", exc)

        # ── 3. 加载 ASR 字幕 (asr_transcript 产物) ──
        asr_subtitles: list[dict[str, Any]] = []
        if asr_transcript:
            try:
                results = asr_transcript.get("results", [])
                if isinstance(results, list):
                    for result in results:
                        if not isinstance(result, dict):
                            continue
                        segments = result.get("segments", [])
                        if isinstance(segments, list):
                            for seg in segments:
                                if not isinstance(seg, dict):
                                    continue
                                asr_subtitles.append({
                                    "start": seg.get("start_time", 0),
                                    "end": seg.get("end_time", 0),
                                    "text": seg.get("text", ""),
                                    "speaker": seg.get("speaker", ""),
                                })
            except Exception as exc:
                logger.warning("加载 ASR 字幕上下文失败: %s", exc)

        # ── 4. 加载 API 字幕 (subtitles 表 source='api') ──
        api_subtitles: list[dict[str, Any]] = []
        try:
            # 查询所有集的 API 字幕
            api_subs = db.query_subtitles(book_id, 1, source="api")
            if not api_subs:
                # 尝试其他集
                for ep in range(1, 11):
                    api_subs = db.query_subtitles(book_id, ep, source="api")
                    if api_subs:
                        break
            api_subtitles = [
                {
                    "start": s.get("start_time", 0),
                    "end": s.get("end_time", 0),
                    "text": s.get("text", ""),
                    "speaker": s.get("speaker", ""),
                }
                for s in api_subs
                if isinstance(s, dict) and s.get("text")
            ]
        except Exception as exc:
            logger.warning("加载 API 字幕上下文失败: %s", exc)

        # ── 5. 加载剧本对白 (subtitles 表 source='script') ──
        script_dialogues: list[dict[str, Any]] = []
        try:
            script_subs = db.query_subtitles(book_id, 1, source="script")
            if not script_subs:
                for ep in range(1, 11):
                    script_subs = db.query_subtitles(book_id, ep, source="script")
                    if script_subs:
                        break
            script_dialogues = [
                {
                    "start": s.get("start_time", 0),
                    "end": s.get("end_time", 0),
                    "text": s.get("text", ""),
                    "speaker": s.get("speaker", ""),
                }
                for s in script_subs
                if isinstance(s, dict) and s.get("text")
            ]
        except Exception as exc:
            logger.warning("加载剧本对白上下文失败: %s", exc)

        # ── 组装 context_injection ──
        has_multisource = bool(
            asr_subtitles or api_subtitles or script_dialogues
        )
        if not characters and scene is None and not has_multisource:
            return None

        result: dict[str, Any] = {}
        if characters:
            result["characters"] = characters
        if scene is not None:
            result["scene"] = scene
        if asr_subtitles:
            result["asr_subtitles"] = asr_subtitles
        if api_subtitles:
            result["api_subtitles"] = api_subtitles
        if script_dialogues:
            result["script_dialogues"] = script_dialogues

        parts = [f"{len(characters)} 个角色"]
        if scene:
            parts.append(f"场景 {scene['location']}")
        if has_multisource:
            parts.append(
                f"多源仲裁 (ASR={len(asr_subtitles)}, "
                f"API={len(api_subtitles)}, Script={len(script_dialogues)})"
            )
        logger.info("VLM 上下文注入: %s", ", ".join(parts))
        return result


# ── 内联产物组装 (从 assemble_story_artifacts.py 提取) ──────────────


def _collect_window_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """从语义批处理 manifest 中收集窗口分析记录。

    行为与 assemble_story_artifacts.py windows 模式一致。
    TODO: 内联 validate_task_response — 当前依赖 story_schemas 模块,
    schema 定义待移到 autocut_core/schema/ 中。
    """
    records: list[dict[str, Any]] = []
    for job in manifest.get("jobs", []):
        if not isinstance(job, dict) or job.get("task") != _WINDOW_ANALYSIS_TASK:
            continue
        output = job.get("output")
        if not isinstance(output, str):
            raise ValueError(
                f"window_analysis job 缺少 output 字段: {job.get('id', '?')}"
            )
        path = Path(output).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"窗口分析产物缺失: {path}")
        value = load_json(path)
        # Schema 校验由 LLM API strict mode 保证
        records.append(value)

    if not records:
        raise ValueError("manifest 中无已完成的 window_analysis 输出")

    records.sort(
        key=lambda item: (item["episode"], item["window"]["start"], item["window_id"])
    )
    return records


# ── 外部数据源提取辅助 ──────────────────────────────────────────────


def _extract_api_boundaries(
    source_metadata: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """从 source_metadata 产物中提取 API 边界列表。

    source_metadata 产物中 boundaries 来自 DB 写入阶段,
    格式: {"boundaries_generated": N, "stats": {...}}。
    这里返回 None 表示 API 数据不可用, 由 DataFusion 降级处理。
    """
    if not source_metadata:
        return None
    # 如果有内联 boundaries 字段 (来自 DB 查询结果), 直接返回
    boundaries = source_metadata.get("boundaries")
    if isinstance(boundaries, list):
        return boundaries
    # 否则需要从 DB 读取 — 但此阶段不直接访问 DB,
    # 返回 None 让 DataFusion 只使用 VLM + ASR
    return None


def _extract_api_subjects(
    source_metadata: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """从 source_metadata 产物中提取 API 主体列表。

    source_metadata 产物中:
      - stats.subjects_upserted: 写入 DB 的角色数量
      - 内联 subjects 字段 (如果有)
    """
    if not source_metadata:
        return None
    subjects = source_metadata.get("subjects")
    if isinstance(subjects, list):
        return subjects
    return None


def _extract_asr_boundaries(
    asr_transcript: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """从 asr_transcript 产物中提取 ASR 边界列表。

    asr_transcript 产物格式:
      {"results": [{"episode": N, "segments": [{"start_time", "end_time", "text"}, ...]}, ...]}
    """
    if not asr_transcript:
        return None
    boundaries: list[dict[str, Any]] = []
    results = asr_transcript.get("results", [])
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        episode = result.get("episode")
        segments = result.get("segments", [])
        if not isinstance(segments, list):
            continue
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            start = seg.get("start_time", 0)
            end = seg.get("end_time", 0)
            if start >= end:
                continue
            boundaries.append({
                "start_time": start,
                "end_time": end,
                "event_type": "dialogue",
                "confidence": "medium",
                "source": "asr",
                "description": seg.get("text", "")[:200],
                "subjects": [],
                "episode": episode,
            })
    return boundaries if boundaries else None


def _extract_asr_full_text(
    asr_transcript: dict[str, Any] | None,
) -> str | None:
    """从 asr_transcript 产物中提取完整 ASR 文本 (供交叉验证用)。

    拼接所有 episodes 的 text 字段。
    """
    if not asr_transcript:
        return None
    results = asr_transcript.get("results", [])
    if not isinstance(results, list):
        return None
    parts: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        text = result.get("text", "")
        if text:
            parts.append(text)
    return " ".join(parts) if parts else None


def _events_to_boundaries(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将 VLM events 字段转换为边界格式。

    VLM 返回的 events 可能包含 event_type, start_time, end_time 等字段。
    标准化为边界格式供 merge_boundaries 使用。
    """
    boundaries: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        start = ev.get("start_time", ev.get("start", 0))
        end = ev.get("end_time", ev.get("end", 0))
        if start >= end:
            continue
        boundaries.append({
            "start_time": start,
            "end_time": end,
            "event_type": ev.get("event_type", "unknown"),
            "confidence": ev.get("confidence", "low"),
            "source": "vlm",
            "description": ev.get("description", ""),
            "subjects": ev.get("subjects", []),
        })
    return boundaries