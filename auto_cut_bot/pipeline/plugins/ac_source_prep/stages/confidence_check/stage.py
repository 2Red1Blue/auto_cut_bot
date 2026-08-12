"""ConfidenceCheckStage — VLM 输出质量门控 + 按需触发补充数据源。

消费 window_summaries (VLM 逐窗分析结果), 对每个窗口执行:
  1. 对白置信度统计 (high/medium/low)
  2. 硬字幕检测 (source_accuracy.agreement)
  3. 边界连续性检查 (相邻窗口)
  4. 角色命名一致性检查
  5. 写入 vlm_confidence_log 表
  6. 低置信度时设置 enrichment_triggered=true 并建议补充动作

根据 should_trigger_asr() 判断是否需要 ASR 补充:
  - 无硬字幕 (agreement=no_subtitle) → 触发 ASR
  - 低置信对白比例 > 20% → 触发 ASR

非阻塞设计: 低置信度时记录警告但不中断流水线, 由 Agent 决定是否重新运行。
"""

from __future__ import annotations

from collections import Counter
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
    atomic_write_json,
    load_jsonl,
    update_project_stage,
    utc_now,
)

logger = get_logger(__name__)

# ── 置信度阈值 ──────────────────────────────────────────────────────────
LOW_CONF_RATIO_THRESHOLD = 0.2  # 低置信对白比例超过此值触发 ASR


def _has_hard_subtitles(dialogue: list[dict]) -> bool:
    """Check if any dialogue entry has hard subtitle evidence.

    Returns True when any entry's source_accuracy.agreement is
    "subtitle_match" or "subtitle_divergence".
    """
    return any(
        isinstance(d.get("source_accuracy"), dict)
        and d["source_accuracy"].get("agreement") in ("subtitle_match", "subtitle_divergence")
        for d in dialogue
    )


def _calc_low_conf_ratio(dialogue: list[dict]) -> float:
    """Calculate the ratio of low-confidence dialogue entries.

    Uses the same Counter-based calculation as _assess_window().
    """
    conf_counter = Counter(
        d.get("confidence", "unknown") for d in dialogue
    )
    total = len(dialogue)
    low_conf = conf_counter.get("low", 0)
    return low_conf / max(total, 1)


class ConfidenceCheckStage(Stage):
    """VLM 输出质量门控 Stage。

    输入:  window_summaries (WindowAnalysisStage 产出)
    输出:  confidence_report (逐窗评估 + 全局摘要)
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="confidence_check",
            input_artifacts=["window_summaries"],
            output_artifacts=["confidence_report"],
            description="VLM 输出质量门控 — 对白置信度统计 + 硬字幕检测 + 边界连续性 + 角色命名一致性",
            db_reads=[],
            db_writes=["vlm_confidence_log"],
        )

    # ── prepare ─────────────────────────────────────────────────────────

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """从 bus 读取 window_summaries 产物, 为每个窗口生成一个 Task。"""
        ref = bus.latest("window_summaries")
        if ref is None:
            raise RuntimeError("上游 window_summaries 产物未找到")

        data = bus.get(ref)
        path_str = self._resolve_path(data, ref)
        if not path_str:
            raise RuntimeError("无法解析 window_summaries 路径")

        summaries_path = Path(path_str)
        if not summaries_path.is_file():
            raise FileNotFoundError(f"window_summaries 文件不存在: {summaries_path}")

        records = load_jsonl(summaries_path)
        if not records:
            raise RuntimeError("window_summaries 为空 — 无窗口可分析")

        logger.info(
            "confidence_check: 读取到 %d 条窗口记录",
            len(records),
        )

        # 每条窗口记录作为一个 Task
        return [
            Task(
                type="confidence_check",
                payload={
                    "window_id": self._window_key(rec),
                    "record": rec,
                },
            )
            for rec in records
        ]

    # ── execute ─────────────────────────────────────────────────────────

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """对每个窗口执行置信度检查, 生成 confidence_report。"""
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        job_root: Path = cfg.job_root

        if not tasks:
            raise RuntimeError("无窗口任务可执行")

        # ── 提取所有窗口记录 (排序后便于相邻窗口比较) ──
        records = [t.payload["record"] for t in tasks]
        records.sort(key=lambda r: (r.get("episode", 0), r.get("window", {}).get("start", 0)))

        # ── 逐窗分析 ──
        window_assessments: list[dict[str, Any]] = []
        global_counts: dict[str, int] = Counter()
        triggered_count = 0

        db = StageDBClient(db_url=cfg.db_url, schema=cfg.db_schema) if cfg.db_enabled else None

        for i, rec in enumerate(records):
            prev_rec = records[i - 1] if i > 0 else None
            next_rec = records[i + 1] if i < len(records) - 1 else None

            assessment = self._assess_window(rec, prev_rec, next_rec)
            window_assessments.append(assessment)

            # 全局计数
            global_counts["total_windows"] += 1
            global_counts[f"confidence_{assessment['dominant_confidence']}"] += 1
            if assessment["enrichment_triggered"]:
                triggered_count += 1
                global_counts["enrichment_triggered"] += 1

            # 写入 DB
            if db is not None:
                dialogue_stats = assessment.get("dialogue_stats", {})
                chars = assessment.get("characters_seen", [])
                char_names = [c.get("name", "") for c in chars if isinstance(c, dict)]
                db.write_confidence_log(
                    book_id=self.config.extra.get("book_id", ""),
                    window_id=assessment["window_id"],
                    total_dialogue=dialogue_stats.get("total", 0),
                    high_conf=dialogue_stats.get("high", 0),
                    low_conf=dialogue_stats.get("low", 0),
                    characters_seen=char_names,
                    has_hard_subtitles=assessment.get("has_hard_subtitles", False),
                    enrichment_triggered=assessment.get("enrichment_triggered", False),
                )

        # ── 全局摘要 ──
        global_summary = self._build_global_summary(
            window_assessments, global_counts, triggered_count
        )

        # ── 组装报告 ──
        report = {
            "generated_at": utc_now(),
            "global_summary": global_summary,
            "window_assessments": window_assessments,
        }

        output_path = job_root / "confidence-report.json"
        atomic_write_json(output_path, report)

        # ── 发布产物 ──
        ref = bus.put(
            "confidence_report",
            {"path": str(output_path)},
            stage="confidence_check",
            inputs=(
                [bus.latest("window_summaries")]
                if bus.latest("window_summaries")
                else []
            ),
        )

        update_project_stage(
            job_root / "project.json",
            "confidence_check",
            "completed",
            outputs={"confidence_report": ref.sha256},
        )

        logger.info(
            "confidence_check 完成: %d 窗口, %d 触发补充",
            global_counts["total_windows"],
            triggered_count,
        )

        return [ref]

    # ── 窗口评估 ────────────────────────────────────────────────────────

    @staticmethod
    def _assess_dialogue_stats(dialogue: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute dialogue confidence statistics for a window.

        Args:
            dialogue: List of dialogue entries from a window record.

        Returns:
            Dict with keys: total, high, medium, low, unknown, low_conf_ratio,
            dominant_confidence.
        """
        conf_counter = Counter(
            d.get("confidence", "unknown") for d in dialogue
        )
        total = len(dialogue)
        high = conf_counter.get("high", 0)
        medium = conf_counter.get("medium", 0)
        low = conf_counter.get("low", 0)
        unknown = conf_counter.get("unknown", 0)
        low_ratio = low / max(total, 1)

        dominant = "high"
        if low > high:
            dominant = "low"
        elif medium > high:
            dominant = "medium"

        return {
            "total": total,
            "high": high,
            "medium": medium,
            "low": low,
            "unknown": unknown,
            "low_conf_ratio": round(low_ratio, 4),
            "dominant_confidence": dominant,
        }

    @staticmethod
    def _assess_hard_subtitles(dialogue: list[dict[str, Any]]) -> bool:
        """Detect whether any dialogue entry has hard subtitles.

        Returns True if any dialogue entry's source_accuracy.agreement is
        "subtitle_match" or "subtitle_divergence".
        """
        return any(
            isinstance(d.get("source_accuracy"), dict)
            and d["source_accuracy"].get("agreement") in ("subtitle_match", "subtitle_divergence")
            for d in dialogue
        )

    def _assess_boundary_continuity(
        self,
        rec: dict[str, Any],
        prev_rec: dict[str, Any] | None,
        next_rec: dict[str, Any] | None,
    ) -> list[str]:
        """Check boundary continuity for a window against its neighbors."""
        return self._check_boundary_continuity(rec, prev_rec, next_rec)

    def _assess_character_consistency(
        self,
        rec: dict[str, Any],
        prev_rec: dict[str, Any] | None,
    ) -> list[str]:
        """Check character naming consistency against the previous window."""
        return self._check_character_consistency(rec, prev_rec)

    @staticmethod
    def _extract_character_names(rec: dict[str, Any]) -> list[str]:
        """Extract character names from a window record.

        Returns a list of unique character name strings.
        """
        characters = ConfidenceCheckStage._extract_characters(rec)
        return [c["name"] for c in characters if isinstance(c, dict) and c.get("name")]

    def _assess_window(
        self,
        rec: dict[str, Any],
        prev_rec: dict[str, Any] | None,
        next_rec: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """对单个窗口做完整的置信度评估。"""
        window_id = self._window_key(rec)
        dialogue = rec.get("dialogue_and_text", [])
        if not isinstance(dialogue, list):
            dialogue = []

        # ── 1. 对白置信度统计 ──
        stats = self._assess_dialogue_stats(dialogue)

        # ── 2. 硬字幕检测 ──
        has_hard_subtitles = self._assess_hard_subtitles(dialogue)

        # ── 3. 边界连续性 (与相邻窗口) ──
        boundary_issues = self._assess_boundary_continuity(rec, prev_rec, next_rec)

        # ── 4. 角色命名一致性 ──
        character_issues = self._assess_character_consistency(rec, prev_rec)

        # ── 5. 提取角色列表 ──
        characters_seen = self._extract_characters(rec)

        low_conf_ratio = stats["low_conf_ratio"]

        # ── 6. 判断是否需要触发补充 ──
        enrichment_triggered = self._should_enrich(
            has_hard_subtitles=has_hard_subtitles,
            low_conf_ratio=low_conf_ratio,
            boundary_issues=boundary_issues,
            character_issues=character_issues,
        )

        suggested_actions: list[str] = []
        if enrichment_triggered:
            suggested_actions = self._suggest_actions(
                has_hard_subtitles=has_hard_subtitles,
                low_conf_ratio=low_conf_ratio,
                boundary_issues=boundary_issues,
                character_issues=character_issues,
            )

        # ── 日志 ──
        if low_conf_ratio > LOW_CONF_RATIO_THRESHOLD:
            logger.warning(
                "窗口 %s 低置信对白比例 %.0f%% (high=%d medium=%d low=%d)",
                window_id,
                low_conf_ratio * 100,
                stats["high"],
                stats["medium"],
                stats["low"],
            )
        if not has_hard_subtitles:
            logger.info("窗口 %s 无硬字幕 — 建议启用 ASR", window_id)
        if boundary_issues:
            logger.warning(
                "窗口 %s 边界连续性异常: %s",
                window_id,
                "; ".join(boundary_issues),
            )
        if character_issues:
            logger.warning(
                "窗口 %s 角色命名不一致: %s",
                window_id,
                "; ".join(character_issues),
            )

        return {
            "window_id": window_id,
            "episode": rec.get("episode"),
            "window_start": rec.get("window", {}).get("start"),
            "window_end": rec.get("window", {}).get("end"),
            "dialogue_stats": {
                "total": stats["total"],
                "high": stats["high"],
                "medium": stats["medium"],
                "low": stats["low"],
                "unknown": stats["unknown"],
                "low_conf_ratio": stats["low_conf_ratio"],
            },
            "dominant_confidence": stats["dominant_confidence"],
            "has_hard_subtitles": has_hard_subtitles,
            "characters_seen": characters_seen,
            "boundary_issues": boundary_issues,
            "character_issues": character_issues,
            "enrichment_triggered": enrichment_triggered,
            "suggested_actions": suggested_actions,
        }

    # ── 边界连续性检查 ──────────────────────────────────────────────────

    @staticmethod
    def _check_boundary_continuity(
        rec: dict[str, Any],
        prev_rec: dict[str, Any] | None,
        next_rec: dict[str, Any] | None,
    ) -> list[str]:
        """检查当前窗口与相邻窗口的边界连续性。

        检查项:
          - ends_mid_scene / starts_mid_scene 匹配
          - timeline_segments.mode 切换是否有 entry/exit_signal
        """
        issues: list[str] = []

        # 与前一个窗口的连续性
        if prev_rec is not None:
            prev_ends_mid = prev_rec.get("boundary_context", {}).get("ends_mid_scene", False)
            cur_starts_mid = rec.get("boundary_context", {}).get("starts_mid_scene", False)
            if prev_ends_mid != cur_starts_mid:
                issues.append(
                    f"prev.ends_mid_scene={prev_ends_mid} != cur.starts_mid_scene={cur_starts_mid}"
                )

        # 与后一个窗口的连续性
        if next_rec is not None:
            cur_ends_mid = rec.get("boundary_context", {}).get("ends_mid_scene", False)
            next_starts_mid = next_rec.get("boundary_context", {}).get("starts_mid_scene", False)
            if cur_ends_mid != next_starts_mid:
                issues.append(
                    f"cur.ends_mid_scene={cur_ends_mid} != next.starts_mid_scene={next_starts_mid}"
                )

        # Timeline mode 切换检查
        timeline = rec.get("timeline_segments", [])
        if isinstance(timeline, list):
            for j, seg in enumerate(timeline):
                if not isinstance(seg, dict):
                    continue
                mode = seg.get("mode", "")
                if j > 0:
                    prev_mode = timeline[j - 1].get("mode", "") if isinstance(timeline[j - 1], dict) else ""
                    if mode != prev_mode:
                        has_entry = bool(seg.get("entry_signal"))
                        has_exit = bool(
                            timeline[j - 1].get("exit_signal")
                            if isinstance(timeline[j - 1], dict)
                            else False
                        )
                        if not has_entry and not has_exit:
                            issues.append(
                                f"timeline mode switch {prev_mode}→{mode} "
                                f"missing entry/exit_signal"
                            )

        return issues

    # ── 角色命名一致性检查 ──────────────────────────────────────────────

    @staticmethod
    def _check_character_consistency(
        rec: dict[str, Any],
        prev_rec: dict[str, Any] | None,
    ) -> list[str]:
        """Check character naming consistency against the previous window.

        Detects:
          - Characters that appear in the previous window but not the current one
          - Characters that appear in the current window but not the previous one
          - Same-count swaps that may indicate renaming/aliasing
        """
        issues: list[str] = []
        cur_chars = ConfidenceCheckStage._extract_characters(rec)

        if prev_rec is not None:
            prev_chars = ConfidenceCheckStage._extract_characters(prev_rec)
            cur_set = set(c["name"] for c in cur_chars if isinstance(c, dict))
            prev_set = set(c["name"] for c in prev_chars if isinstance(c, dict))

            if cur_set and prev_set:
                missing = prev_set - cur_set
                new_chars = cur_set - prev_set

                if missing:
                    issues.append(
                        f"characters_missing: {', '.join(sorted(missing))} "
                        f"appeared in previous window but not in current"
                    )
                if new_chars:
                    issues.append(
                        f"characters_new: {', '.join(sorted(new_chars))} "
                        f"appeared in current window but not in previous"
                    )
                if missing and new_chars and len(missing) == len(new_chars):
                    issues.append(
                        f"character_rename_suspected: {', '.join(sorted(missing))} "
                        f"→ {', '.join(sorted(new_chars))} "
                        f"(same count swap, may be aliasing/renaming)"
                    )

        return issues

    @staticmethod
    def _extract_characters(rec: dict[str, Any]) -> list[dict[str, Any]]:
        """从窗口记录中提取角色列表。"""
        characters: list[dict[str, Any]] = []
        subjects = rec.get("subjects", [])
        if isinstance(subjects, list):
            for s in subjects:
                if isinstance(s, dict) and s.get("name"):
                    characters.append({"name": s["name"], "role": s.get("role", "")})

        # 也从 dialogue_and_text 中提取 speaker
        dialogue = rec.get("dialogue_and_text", [])
        if isinstance(dialogue, list):
            seen_names = {c["name"] for c in characters}
            for d in dialogue:
                if not isinstance(d, dict):
                    continue
                speaker = d.get("speaker", "")
                if speaker and speaker not in seen_names and speaker not in ("", "unknown", "旁白"):
                    characters.append({"name": speaker, "role": ""})
                    seen_names.add(speaker)

        return characters

    # ── 补充触发判断 ────────────────────────────────────────────────────

    @staticmethod
    def _should_enrich(
        has_hard_subtitles: bool,
        low_conf_ratio: float,
        boundary_issues: list[str],
        character_issues: list[str],
    ) -> bool:
        """判断是否需要触发补充数据源 (ASR/剧本注入)。

        触发条件 (任一满足):
          1. 无硬字幕
          2. 低置信对白比例 > 20%
          3. 有边界连续性异常
          4. 有角色命名不一致
        """
        if not has_hard_subtitles:
            return True
        if low_conf_ratio > LOW_CONF_RATIO_THRESHOLD:
            return True
        if boundary_issues:
            return True
        if character_issues:
            return True
        return False

    @staticmethod
    def _suggest_actions(
        has_hard_subtitles: bool,
        low_conf_ratio: float,
        boundary_issues: list[str],
        character_issues: list[str],
    ) -> list[str]:
        """根据检测到的信号建议补充动作。"""
        actions: list[str] = []

        if not has_hard_subtitles:
            actions.append("trigger_asr: 无硬字幕, 建议启用 ASR 转录补充对白")
        if low_conf_ratio > LOW_CONF_RATIO_THRESHOLD:
            actions.append(
                f"trigger_asr: 低置信对白比例 {low_conf_ratio:.0%} > "
                f"{LOW_CONF_RATIO_THRESHOLD:.0%}, 建议启用 ASR"
            )
        if boundary_issues:
            actions.append("inject_episode_summary: 边界连续性异常, 建议注入本集摘要")
        if character_issues:
            actions.append("inject_character_reference: 角色命名不一致, 建议注入角色参考表")

        return actions

    # ── should_trigger_asr (design doc section 五) ────────────────────────

    @staticmethod
    def should_trigger_asr(vlm_output: dict[str, Any]) -> bool:
        """判断是否需要 ASR 补充 (设计文档 section 五)。

        逻辑:
          1. 检查是否有硬字幕 (VLM 读到了屏幕文字)
          2. 检查低置信对白比例
          3. 无硬字幕 或 低置信比例 > 20% → 触发 ASR
        """
        dialogue = vlm_output.get("dialogue_and_text", [])
        if not isinstance(dialogue, list):
            dialogue = []

        # 检查是否有硬字幕
        has_hard_subtitles = _has_hard_subtitles(dialogue)

        # 检查低置信对白比例
        low_conf_ratio = _calc_low_conf_ratio(dialogue)

        return not has_hard_subtitles or low_conf_ratio > LOW_CONF_RATIO_THRESHOLD

    # ── 全局摘要 ────────────────────────────────────────────────────────

    def _build_global_summary(
        self,
        assessments: list[dict[str, Any]],
        counts: dict[str, int],
        triggered_count: int,
    ) -> dict[str, Any]:
        """构建全局摘要 — 供 Agent 决策是否重新运行 pipeline。"""
        total = counts.get("total_windows", 0)
        if total == 0:
            return {"status": "empty", "message": "无窗口数据"}

        high_windows = counts.get("confidence_high", 0)
        low_windows = counts.get("confidence_low", 0)

        # 整体质量评分
        if triggered_count == 0:
            overall_status = "pass"
            recommendation = "no_action: 所有窗口置信度正常, 无需补充数据源"
        elif triggered_count <= max(total * 0.3, 1):
            overall_status = "warning"
            recommendation = (
                f"consider_enrichment: {triggered_count}/{total} 窗口触发补充, "
                "Agent 可决定是否重新运行 pipeline 并启用 ASR/剧本注入"
            )
        else:
            overall_status = "action_required"
            recommendation = (
                f"recommend_enrichment: {triggered_count}/{total} 窗口触发补充, "
                "强烈建议启用 ASR 和/或剧本注入后重新运行 vlm_analysis"
            )

        return {
            "status": overall_status,
            "recommendation": recommendation,
            "total_windows": total,
            "high_confidence_windows": high_windows,
            "low_confidence_windows": low_windows,
            "enrichment_triggered_count": triggered_count,
            "asr_recommended": any(
                "trigger_asr" in a.get("suggested_actions", [])
                for a in assessments
            ),
            "any_hard_subtitles_missing": any(
                not a.get("has_hard_subtitles", True) for a in assessments
            ),
            "any_boundary_issues": any(
                a.get("boundary_issues") for a in assessments
            ),
            "any_character_issues": any(
                a.get("character_issues") for a in assessments
            ),
        }

    # ── 工具方法 ─────────────────────────────────────────────────────────

    @staticmethod
    def _window_key(rec: dict[str, Any]) -> str:
        """生成窗口唯一标识。"""
        episode = rec.get("episode", "?")
        window_id = rec.get("window_id", "")
        if window_id:
            return window_id
        win = rec.get("window", {})
        start = win.get("start", 0) if isinstance(win, dict) else 0
        return f"ep{episode}_w{start}"

    @staticmethod
    def _resolve_path(data: Any, ref: Artifact) -> str | None:
        """从多种可能的产物结构中解析文件路径。"""
        if isinstance(data, dict):
            # 直接路径
            path = data.get("path")
            if isinstance(path, str) and path:
                return path
            # 内嵌 summaries
            summaries = data.get("window_summaries", {})
            if isinstance(summaries, dict):
                path = summaries.get("path")
                if isinstance(path, str) and path:
                    return path
        # 从 artifact 自身路径
        return str(ref.path) if ref.path else None