#!/usr/bin/env python3
"""story_evidence Stage — 为已批准故事构建 Story Evidence Packet (证据包)。

直接调用evidence_builder.build_packet()，无sys.argv hack副作用。

输入: story_scripts (经 story_approval 审批后的脚本集)
输出: story_evidence (story-evidence/index.json)
"""

from __future__ import annotations

import json
from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.contracts.evidence_validation import validate as validate_story_evidence
from autocut_core.io import (
    atomic_write_json, load_json, sha256_file, update_project_stage, load_jsonl,
)
from autocut_core.libs.evidence_builder import (
    build_packet, by_id, inferred_window_neighbors, packet_filename, render_review,
)
from autocut_core.db.client import StageDBClient
from autocut_core.logging import get_logger

logger = get_logger(__name__)


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
    candidates_list = candidates_raw.get("candidates", candidates_raw) if isinstance(candidates_raw, dict) else candidates_raw
    catalogs["candidates_by_id"] = by_id(candidates_list, where="candidate_catalog")

    # Character bible
    char_path = root / "character-bible.json"
    char_data = load_json(char_path) if char_path.is_file() else {"characters": []}
    char_list = char_data.get("characters", char_data) if isinstance(char_data, dict) else char_data
    catalogs["characters_by_id"] = by_id(char_list, where="character_bible")

    # Relationship bible
    rel_path = root / "relationship-bible.json"
    rel_data = load_json(rel_path) if rel_path.is_file() else {"relationships": []}
    rel_list = rel_data.get("relationships", rel_data) if isinstance(rel_data, dict) else rel_data
    catalogs["relationships_by_id"] = by_id(rel_list, where="relationship_bible")

    # Fact bible
    fact_path = root / "fact-bible.json"
    fact_data = load_json(fact_path) if fact_path.is_file() else {"facts": []}
    fact_list = fact_data.get("facts", fact_data) if isinstance(fact_data, dict) else fact_data
    catalogs["facts_by_id"] = by_id(fact_list, where="fact_bible")

    # Thread bible
    thread_path = root / "thread-bible.json"
    thread_data = load_json(thread_path) if thread_path.is_file() else {"story_threads": []}
    thread_list = thread_data.get("story_threads", thread_data) if isinstance(thread_data, dict) else thread_data
    catalogs["threads_by_id"] = by_id(thread_list, where="thread_bible")

    # Question bible
    q_path = root / "question-bible.json"
    q_data = load_json(q_path) if q_path.is_file() else {"open_questions": []}
    q_list = q_data.get("open_questions", q_data) if isinstance(q_data, dict) else q_data
    catalogs["questions_by_id"] = by_id(q_list, where="question_bible")

    # Source manifest (兼容下划线官方命名和中划线旧命名)
    source_path = root / "source_manifest.json"
    if not source_path.is_file():
        source_path = root / "source-manifest.json"
    if not source_path.is_file():
        raise FileNotFoundError(f"source_manifest.json not found at {root}")
    source_data = load_json(source_path)
    source_list = source_data.get("sources", source_data) if isinstance(source_data, dict) else source_data
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

    # Window manifest
    win_manifest_path = root / "window-manifest.json"
    if not win_manifest_path.is_file():
        raise FileNotFoundError(f"window-manifest.json not found at {win_manifest_path}")
    win_manifest = load_json(win_manifest_path)
    manifest_windows = win_manifest.get("windows", win_manifest) if isinstance(win_manifest, dict) else win_manifest
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
        ("event_cards_sha256", root / "event-cards.jsonl"),
        ("highlight_hook_catalog_sha256", root / "highlight-hook-catalog.json"),
        ("source_manifest_sha256", source_path),
        ("window_manifest_sha256", root / "window_manifest.json"),
    ]:
        if path.is_file():
            fingerprints[name] = sha256_file(path)
    catalogs["fingerprints"] = fingerprints

    return catalogs


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
        return [Task(type="assemble", payload={
            "story_approval": self.resolve_artifact_path(bus, "story_approval", "story_approval"),
        })]

    def execute(self, bus: ArtifactBus, tasks):
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root = Path(cfg.job_root)
        p = tasks[0].payload

        approval_path = Path(p["story_approval"])
        approval = load_json(approval_path)
        if not isinstance(approval, dict):
            raise ValueError(f"Invalid story_approval: {approval_path}")

        approved_scripts = approval.get("approved_scripts", [])
        if not approved_scripts:
            logger.warning("story_evidence: 无已批准的story scripts")

        catalogs = _load_catalogs(root)
        adjacent_window_hops = 2

        output_dir = root / "story-evidence"
        output_dir.mkdir(parents=True, exist_ok=True)

        packets = []
        approval_sha256 = sha256_file(approval_path) if approval_path.is_file() else ""

        for approval_item in approved_scripts:
            script_path = root / "story-scripts" / f"{approval_item['story_id']}.json"
            if not script_path.is_file():
                logger.warning("story_evidence: 找不到脚本文件: %s", script_path)
                continue
            script = load_json(script_path)

            packet = build_packet(
                approval_item=approval_item,
                approval_sha256=approval_sha256,
                script=script,
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

            packet_path = output_dir / packet_filename(packet["story_id"])
            atomic_write_json(packet_path, packet)
            packets.append(packet)

        index = {
            "schema_version": "1.2",
            "method": "structured-thread-beat-recall-v3",
            "packets": packets,
            "review_markdown": render_review(packets),
        }
        index_path = output_dir / "index.json"
        atomic_write_json(index_path, index)

        report = validate_story_evidence(root)
        if not report["ok"]:
            raise ValueError(
                "Story Evidence validation failed: "
                + "; ".join(report["errors"][:30])
            )

        ref = bus.put("story_evidence", {"path": str(index_path)}, stage="story_evidence")
        update_project_stage(
            root / "project.json", "story_evidence", "completed",
            outputs={"story_evidence": str(index_path)},
        )
        logger.info("story_evidence: 构建完成 %d 个证据包", len(packets))
        return [ref]
