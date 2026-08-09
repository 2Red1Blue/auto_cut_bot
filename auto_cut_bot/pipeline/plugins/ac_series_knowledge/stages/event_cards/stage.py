"""EventCardsStage — 从窗口分析结果编译中等粒度剧情事件。
"""

from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Any

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import (
    atomic_write_json, atomic_write_jsonl, load_jsonl,
    normalize_text, stable_id, update_project_stage,
)


class EventCardsStage(Stage):
    """编译 Event Cards 和 Highlight/Hook 候选目录。

    输入:  window_summaries (WindowAnalysisStage 产出)
    输出:  event_cards + highlight_hook_catalog
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="event_cards",
            input_artifacts=["window_analysis"],
            output_artifacts=["event_cards", "highlight_hook_catalog"],
            description="编译中等粒度剧情事件 + Highlight/Hook 候选目录",
            db_reads=[],
            db_writes=["boundaries", "subjects"],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """从 bus 解析上游 window_summaries 的路径作为编译输入。"""
        ref = bus.latest("window_analysis")
        if ref is None:
            raise RuntimeError("上游 window_analysis 产物未找到")
        artifacts = bus.get(ref)
        summaries_path = artifacts.get("path") if isinstance(artifacts, dict) else None
        if not summaries_path:
            summaries_path = str(bus.resolve("window_analysis", "window_summaries").path)  # type: ignore[union-attr]
        return [Task(type="compile_events", payload={
            "window_summaries": str(summaries_path),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """内联编译事件卡和候选目录。"""
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        summaries = tasks[0].payload["window_summaries"]

        # ── 加载窗口数据 ──
        windows = load_jsonl(Path(summaries))
        if not windows:
            raise RuntimeError("window summaries 为空")

        # ── 编译事件 ──
        events = _compile_events(windows)
        if not events:
            raise RuntimeError("无可用的 story beats 编译 Event Cards")

        # ── 编译候选目录 ──
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
    character_overlap = bool(set(left["characters"]) & set(right["characters"]))
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
            provisional.append({
                "source_id": window.get("source_id"),
                "episode": window.get("episode"),
                "start": float(start),
                "end": float(end),
                "summary": normalize_text(beat.get("summary")),
                "function": normalize_text(beat.get("function")) or "other",
                "characters": sorted({
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
    groups: list[list[dict[str, Any]]] = []
    for item in sorted(
        provisional,
        key=lambda v: (int(v.get("episode", 0)), str(v.get("source_id")), v["start"], v["end"]),
    ):
        matched = next((g for g in groups if _same_event(g[0], item)), None)
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
        payload = {
            "source_id": group[0]["source_id"],
            "episode": group[0]["episode"],
            "start": round(float(median(starts)), 3),
            "end": round(float(median(ends)), 3),
            "summary": summary,
        }
        events.append({
            "id": stable_id("event", payload),
            "episode": group[0]["episode"],
            "source_id": group[0]["source_id"],
            "source_ranges": [{
                "start": payload["start"],
                "end": payload["end"],
                "evidence_window_ids": sorted({
                    str(wid)
                    for item in group
                    for wid in item["evidence_window_ids"]
                    if wid
                }),
            }],
            "summary": summary,
            "function": group[0]["function"],
            "character_names": sorted({n for item in group for n in item["characters"]}),
            "cause": max((item["cause"] for item in group), key=len, default=""),
            "effect": max((item["effect"] for item in group), key=len, default=""),
            "open_question": max((item["open_question"] for item in group), key=len, default=""),
            "temporal_mode": next(
                (item["temporal_mode"] for item in group if item["temporal_mode"] != "unknown"),
                "unknown",
            ),
            "candidate_ids": [],
            "boundary_resolution": {
                "status": "consensus" if len(group) > 1 else "single_observation",
                "member_ranges": [m for item in group for m in item["member_ranges"]],
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
                and min(float(end), event["source_ranges"][0]["end"])
                - max(float(start), event["source_ranges"][0]["start"])
                > 0.05
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