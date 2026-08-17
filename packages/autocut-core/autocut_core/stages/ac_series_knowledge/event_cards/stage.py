"""EventCardsStage — 从窗口分析结果编译中等粒度剧情事件。

设计原理详见 work_ai/ac_auto_cut/原理/event-cards-compilation-design.md
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import (
    atomic_write_json, atomic_write_jsonl, load_json, load_jsonl,
    normalize_text, stable_id, update_project_stage,
)
from autocut_core.logging import get_logger

logger = get_logger(__name__)

# Three-tier ASR snap (optional import, graceful fallback if not available)
try:
    from autocut_core.audio.asr_anchor import (
        AudioAnchorResult,
        three_tier_snap_start,
        three_tier_snap_end,
        result_from_dict as anchor_result_from_dict,
    )
    from autocut_core.semantic.scene_boundary_fusion import extract_cut_points
    _HAS_SNAP = True
except ImportError:
    _HAS_SNAP = False
    AudioAnchorResult = None
    three_tier_snap_start = None
    three_tier_snap_end = None
    anchor_result_from_dict = None
    extract_cut_points = None


class EventCardsStage(Stage):
    """编译 Event Cards 和 Highlight/Hook 候选目录。

    输入:  window_summaries (WindowAnalysisStage 产出)
    输出:  event_cards + highlight_hook_catalog
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="event_cards",
            input_artifacts=["vlm_analysis", "scene_boundaries"],
            output_artifacts=["event_cards", "highlight_hook_catalog"],
            description="编译中等粒度剧情事件 + Highlight/Hook 候选目录（全量三层ASR snap时间戳）。可选依赖: silence_intervals, speech_intervals, asr_anchor_results（存在则用于高精度snap，缺失自动回退到median）",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """从 bus 解析上游 window_summaries 的路径作为编译输入。"""
        ref = bus.latest("vlm_analysis") or bus.latest("window_analysis")
        if ref is None:
            raise RuntimeError("上游 vlm_analysis 产物未找到")
        artifacts = bus.get(ref)
        summaries_path = artifacts.get("path") if isinstance(artifacts, dict) else None
        if not summaries_path:
            summaries_path = str((bus.resolve("vlm_analysis", "window_summaries") or bus.resolve("window_analysis", "window_summaries")).path)  # type: ignore[union-attr]
        return [Task(type="compile_events", payload={
            "window_summaries": str(summaries_path),
        })]

    def _load_snap_data(self, bus: ArtifactBus, root: Path) -> dict[str, Any]:
        """加载三层snap所需的所有数据（场景边界、静音、语音、ASR anchor）。"""
        data = {
            "scene_cut_points_by_source": {},
            "silence_intervals": None,
            "speech_intervals": None,
            "anchor_results": None,
        }
        # 加载场景边界
        scene_ref = bus.latest("scene_boundaries")
        scene_boundaries = None
        if scene_ref is not None:
            scene_boundaries = bus.get(scene_ref)
        elif (root / "scene_boundaries.json").is_file():
            scene_boundaries = load_json(root / "scene_boundaries.json")
        if scene_boundaries and "episodes" in scene_boundaries and _HAS_SNAP:
            for ep_id, ep_scenes in scene_boundaries["episodes"].items():
                cut_points = extract_cut_points(ep_scenes)
                if cut_points:
                    try:
                        ep_num = int(ep_id)
                        source_id = f"source-{ep_num:03d}"
                        data["scene_cut_points_by_source"][source_id] = cut_points
                    except (ValueError, TypeError):
                        pass
        # 加载静音区间
        silence_ref = bus.latest("silence_intervals")
        if silence_ref is not None:
            try:
                data["silence_intervals"] = bus.get(silence_ref)
            except Exception as exc:
                logger.debug("event_cards: 加载silence_intervals失败: %s", exc)
        elif (root / "silence_intervals.json").is_file():
            try:
                data["silence_intervals"] = load_json(root / "silence_intervals.json")
            except Exception as exc:
                logger.debug("event_cards: 从文件加载silence_intervals失败: %s", exc)
        # 加载语音区间
        speech_ref = bus.latest("speech_intervals")
        if speech_ref is not None:
            try:
                data["speech_intervals"] = bus.get(speech_ref)
            except Exception as exc:
                logger.debug("event_cards: 加载speech_intervals失败: %s", exc)
        elif (root / "speech_intervals.json").is_file():
            try:
                data["speech_intervals"] = load_json(root / "speech_intervals.json")
            except Exception as exc:
                logger.debug("event_cards: 从文件加载speech_intervals失败: %s", exc)
        # 加载ASR anchor结果
        anchor_raw = None
        anchor_ref = bus.latest("asr_anchor_results")
        if anchor_ref is not None and _HAS_SNAP:
            try:
                anchor_raw = bus.get(anchor_ref)
            except Exception as exc:
                logger.debug("event_cards: 从bus加载asr_anchor_results失败: %s", exc)
        if anchor_raw is None and (root / "asr_anchor_results.json").is_file() and _HAS_SNAP:
            try:
                anchor_raw = load_json(root / "asr_anchor_results.json")
            except Exception as exc:
                logger.debug("event_cards: 从文件加载asr_anchor_results失败: %s", exc)
        if anchor_raw and anchor_raw.get("episodes") and _HAS_SNAP:
            try:
                anchor_results = {}
                for ep_id, ep_dict in anchor_raw["episodes"].items():
                    try:
                        anchor_results[str(ep_id)] = anchor_result_from_dict(ep_dict)
                    except Exception as exc:
                        logger.debug("event_cards: 反序列化anchor ep=%s失败: %s", ep_id, exc)
                data["anchor_results"] = anchor_results
                logger.info("event_cards: 已加载ASR anchor数据 (%d 集)", len(anchor_results))
            except Exception as exc:
                logger.debug("event_cards: 解析asr_anchor_results失败: %s", exc)
        return data

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """内联编译事件卡和候选目录（全量三层ASR snap时间戳）。"""
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        summaries = tasks[0].payload["window_summaries"]

        # ── 加载窗口数据 ──
        windows = load_jsonl(Path(summaries))
        if not windows:
            raise RuntimeError("window summaries 为空")

        # ── 加载snap所需数据 ──
        snap_data = self._load_snap_data(bus, root)
        fusion_cfg = cfg.extra.get("fusion", {}) if hasattr(cfg, "extra") and isinstance(cfg.extra, dict) else {}
        audio_max_shift = float(fusion_cfg.get("audio_max_shift", 3.0))

        # ── 编译事件（原始median时间戳）──
        events = _compile_events(windows)
        if not events:
            raise RuntimeError("无可用的 story beats 编译 Event Cards")

        # ── 全量snap事件时间戳 ──
        if _HAS_SNAP:
            _snap_all_events(events, snap_data, audio_max_shift=audio_max_shift, fusion_cfg=fusion_cfg)

        # ── 编译候选目录（使用snap后的事件做关联，保证时间一致性）──
        candidates = _compile_candidates(windows, events)

        # ── 落盘 + 发布 ──
        cards_path = root / "event-cards.jsonl"
        catalog_path = root / "highlight-hook-catalog.json"
        atomic_write_jsonl(cards_path, events)
        atomic_write_json(catalog_path, {
            "schema_version": "1.0",
            "immutable": True,
            "candidates": candidates,
        })

        refs = [
            bus.put("event_cards", {"path": str(cards_path)}, stage="event_cards"),
            bus.put("highlight_hook_catalog", {"path": str(catalog_path)}, stage="event_cards"),
        ]



        update_project_stage(
            root / "project.json", "event_cards", "completed",
            outputs={"event_cards": str(cards_path), "catalog": str(catalog_path)},
        )
        return refs


# ── Three-tier snap 辅助函数 ────────────────────────────────────────


def _get_episode_id_from_source(source_id):
    """从source_id提取episode号，例如'source-001' -&gt; '1'。"""
    if not source_id or not isinstance(source_id, str) or not source_id.startswith("source-"):
        return None
    try:
        return str(int(source_id.split("-")[1]))
    except (IndexError, ValueError):
        return None


def _snap_all_events(events, snap_data, *, audio_max_shift=3.0, fusion_cfg=None):
    """对所有event的source_ranges做三层ASR snap精修，原地修改events。

    总耗时 -&gt;10ms（纯本地二分查找，无LLM/重计算）。
    每个事件保留original_start/original_end供回溯。
    """
    if not _HAS_SNAP:
        logger.info("event_cards: ASR snap模块不可用，使用median时间戳")
        return
    fusion_cfg = fusion_cfg or {}
    anchor_results = snap_data.get("anchor_results") or {}
    scene_cut_points = snap_data.get("scene_cut_points_by_source") or {}
    snapped_count = 0
    fallback_count = 0

    for event in events:
        source_id = event.get("source_id")
        ep_id = _get_episode_id_from_source(source_id)
        if ep_id is None:
            event["snap_method"] = "median_fallback"
            continue

        cut_points = scene_cut_points.get(source_id, [])
        anchor = anchor_results.get(ep_id)
        if anchor is None or getattr(anchor, "status", None) != "ready":
            event["snap_method"] = "median_fallback"
            continue

        # Snap each source range
        for rng in event.get("source_ranges", []):
            original_start = rng.get("start")
            original_end = rng.get("end")
            if not isinstance(original_start, (int, float)) or not isinstance(original_end, (int, float)):
                continue

            rng["original_start"] = original_start
            rng["original_end"] = original_end

            # Snap start
            snap_s = three_tier_snap_start(
                float(original_start), cut_points, anchor,
                lead_in_audio=0.15,
                lead_in_visual=0.05,
                visual_lead_window=float(fusion_cfg.get("visual_lead_window", 0.60)),
                visual_follow_window=float(fusion_cfg.get("visual_follow_window", 0.45)),
                search_radius=float(fusion_cfg.get("audio_search_radius", 2.0)),
                max_shift=audio_max_shift,
                micro_crossfade_ms=float(fusion_cfg.get("micro_crossfade_ms", 80)),
            )
            rng["start"] = round(snap_s["final_start"], 3)
            rng["snap_start_decision"] = snap_s["decision"]
            rng["snap_start_source"] = snap_s["anchor_source"]
            if snap_s.get("needs_fade"):
                rng["snap_start_needs_fade"] = True
                rng["snap_start_fade_ms"] = snap_s.get("fade_ms", 80)
            if snap_s.get("audio_onset") is not None:
                rng["audio_onset_start"] = snap_s["audio_onset"]

            # Snap end
            snap_e = three_tier_snap_end(
                float(original_end), cut_points, anchor,
                lead_out_audio=0.10,
                lead_out_visual=0.05,
                visual_tail_window=float(fusion_cfg.get("visual_tail_window_end", 0.60)),
                search_radius=float(fusion_cfg.get("audio_search_radius", 2.0)),
                max_shift=audio_max_shift,
            )
            rng["end"] = round(snap_e["final_end"], 3)
            rng["snap_end_decision"] = snap_e["decision"]
            rng["snap_end_source"] = snap_e["anchor_source"]
            if snap_e.get("needs_fade"):
                rng["snap_end_needs_fade"] = True
                rng["snap_end_fade_ms"] = snap_e.get("fade_ms", 80)
            if snap_e.get("audio_end") is not None:
                rng["audio_onset_end"] = snap_e["audio_end"]

        # Update top-level start/end for backward compatibility
        if event.get("source_ranges"):
            event["start"] = event["source_ranges"][0]["start"]
            event["end"] = event["source_ranges"][0]["end"]
        # Mark snap_method based on actual success
        snapped = any("snap_start_decision" in rng for rng in event.get("source_ranges", []))
        if snapped:
            event["snap_method"] = "three_tier_asr"
            snapped_count += 1
        else:
            event["snap_method"] = "median_fallback"
            fallback_count += 1

    logger.info(
        "event_cards: 全量snap完成 — %d个事件精修成功, %d个回退到median",
        snapped_count, fallback_count,
    )


def _snap_all_candidates(candidates, snap_data, *, audio_max_shift=3.0, fusion_cfg=None):
    """对所有highlight/hook candidate的时间范围做三层ASR snap精修，原地修改candidates。
    和events使用完全相同的snap参数与逻辑，保证时间对齐精度一致。
    """
    if not _HAS_SNAP:
        logger.info("event_cards: ASR snap模块不可用，candidates使用原始VLM时间戳")
        return
    fusion_cfg = fusion_cfg or {}
    anchor_results = snap_data.get("anchor_results") or {}
    scene_cut_points = snap_data.get("scene_cut_points_by_source") or {}
    snapped_count = 0
    fallback_count = 0

    for cand in candidates:
        source_id = cand.get("source_id")
        ep_id = _get_episode_id_from_source(source_id)
        if ep_id is None:
            cand["snap_method"] = "vlm_raw"
            fallback_count += 1
            continue

        cut_points = scene_cut_points.get(source_id, [])
        anchor = anchor_results.get(ep_id)
        if anchor is None or getattr(anchor, "status", None) != "ready":
            cand["snap_method"] = "vlm_raw"
            fallback_count += 1
            continue

        original_start = cand.get("start")
        original_end = cand.get("end")
        if not isinstance(original_start, (int, float)) or not isinstance(original_end, (int, float)):
            cand["snap_method"] = "vlm_raw"
            fallback_count += 1
            continue

        # 保存原始时间
        cand["original_start"] = round(float(original_start), 3)
        cand["original_end"] = round(float(original_end), 3)

        # Snap start (和events用完全相同的参数)
        snap_s = three_tier_snap_start(
            float(original_start), cut_points, anchor,
            lead_in_audio=0.15,
            lead_in_visual=0.05,
            visual_lead_window=float(fusion_cfg.get("visual_lead_window", 0.60)),
            visual_follow_window=float(fusion_cfg.get("visual_follow_window", 0.45)),
            search_radius=float(fusion_cfg.get("audio_search_radius", 2.0)),
            max_shift=audio_max_shift,
            micro_crossfade_ms=float(fusion_cfg.get("micro_crossfade_ms", 80)),
        )
        cand["start"] = round(snap_s["final_start"], 3)
        cand["snap_start_decision"] = snap_s["decision"]
        cand["snap_start_source"] = snap_s["anchor_source"]
        if snap_s.get("needs_fade"):
            cand["snap_start_needs_fade"] = True
            cand["snap_start_fade_ms"] = snap_s.get("fade_ms", 80)
        if snap_s.get("audio_onset") is not None:
            cand["audio_onset_start"] = snap_s["audio_onset"]

        # Snap end (和events用完全相同的参数)
        snap_e = three_tier_snap_end(
            float(original_end), cut_points, anchor,
            lead_out_audio=0.10,
            lead_out_visual=0.05,
            visual_tail_window=float(fusion_cfg.get("visual_tail_window_end", 0.60)),
            search_radius=float(fusion_cfg.get("audio_search_radius", 2.0)),
            max_shift=audio_max_shift,
        )
        cand["end"] = round(snap_e["final_end"], 3)
        cand["snap_end_decision"] = snap_e["decision"]
        cand["snap_end_source"] = snap_e["anchor_source"]
        if snap_e.get("needs_fade"):
            cand["snap_end_needs_fade"] = True
            cand["snap_end_fade_ms"] = snap_e.get("fade_ms", 80)
        if snap_e.get("audio_end") is not None:
            cand["audio_onset_end"] = snap_e["audio_end"]

        cand["snap_method"] = "three_tier_asr"
        snapped_count += 1

    logger.info(
        "event_cards: 候选片段snap完成 — %d个候选精修成功, %d个保留原始VLM时间",
        snapped_count, fallback_count,
    )


# ── 事件编译逻辑 (从 compile_event_cards.py 内联) ────────────────


def _temporal_mode(window: dict[str, Any], start: float, end: float) -> str:
    """确定事件的时间线模式 (flashback/flashforward/linear)。"""
    best = (0.0, "unknown")
    for item in window.get("timeline_segments", []):
        if not isinstance(item, dict):
            continue
        left, right = item.get("start"), item.get("end")
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            continue
        overlap = min(end, float(right)) - max(start, float(left))
        if overlap > best[0]:
            best = (overlap, str(item.get("mode", "unknown")))
    return best[1]


# Event function 归一化规则（关键词 + 正则，优先级：正则 > 关键词）
# 覆盖 VLM 自由文本输出的常见变体，归一化到 7 个标准叙事功能类别
# 注意：VLM 输出的中文 function 值常为具体叙事描述（如"神罚启动与逃亡指令"），
# 关键词覆盖不可避免有遗漏，"other" 类别是预期内的。
_FUNCTION_RULES: list[tuple[str, list[str], str | None]] = [
    # (category, keywords, regex_pattern_or_None)
    # 顺序重要：更具体/排他的类别放前面
    ("payoff",
     [
         "高潮", "决战", "团聚", "和解", "惩戒", "救赎", "climax",
         "家人相认", "父女相认", "夫妻重逢", "母女团聚", "力量觉醒", "能力爆发",
         "能力展示", "救援", "救场", "英雄", "神罚启动",
         "守护", "反击", "震撼", "击杀", "胜利", "击溃敌人",
     ],
     None),
    ("reveal",
     [
         "真相揭露", "身份揭示", "反转", "揭秘", "闪回交代", "revelation",
         "揭晓", "身份反转", "揭露", "真相", "线索发现", "身份暗示", "旧怨揭露",
         "闪回", "回忆", "记忆", "身世", "阴谋揭露", "信息获取", "情报",
         "告知", "消息", "背景交代", "真相引爆", "真相揭晓", "核心反转",
         "真相大白",
     ],
     r".*(真相|揭露|揭示|揭晓|反转|闪回|回忆|身世|揭秘|阴谋).*"),
    ("coda",
     [
         "尾声", "结局", "收束", "归家", "resolution",
         "悬念收尾", "悬念留存", "悬念抛出", "结尾", "离场", "收尾",
         "回家", "归家", "约定", "承诺", "团聚与归家",
     ],
     None),
    ("consequence",
     [
         "后果", "余波", "创伤", "代价", "aftermath", "牺牲",
         "悼念", "复仇誓言", "濒死", "重伤", "受伤", "哭泣",
         "崩溃", "病情恶化", "绝望嘶吼",
     ],
     None),
    ("turn",
     [
         "转折", "命运转折", "立场转变", "态度转变", "turning_point",
         "逃亡失败", "绝境", "绝望时刻", "决心", "决断",
         "情绪低谷", "悲剧", "危机降临", "异状陡生",
     ],
     None),
    ("escalation",
     [
         "冲突升级", "矛盾升级", "对峙", "升级", "激化", "推进", "confrontation",
         "交锋", "冲突爆发", "矛盾激化", "矛盾引爆", "正面冲突", "冲突引入",
         "冲突引爆", "核心冲突", "立场决裂", "正式宣战", "言语对峙",
         "立场表露", "逼问", "刑讯", "反抗", "交战", "追兵", "密谋",
         "反派登场", "威胁", "放话", "挑衅", "嘲讽", "谋划",
         "反对", "拒绝", "谈判", "交易", "条件", "驱逐", "警告",
         "冲突开场", "对立", "冲突建立", "矛盾激化",
     ],
     r".*(升级|对峙|交锋|冲突|宣战|交战|对抗|威胁|挑衅|密谋).*"),
    ("setup",
     [
         "开篇", "场景建立", "人物引入", "铺垫", "状态展示", "开场", "establish",
         "场景引入", "状态呈现", "新场景", "氛围铺垫", "新线展开", "新冲突开场",
         "关键人物登场", "人物登场", "登场", "建立", "交代", "仪式开场", "仪式启动",
         "场景切换", "转场", "准备", "出发", "行动启动", "建立状态",
         "场景建立", "世界观", "温情段落", "新冲突出现",
     ],
     r"^(开篇|开场|场景|登场|建立|氛围|出发|准备).*"),
]


def _normalize_event_function(raw: str) -> tuple[str, bool]:
    """将 VLM 自由文本 function 归一化到标准叙事功能类别。

    返回 (normalized_category, is_matched)。is_matched=False 时返回 "other"。
    匹配策略：正则优先于关键词，规则按数组顺序首次匹配即返回。
    注意：VLM 输出常为混合描述（如"真相揭露后的冲突升级"），无法完美分类，
    "other" 类别是预期内的，下游消费方需容忍。
    """
    import re as _re
    if not raw:
        return "other", False
    raw_lower = raw.lower().strip()

    for _cat, _kws, _pat in _FUNCTION_RULES:
        # 1. 正则匹配
        if _pat and _re.search(_pat, raw):
            return _cat, True
        # 2. 关键词包含匹配
        if any(kw in raw_lower for kw in _kws):
            return _cat, True

    return "other", False


def _same_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """判断两个事件是否重复 (基于 IoU + 摘要 + 角色重叠)。"""
    if left["source_id"] != right["source_id"] or left["function"] != right["function"]:
        return False
    overlap = min(left["end"], right["end"]) - max(left["start"], right["start"])
    union = max(left["end"], right["end"]) - min(left["start"], right["start"])
    iou = overlap / union if overlap > 0 and union > 0 else 0.0
    close = (
        abs(left["start"] - right["start"]) <= 1.0
        and abs(left["end"] - right["end"]) <= 1.0
    )
    same_summary = normalize_text(left["summary"]).casefold() == normalize_text(
        right["summary"]
    ).casefold()
    character_overlap = bool(set(left["character_names"]) & set(right["character_names"]))
    return (same_summary and iou >= 0.5) or (close and character_overlap)


def _compile_events(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从窗口分析结果编译去重后的中等粒度事件卡。"""
    provisional: list[dict[str, Any]] = []
    for window in windows:
        for beat in window.get("story_beats", []):
            if not isinstance(beat, dict):
                continue
            start, end = beat.get("start"), beat.get("end")
            if (
                not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or float(end) <= float(start)
            ):
                continue
            raw_function = normalize_text(beat.get("function")) or "other"
            provisional.append({
                "source_id": window.get("source_id"),
                "episode": window.get("episode"),
                "start": float(start),
                "end": float(end),
                "summary": normalize_text(beat.get("summary")),
                "function": _normalize_event_function(raw_function)[0],
                "raw_function": raw_function,
                "character_names": sorted({
                    normalize_text(item)
                    for item in beat.get("characters", [])
                    if normalize_text(item)
                }),
                "cause": normalize_text(beat.get("cause")),
                "effect": normalize_text(beat.get("effect")),
                "open_question": normalize_text(beat.get("open_question")),
                "temporal_mode": _temporal_mode(window, float(start), float(end)),
                "evidence_window_ids": [window.get("window_id")],
                "member_ranges": [{"start": float(start), "end": float(end)}],
            })

    # 聚类去重
    # 与组内任意已有元素匹配即加入该组（避免传递性匹配失败）
    groups: list[list[dict[str, Any]]] = []
    for item in sorted(
        provisional,
        key=lambda v: (int(v.get("episode", 0)), str(v.get("source_id")), v["start"], v["end"]),
    ):
        matched = None
        for g in groups:
            if any(_same_event(existing, item) for existing in g):
                matched = g
                break
        if matched is None:
            groups.append([item])
        else:
            matched.append(item)

    # 合并每组
    events: list[dict[str, Any]] = []
    for group in groups:
        starts = [item["start"] for item in group]
        ends = [item["end"] for item in group]
        summaries = sorted(
            {item["summary"] for item in group if item["summary"]},
            key=lambda v: (-len(v), v),
        )
        summary = summaries[0] if summaries else "未命名剧情事件"
        # BUGFIX: 使用min(starts)/max(ends)覆盖所有观测范围，避免中位数截断事件首尾
        event_start = round(float(min(starts)), 3)
        event_end = round(float(max(ends)), 3)
        median_start = round(float(median(starts)), 3)
        median_end = round(float(median(ends)), 3)
        payload = {
            "source_id": group[0]["source_id"],
            "episode": group[0]["episode"],
            "start": event_start,
            "end": event_end,
            "summary": summary,
        }
        events.append({
            "id": stable_id("event", payload),
            "episode": group[0]["episode"],
            "source_id": group[0]["source_id"],
            "source_ranges": [{
                "start": event_start,
                "end": event_end,
                "original_start": median_start,
                "original_end": median_end,
                "evidence_window_ids": sorted({
                    str(wid)
                    for item in group
                    for wid in item["evidence_window_ids"]
                    if wid
                }),
            }],
            "summary": summary,
            "function": Counter(item["function"] for item in group).most_common(1)[0][0],
            "raw_function": Counter(item["raw_function"] for item in group).most_common(1)[0][0],
            "character_names": sorted({n for item in group for n in item["character_names"]}),
            "cause": max((item["cause"] for item in group), key=len, default=""),
            "effect": max((item["effect"] for item in group), key=len, default=""),
            "open_question": max((item["open_question"] for item in group), key=len, default=""),
            "temporal_mode": (
                Counter(
                    item["temporal_mode"] for item in group if item["temporal_mode"] != "unknown"
                ).most_common(1)[0][0]
                if any(item["temporal_mode"] != "unknown" for item in group)
                else "unknown"
            ),
            "candidate_ids": [],
            "boundary_resolution": {
                "status": "consensus" if len(group) > 1 else "single_observation",
                "member_ranges": [m for item in group for m in item["member_ranges"]],
                "median_start": median_start,
                "median_end": median_end,
            },
        })

    return sorted(
        events,
        key=lambda item: (
            int(item["episode"]),
            str(item["source_id"]),
            float(item["source_ranges"][0]["start"]),
            item["id"],
        ),
    )


def _compile_candidates(
    windows: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """从窗口分析结果编译 highlight/hook 候选目录。"""
    records: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for window in windows:
        for candidate in window.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            start, end = candidate.get("start"), candidate.get("end")
            kind = candidate.get("type")
            if (
                kind not in {"highlight", "hook"}
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or float(end) <= float(start)
            ):
                continue
            original_id = normalize_text(candidate.get("id"))
            identity = {
                "source_id": window.get("source_id"),
                "episode": window.get("episode"),
                "start": round(float(start), 3),
                "end": round(float(end), 3),
                "type": kind,
                "original_id": original_id,
            }
            # 去重
            duplicate = next(
                (
                    item for item in records
                    if item["source_id"] == window.get("source_id")
                    and item["type"] == kind
                    and (
                        (original_id and item.get("original_id") == original_id)
                        or (
                            abs(item["start"] - float(start)) <= 0.5
                            and abs(item["end"] - float(end)) <= 0.5
                        )
                    )
                ),
                None,
            )
            if duplicate:
                duplicate["evidence_window_ids"] = sorted(
                    set(duplicate["evidence_window_ids"]) | {str(window.get("window_id"))}
                )
                continue
            candidate_id = (
                original_id
                if original_id and original_id not in used_ids
                else stable_id(f"candidate-{kind}", identity)
            )
            if candidate_id in used_ids:
                candidate_id = stable_id(f"candidate-{kind}", identity)
            used_ids.add(candidate_id)
            overlapping_events = [
                event["id"] for event in events
                if event["source_id"] == window.get("source_id")
                and any(
                    min(float(end), float(rng["end"]))
                    - max(float(start), float(rng["start"]))
                    > 0.05
                    for rng in event["source_ranges"]
                )
            ]
            records.append({
                "id": candidate_id,
                "original_id": original_id,
                "source_id": window.get("source_id"),
                "episode": window.get("episode"),
                "start": float(start),
                "end": float(end),
                "type": kind,
                "strength": candidate.get("strength"),
                "reason": normalize_text(candidate.get("reason")),
                "anchor": normalize_text(candidate.get("anchor")),
                "lead_in": normalize_text(candidate.get("lead_in")),
                "payoff_or_open_question": normalize_text(candidate.get("payoff_or_open_question")),
                "dialogue_excerpt": normalize_text(candidate.get("dialogue_excerpt")),
                "event_ids": overlapping_events,
                "evidence_window_ids": [str(window.get("window_id"))],
            })

    # 回填事件→候选的映射
    for event in events:
        event["candidate_ids"] = sorted(
            c["id"] for c in records if event["id"] in c["event_ids"]
        )

    return sorted(
        records,
        key=lambda item: (int(item["episode"]), float(item["start"]), item["type"], item["id"]),
    )