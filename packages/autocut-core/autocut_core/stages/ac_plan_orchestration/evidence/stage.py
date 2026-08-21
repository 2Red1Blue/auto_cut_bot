#!/usr/bin/env python3
"""story_evidence Stage — 为已批准故事构建 Story Evidence Packet (证据包)。

直接调用evidence_builder.build_packet()，无sys.argv hack副作用。

输入: story_scripts (经 story_approval 审批后的脚本集)
输出: story_evidence (story-evidence/index.json)
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autocut_core import (
    ArtifactBus,
    Stage,
    StageContract,
    Task,
)
from autocut_core.contracts.evidence_validation import (
    selected_manifest_path,
)
from autocut_core.contracts.evidence_validation import (
    validate as validate_story_evidence,
)
from autocut_core.io import (
    atomic_write_json,
    load_json,
    sha256_file,
    update_project_stage,
)
from autocut_core.libs.evidence_builder import (
    build_packet,
    by_id,
    inferred_window_neighbors,
    packet_filename,
    render_review,
)
from autocut_core.logging import get_logger
from autocut_core.schema.compat import validate_task_response

logger = get_logger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _ApprovedStoryInput:
    approval_item: dict[str, Any]
    script: dict[str, Any]
    script_path: Path


def _require_sha256(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{where} must be a SHA-256 hex digest")
    return value


def _admit_approved_stories(root: Path, approval: Any) -> tuple[list[_ApprovedStoryInput], str]:
    """Validate every approved input before any evidence output is created."""
    if not isinstance(approval, dict):
        raise ValueError("Story Approval must be an object")
    if approval.get("fulfillment_status") != "ready":
        raise ValueError("Story Approval fulfillment_status is not ready")
    stories = approval.get("stories")
    if not isinstance(stories, list):
        raise ValueError("Story Approval stories must be an array")
    selected_story_ids = approval.get("selected_story_ids")
    if not isinstance(selected_story_ids, list) or not all(
        isinstance(story_id, str) and story_id for story_id in selected_story_ids
    ):
        raise ValueError("Story Approval selected_story_ids must be non-empty strings")
    if not selected_story_ids:
        raise ValueError("Story Approval selects no Stories")
    if len(set(selected_story_ids)) != len(selected_story_ids):
        raise ValueError("Story Approval selected_story_ids contains duplicates")

    stories_by_id: dict[str, dict[str, Any]] = {}
    approved_by_id: dict[str, dict[str, Any]] = {}
    for item_index, item in enumerate(stories):
        where = f"Story Approval stories[{item_index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where} must be an object")
        story_id = item.get("story_id")
        if not isinstance(story_id, str) or not story_id:
            raise ValueError(f"{where}.story_id must be non-empty")
        if story_id in stories_by_id:
            raise ValueError(f"Story Approval contains duplicate story_id: {story_id}")
        stories_by_id[story_id] = item
        if item.get("decision") == "approved":
            approved_by_id[story_id] = item

    selected_story_id_set = set(selected_story_ids)
    if selected_story_id_set != set(approved_by_id):
        raise ValueError("Story Approval selected_story_ids must match approved Stories exactly")

    portfolio_path = root / "story-portfolio.json"
    if not portfolio_path.is_file():
        raise FileNotFoundError(f"Story Portfolio is missing: {portfolio_path}")
    # Reading is deliberate: a syntactically invalid portfolio is not a binding.
    portfolio = load_json(portfolio_path)
    if not isinstance(portfolio, dict):
        raise ValueError("Story Portfolio must be an object")
    portfolio_sha256 = sha256_file(portfolio_path)

    admitted: list[_ApprovedStoryInput] = []
    for story_id in selected_story_ids:
        item = approved_by_id[story_id]
        script_path_value = item.get("script_path")
        if not isinstance(script_path_value, str) or not script_path_value:
            raise ValueError(f"{story_id}: approved Story lacks script_path")
        script_path = Path(script_path_value).expanduser().resolve()
        if not script_path.is_file():
            raise FileNotFoundError(f"{story_id}: approved Story Script is missing")
        approved_script_sha256 = _require_sha256(
            item.get("approved_script_sha256"),
            f"{story_id}: approved_script_sha256",
        )
        if sha256_file(script_path) != approved_script_sha256:
            raise ValueError(f"{story_id}: approved Story Script SHA-256 is stale")
        if item.get("portfolio_sha256") != portfolio_sha256:
            raise ValueError(f"{story_id}: approved Story Portfolio SHA-256 is stale")
        if not isinstance(item.get("decided_at"), str) or not item["decided_at"]:
            raise ValueError(f"{story_id}: approved Story lacks decided_at")
        script = load_json(script_path)
        if not isinstance(script, dict):
            raise ValueError(f"{story_id}: approved Story Script must be an object")
        script_errors = validate_task_response("story_script", script)
        if script_errors:
            raise ValueError(
                f"{story_id}: invalid approved Story Script: " + "; ".join(script_errors[:20])
            )
        if script.get("story_id") != story_id:
            raise ValueError(f"{story_id}: approved Story Script identity mismatch")
        script_portfolio = script.get("portfolio")
        if (
            not isinstance(script_portfolio, dict)
            or script_portfolio.get("portfolio_sha256") != portfolio_sha256
        ):
            raise ValueError(f"{story_id}: approved Story Script Portfolio binding is stale")
        admitted.append(_ApprovedStoryInput(item, script, script_path))
    return admitted, portfolio_sha256


def _load_jsonl_records(path):
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _load_catalogs(root):
    """加载证据包编译所需的所有上游catalog数据。"""
    catalogs = {}

    # Events (jsonl)
    events_path = root / "event-cards.jsonl"
    if not events_path.is_file():
        raise FileNotFoundError(f"event-cards.jsonl not found at {events_path}")
    events_list = _load_jsonl_records(events_path)
    catalogs["events_by_id"] = by_id(events_list, where="event_cards")

    # Candidates (json, wrapped in {"candidates": [...]})
    candidates_path = root / "highlight-hook-catalog.json"
    if not candidates_path.is_file():
        raise FileNotFoundError(f"highlight-hook-catalog.json not found at {candidates_path}")
    candidates_raw = load_json(candidates_path)
    candidates_list = (
        candidates_raw.get("candidates", candidates_raw)
        if isinstance(candidates_raw, dict)
        else candidates_raw
    )
    catalogs["candidates_by_id"] = by_id(candidates_list, where="candidate_catalog")

    # Bible 数据: 优先独立 bible 文件 (character-bible.json 等),
    # 不存在时回退到全剧 series-bible.json (v2 流程产物, 角色/关系/事实/
    # 线程/问题都内嵌其中)
    series_bible_path = root / "series-bible.json"
    if not series_bible_path.is_file():
        raise FileNotFoundError(f"series-bible.json not found at {series_bible_path}")
    series_bible = load_json(series_bible_path)
    if not isinstance(series_bible, dict):
        raise ValueError("series-bible.json must be an object")

    def _bible_section(file_name: str, section_key: str) -> list:
        file_path = root / file_name
        if file_path.is_file():
            payload = load_json(file_path)
            items = payload.get(section_key, payload)
            if isinstance(items, dict) and section_key in payload:
                items = payload.get(section_key, [])
            return items if isinstance(items, list) else []
        return series_bible.get(section_key, [])

    catalogs["characters_by_id"] = by_id(
        _bible_section("character-bible.json", "characters"),
        where="character_bible",
    )
    catalogs["relationships_by_id"] = by_id(
        _bible_section("relationship-bible.json", "relationships"),
        where="relationship_bible",
    )
    catalogs["facts_by_id"] = by_id(
        _bible_section("fact-bible.json", "facts"),
        where="fact_bible",
    )
    catalogs["threads_by_id"] = by_id(
        _bible_section("thread-bible.json", "story_threads"),
        where="thread_bible",
    )
    catalogs["questions_by_id"] = by_id(
        _bible_section("question-bible.json", "open_questions"),
        where="question_bible",
    )

    # Source manifest (兼容下划线官方命名和中划线旧命名)
    source_path = selected_manifest_path(root, "source_manifest.json", "source-manifest.json")
    source_data = load_json(source_path)
    source_list = (
        source_data.get("sources", source_data) if isinstance(source_data, dict) else source_data
    )
    catalogs["sources_by_id"] = by_id(source_list, where="source_manifest")

    # Thread beats from episode digests
    thread_beats = {}
    digest_dir = root / "episode-digests"
    if digest_dir.is_dir():
        for digest_path in sorted(digest_dir.glob("*.json")):
            digest = load_json(digest_path)
            for beat in digest.get("thread_beats", []):
                if isinstance(beat, dict) and beat.get("id"):
                    thread_beats[beat["id"]] = beat
    catalogs["thread_beats_by_id"] = thread_beats

    # Window manifest (兼容下划线官方命名和中划线旧命名)
    win_manifest_path = selected_manifest_path(root, "window_manifest.json", "window-manifest.json")
    win_manifest = load_json(win_manifest_path)
    manifest_windows = (
        win_manifest.get("windows", win_manifest)
        if isinstance(win_manifest, dict)
        else win_manifest
    )
    catalogs["manifest_windows_by_id"] = by_id(manifest_windows, where="window_manifest")

    # Window summaries (jsonl)
    win_summ_path = root / "window-summaries.jsonl"
    if not win_summ_path.is_file():
        raise FileNotFoundError(f"window-summaries.jsonl not found at {win_summ_path}")
    window_summaries = {}
    for w in _load_jsonl_records(win_summ_path):
        if w.get("window_id"):
            window_summaries[w["window_id"]] = w
    catalogs["window_summaries_by_id"] = window_summaries

    # Window neighbors
    catalogs["neighbors"] = inferred_window_neighbors(list(manifest_windows))

    # Processing units by episode
    units_by_episode = {}
    for source in source_list:
        ep = source.get("episode")
        sid = source.get("id")
        if isinstance(ep, int) and isinstance(sid, str):
            units_by_episode.setdefault(ep, set()).add(sid)
    catalogs["units_by_episode"] = units_by_episode

    # Fingerprints
    fingerprints = {}
    for name, path in [
        ("series_bible_sha256", series_bible_path),
        ("event_cards_sha256", root / "event-cards.jsonl"),
        ("candidate_catalog_sha256", root / "highlight-hook-catalog.json"),
        ("source_manifest_sha256", source_path),
        ("window_manifest_sha256", win_manifest_path),
        ("window_summaries_sha256", root / "window-summaries.jsonl"),
    ]:
        if not path.is_file():
            raise FileNotFoundError(f"missing Story Evidence fingerprint input: {path}")
        fingerprints[name] = sha256_file(path)
    catalogs["fingerprints"] = fingerprints

    return catalogs


def _reserve_evidence_publication(execute):
    """Hold an exclusive, fail-closed reservation through publication."""

    def reserved_execute(self, bus, tasks):
        job_root = self.config.job_root
        if job_root is None:
            return execute(self, bus, tasks)
        reservation = Path(job_root) / ".story-evidence-publication.lock"
        try:
            reservation.mkdir()
        except FileExistsError as exc:
            raise FileExistsError(
                f"Story Evidence publication is already reserved: {reservation}"
            ) from exc
        try:
            return execute(self, bus, tasks)
        finally:
            reservation.rmdir()

    return reserved_execute


class EvidenceStage(Stage):
    """Story Evidence — 为已批准故事构建证据包。"""

    @property
    def contract(self):
        return StageContract(
            stage_name="story_evidence",
            input_artifacts=["story_scripts", "story_approval"],
            output_artifacts=["story_evidence"],
            description="构建 Story Evidence Packet（直接函数调用，无sys.argv hack）",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus):
        return [
            Task(
                type="assemble",
                payload={
                    "story_approval": self.resolve_artifact_path(
                        bus, "story_approval", "story_approval"
                    ),
                },
            )
        ]

    @_reserve_evidence_publication
    def execute(self, bus: ArtifactBus, tasks):
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root = Path(cfg.job_root)
        p = tasks[0].payload

        approval_path = Path(p["story_approval"])
        approval = load_json(approval_path)
        approved_scripts, portfolio_sha256 = _admit_approved_stories(root, approval)

        catalogs = _load_catalogs(root)
        adjacent_window_hops = 2

        output_dir = root / "story-evidence"
        packets = []
        approval_sha256 = sha256_file(approval_path) if approval_path.is_file() else ""

        for approved_input in approved_scripts:
            packet = build_packet(
                approval_item=approved_input.approval_item,
                approval_sha256=approval_sha256,
                script=approved_input.script,
                events=catalogs["events_by_id"],
                candidates=catalogs["candidates_by_id"],
                characters=catalogs["characters_by_id"],
                relationships=catalogs["relationships_by_id"],
                facts=catalogs["facts_by_id"],
                threads=catalogs["threads_by_id"],
                thread_beats=catalogs["thread_beats_by_id"],
                questions=catalogs["questions_by_id"],
                sources=catalogs["sources_by_id"],
                manifest_windows=catalogs["manifest_windows_by_id"],
                window_summaries=catalogs["window_summaries_by_id"],
                neighbors=catalogs["neighbors"],
                fingerprints=catalogs["fingerprints"],
                adjacent_window_hops=adjacent_window_hops,
                units_by_episode=catalogs["units_by_episode"],
            )

            packets.append(packet)

        # index 按 STORY_EVIDENCE_INDEX_SCHEMA 构建：
        # schema_version=1.1, method=v4, packets 为引用式（path+sha256）
        # Build every packet before creating the output directory.  A broken
        # later Story must never leave a prefix of the approved portfolio
        # published as evidence.
        packet_refs = []
        all_statuses = set()
        for packet in packets:
            packet_path = output_dir / packet_filename(packet["story_id"])
            story_script_sha256 = ""
            approval_item = next(
                item.approval_item
                for item in approved_scripts
                if item.approval_item["story_id"] == packet["story_id"]
            )
            story_script_sha256 = approval_item["approved_script_sha256"]
            packet_refs.append(
                {
                    "story_id": packet["story_id"],
                    "title": packet.get("title", ""),
                    "production_slot": packet.get("production_slot"),
                    "status": packet.get("status", "ready"),
                    "path": str(packet_path),
                    # Filled from the isolated candidate directory below.
                    "packet_sha256": "",
                    "story_script_sha256": story_script_sha256,
                }
            )
            all_statuses.add(packet.get("status", "ready"))
        if all_statuses and all_statuses <= {"ready"}:
            index_status = "ready"
        elif all_statuses and all_statuses <= {"ready", "needs_video_review"}:
            index_status = "needs_video_review"
        elif all_statuses:
            index_status = "incomplete"
        else:
            index_status = "incomplete"
        index = {
            "schema_version": "1.1",
            "method": "structured-thread-beat-recall-v4",
            "status": index_status,
            "story_approval_sha256": approval_sha256,
            "portfolio_sha256": portfolio_sha256,
            "selected_story_count": len(packet_refs),
            "packets": packet_refs,
        }
        candidate_dir = Path(tempfile.mkdtemp(prefix=".story-evidence-", dir=root))
        try:
            for packet in packets:
                atomic_write_json(candidate_dir / packet_filename(packet["story_id"]), packet)
            for packet_ref in packet_refs:
                packet_ref["packet_sha256"] = sha256_file(
                    candidate_dir / packet_filename(packet_ref["story_id"])
                )
            atomic_write_json(candidate_dir / "index.json", index)
            # review markdown 单独落盘（不在 schema 内，供人工查看）
            atomic_write_json(
                candidate_dir / "review.md",
                {"schema_version": "1.0", "review_markdown": render_review(packets)},
            )
            report = validate_story_evidence(root, evidence_dir=candidate_dir)
            if not report["ok"]:
                raise ValueError(
                    "Story Evidence validation failed: " + "; ".join(report["errors"][:30])
                )
            if output_dir.exists():
                raise FileExistsError(
                    f"Story Evidence output already exists and will not be overwritten: {output_dir}"
                )
            candidate_dir.replace(output_dir)
        finally:
            if candidate_dir.exists():
                for path in candidate_dir.iterdir():
                    path.unlink()
                candidate_dir.rmdir()

        index_path = output_dir / "index.json"

        ref = bus.put("story_evidence", {"path": str(index_path)}, stage="story_evidence")
        update_project_stage(
            root / "project.json",
            "story_evidence",
            "completed",
            outputs={"story_evidence": str(index_path)},
        )
        logger.info("story_evidence: 构建完成 %d 个证据包", len(packets))
        return [ref]
