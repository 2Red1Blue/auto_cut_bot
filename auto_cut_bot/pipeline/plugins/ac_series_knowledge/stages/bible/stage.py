"""BibleStage — Series Bible 汇总。

把 Registry (实体知识库) + Assignment (集→Series 分配) 汇总为
系列圣经: 结构化 JSON + 人工评审用 Markdown 视图。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import (
    atomic_write_json, atomic_write_text, load_json, load_jsonl,
    update_project_stage,
)
from autocut_core.libs.assemble_series_bible import assemble_bible


class BibleStage(Stage):
    """Series Bible — 汇总 Registry + Assignment 为系列圣经。

    输入: series_assignment (AssignmentStage 产出)
    输出: series_bible (series-bible.json + series-bible.md)
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="series_bible",
            input_artifacts=["series_assignment", "series_registry"],
            output_artifacts=["series_bible"],
            description="Series Bible 汇总",
            db_reads=["books"],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """解析 assignment 批次与 registry 产物 (含附属产物) 路径。"""
        ref = (
            bus.latest("series_assignment")
            or bus.resolve("series_assignment", "series_assignment")
        )
        if ref is None:
            raise RuntimeError("产物 series_assignment/series_assignment 未找到")
        data = bus.get(ref)
        assignment_batch = (
            data["path"] if isinstance(data, dict) and "path" in data else str(ref.path)
        )

        reg_ref = (
            bus.latest("series_registry")
            or bus.resolve("series_registry", "series_registry")
        )
        if reg_ref is None:
            raise RuntimeError("产物 series_registry/series_registry 未找到")
        reg_data = bus.get(reg_ref)
        return [Task(type="assemble", payload={
            "assignment_batch": assignment_batch,
            "series_registry": (
                reg_data["path"]
                if isinstance(reg_data, dict) and "path" in reg_data
                else str(reg_ref.path)
            ),
            "registry_admission": (
                reg_data.get("registry_admission", "") if isinstance(reg_data, dict) else ""
            ),
            "registry_quarantine": (
                reg_data.get("registry_quarantine", "") if isinstance(reg_data, dict) else ""
            ),
            "episode_digests": self.resolve_artifact_path(
                bus, "episode_digests", "episode_digests"
            ),
            "event_cards": self.resolve_artifact_path(bus, "event_cards", "event_cards"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """组装 JSON → 内联渲染 Markdown 评审视图。"""
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        p = tasks[0].payload

        # 1. 加载输入文件
        manifest = load_json(Path(p["assignment_batch"]))
        registry = load_json(Path(p["series_registry"]))
        source_manifest = load_json(root / "source_manifest.json")
        window_manifest = load_json(root / "window_manifest.json")
        episode_digests = load_jsonl(Path(p["episode_digests"]))
        event_cards = load_jsonl(Path(p["event_cards"]))

        # 2. 收集 Series Assignment 结果
        assignments: list[dict[str, Any]] = []
        for index, job in enumerate(manifest.get("jobs", [])):
            if not isinstance(job, dict) or job.get("task") != "series_assignment":
                continue
            output = job.get("output")
            if not isinstance(output, str):
                raise ValueError(f"assignment job {index} has no output")
            job_path = Path(output).expanduser().resolve()
            if not job_path.is_file():
                raise FileNotFoundError(f"missing Series Assignment result: {job_path}")
            assignments.append(load_json(job_path))
        if not assignments:
            raise ValueError("assignment manifest contains no Series Assignment jobs")

        # 3. 组装 Series Bible
        bible = assemble_bible(
            registry=registry,
            assignments=assignments,
            sources=source_manifest.get("sources", []),
            manifest_windows=window_manifest.get("windows", []),
            episode_digests=episode_digests,
            events=event_cards,
        )

        # 4. 写入 JSON
        bible_path = root / "series-bible.json"
        atomic_write_json(bible_path, bible)

        # 5. 内联渲染 Markdown 评审视图
        if not isinstance(bible, dict):
            raise RuntimeError("series-bible.json 不是 JSON 对象")
        md_text = _render_bible(bible)
        md_path = root / "series-bible.md"
        atomic_write_text(md_path, md_text)

        ref = bus.put("series_bible", {"path": str(bible_path)}, stage="series_bible")
        update_project_stage(root / "project.json", "series_bible", "completed",
                             outputs={"series_bible": str(bible_path)})
        return [ref]


# ── Markdown 渲染 (从 export_story_markdown.py render_bible 内联) ──


def _bullet(values: list[Any]) -> str:
    return "、".join(str(item) for item in values) if values else "无"


def _render_bible(value: dict[str, Any]) -> str:
    """渲染 Series Bible 为人类可读的 Markdown 评审视图。"""
    lines = ["# 全剧 Story Bible", ""]
    metadata = value.get("metadata") or {}
    if metadata:
        lines.extend([
            "> **元数据**（v1.3 审计栏，用于跨 run 溯源）",
            "> ",
            f"> - schema_version：{value.get('schema_version', 'unknown')}",
            f"> - pipeline_version：{metadata.get('pipeline_version', 'unknown')}",
            f"> - skill_version：{metadata.get('skill_version', 'unknown')}",
            f"> - generated_at：{metadata.get('generated_at', 'unknown')}",
            f"> - model_id：{metadata.get('model_id', 'unknown')}",
            f"> - seed：{metadata.get('seed', 'null')}",
            f"> - prompt_template_hash：`{metadata.get('prompt_template_hash', '')}`",
            f"> - input_manifest_hash：`{metadata.get('input_manifest_hash', '')}`",
            f"> - output_language：{metadata.get('output_language', 'unknown')}",
            f"> - determinism_class：{metadata.get('determinism_class', 'unknown')}",
            "",
        ])
    lines.extend([value.get("series_summary", ""), "", "## 主要人物", ""])

    characters = value.get("characters", [])
    character_by_id = {
        char["id"]: char for char in characters if isinstance(char, dict)
    }
    main_ids = value.get("main_characters") or [
        char["id"] for char in characters if isinstance(char, dict)
    ]
    importance = value.get("entity_importance") or {}
    for char_id in main_ids:
        character = character_by_id.get(char_id)
        if not character:
            continue
        stats = importance.get(char_id) or {}
        score_line = (
            f"- 重要性得分：{stats.get('score', 0):.1f} "
            f"（事件×{stats.get('event_ref_count', 0)}、"
            f"关系×{stats.get('relationship_ref_count', 0)}、"
            f"故事线×{stats.get('thread_ref_count', 0)}）"
        )
        lines.extend([
            f"### {character['canonical_name']}（`{character['id']}`）",
            "",
            f"- 身份：{character.get('identity') or '未明确'}",
            f"- 别名：{_bullet(character.get('aliases', []))}",
            f"- 目标：{_bullet(character.get('goals', []))}",
            f"- 证据事件：{_bullet(character.get('evidence_event_ids', []))}",
            score_line,
            "",
        ])

    secondary = [
        char for char in characters
        if isinstance(char, dict) and char["id"] not in set(main_ids)
    ]
    if secondary:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for char in secondary:
            grouped.setdefault(char.get("entity_type", "unknown"), []).append(char)
        type_label = {
            "individual": "个体（未入主要人物）",
            "group": "群体 / 组织",
            "creature": "非人角色",
            "unknown": "身份未消解",
        }
        for entity_type, group in grouped.items():
            lines.extend([f"### 次要角色 · {type_label.get(entity_type, entity_type)}", ""])
            for char in group:
                lines.append(
                    f"- `{char['id']}` {char['canonical_name']}"
                    + (f"（别名：{'/'.join(char.get('aliases', []))}）" if char.get("aliases") else "")
                )
            lines.append("")

    lines.extend(["## 人物关系", ""])
    for relationship in value.get("relationships", []):
        lines.extend([
            f"### `{relationship['id']}`",
            "",
            f"- 人物：{_bullet(relationship.get('character_ids', []))}",
            f"- 初始状态：{relationship.get('initial_state') or '未明确'}",
        ])
        for change in relationship.get("state_changes", []):
            lines.append(f"- `{change['event_id']}`：{change['state']}；{change.get('reason', '')}")
        lines.append("")

    lines.extend(["## 主要故事线", ""])
    beat_by_id = {
        item["id"]: item
        for item in value.get("thread_beats", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for thread in value.get("story_threads", []):
        lines.extend([
            f"### {thread['title']}（`{thread['id']}`）",
            "",
            thread["premise"],
            "",
            f"- 类型：{thread['thread_kind']}",
            f"- 状态：{thread['status']}",
            f"- 人物：{_bullet(thread.get('character_ids', []))}",
            f"- 覆盖集：{_bullet(thread.get('episode_ids', []))}",
            "",
            "| 集 | 阶段 | 重要性 | Thread Beat | 事件 | 前置 Beat |",
            "|---:|---|---|---|---|---|",
        ])
        for beat_id in thread.get("thread_beat_ids", []):
            beat = beat_by_id.get(beat_id)
            if not beat:
                continue
            lines.append(
                f"| {beat['episode']} | `{beat['phase']}` | "
                f"`{beat['importance']}` | {beat['summary']} | "
                f"{_bullet(beat.get('event_ids', []))} | "
                f"{_bullet(beat.get('requires_beat_ids', []))} |"
            )
        lines.append("")

    lines.extend(["## 未解问题", ""])
    for question in value.get("open_questions", []):
        lines.append(f"- `{question['id']}` [{question['status']}] {question['question']}")

    lines.extend(["", "## 覆盖统计", ""])
    coverage = value.get("coverage", {})
    ingestion = coverage.get("ingestion_coverage", {})
    narrative = coverage.get("narrative_coverage", {})
    lines.extend([
        "### 摄取覆盖", "",
        f"- 素材：{ingestion.get('source_count', 0)}",
        f"- 集数：{ingestion.get('episode_count', 0)}",
        f"- 视频窗：{ingestion.get('window_count', 0)}",
        f"- Episode Digest：{ingestion.get('episode_digest_count', 0)}",
        f"- 缺失集：{_bullet(ingestion.get('missing_episode_ids', []))}",
        "",
        "### 叙事覆盖", "",
        f"- 已分配集：{_bullet(narrative.get('covered_episode_ids', []))}",
        f"- 未分配集：{_bullet(narrative.get('unassigned_episode_ids', []))}",
        "- 明确排除集：" + _bullet([
            f"EP{item.get('episode')} {item.get('reason_type')}: {item.get('explanation')}"
            for item in narrative.get("excluded_episodes", [])
        ]),
    ])
    return "\n".join(lines).rstrip() + "\n"