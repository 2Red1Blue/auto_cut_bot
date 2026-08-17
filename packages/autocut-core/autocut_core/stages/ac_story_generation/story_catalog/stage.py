"""story_catalog Stage — 从 Series Bible 与事件卡片中发现独立故事子弧。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core import ArtifactBus, Artifact, Stage, StageContract, Task
from autocut_core.io import (
    atomic_write_json, load_json, sha256_file, update_project_stage,
)
from autocut_core.semantic.prep.catalog import prepare_catalog
from autocut_core.stages.ports import LLMPort, get_llm_port

_BROAD = "broad"
_CATALOG_TASK = "story_catalog"


class CatalogStage(Stage):
    """Broad Story Catalog 发现子故事。

    输入: series_bible, event_cards, highlight_hook_catalog
    输出: story_catalog
    """

    def __init__(self, config: "PipelineConfig", llm_port: LLMPort | None = None) -> None:
        super().__init__(config)
        self._llm_port: LLMPort | None = llm_port

    @property
    def llm_port(self) -> LLMPort:
        if self._llm_port is None:
            self._llm_port = get_llm_port()
        return self._llm_port

    @property
    def contract(self) -> StageContract:
        return StageContract(stage_name="story_catalog",
            input_artifacts=["series_bible", "event_cards"],
            output_artifacts=["story_catalog"],
            description="发现独立故事子弧",
            db_reads=["books"],
            db_writes=[])

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="semantic_batch", payload={
            "bible": self.resolve_artifact_path(bus, "series_bible", "series_bible"),
            "event_cards": self.resolve_artifact_path(bus, "event_cards", "event_cards"),
            "catalog": self.resolve_artifact_path(bus, "event_cards", "highlight_hook_catalog"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        root: Path = cfg.job_root  # type: ignore
        p = tasks[0].payload

        # 1. 准备批次
        ns = argparse.Namespace()
        ns.job_root = root
        ns.backend = cfg.backend
        ns.max_context_chars = 600000
        ns.series_bible = Path(p["bible"])
        ns.event_cards = Path(p["event_cards"])
        ns.candidate_catalog = Path(p["catalog"])
        batch_path = prepare_catalog(ns)

        # 2. LLM 推理
        self.llm_port.run_batch(
            batch_path,
            backend=cfg.backend,
            workers=cfg.workers,
            requests_per_minute=cfg.requests_per_minute,
            semantic_retries=cfg.semantic_retries,
        )

        # 3. 内联组装 (不再调用 assemble_broad_story_catalog.py)
        option_path = root / "story-subarc-options.json"
        output_path = root / "story-catalog.json"
        _assemble_catalog(batch_path, option_path, output_path)

        ref = bus.put("story_catalog", {"path": str(output_path)}, stage="story_catalog")
        update_project_stage(root / "project.json", "story_catalog", "completed")
        return [ref]


def _inject_deterministic_fields(
    story: dict[str, Any],
    option: dict[str, Any],
    story_id: str,
    bible: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Overwrite all deterministic fields in a story from the option.

    The LLM only produces narrative (FREE) fields; this function
    injects CONST/ENUM fields that are fully determined by upstream code.
    """
    from autocut_core.semantic.granularity import (
        compute_option_character_ids,
        compute_option_relationship_ids,
    )

    story["story_id"] = story_id
    story["subarc_option_id"] = option["subarc_option_id"]
    story["story_thread_ids"] = list(option.get("story_thread_ids", []))
    story["source_thread_beat_ids"] = list(option.get("source_thread_beat_ids", []))
    story["subarc_start_beat_id"] = option.get("subarc_start_beat_id", "")
    story["subarc_end_beat_id"] = option.get("subarc_end_beat_id", "")
    story["required_bridge_beat_ids"] = list(option.get("required_bridge_beat_ids", []))
    story["evidence_event_ids"] = list(option.get("evidence_event_ids", []))
    story["estimated_source_seconds"] = option.get("estimated_source_seconds", 0)
    story["duration_feasibility"] = option.get("duration_feasibility", "viable")
    story["required_fact_ids"] = list(option.get("required_fact_ids", []))

    # Compute character/relationship IDs from bible
    if bible:
        thread_by_id = {
            t["id"]: t
            for t in (bible.get("story_threads") or [])
            if isinstance(t, dict) and isinstance(t.get("id"), str)
        }
        relationships = [
            r for r in (bible.get("relationships") or [])
            if isinstance(r, dict) and isinstance(r.get("id"), str)
        ]
        char_ids = compute_option_character_ids(option, thread_by_id)
        rel_ids = compute_option_relationship_ids(char_ids, relationships)
        story["character_ids"] = char_ids or ["char-unknown"]
        story["relationship_ids"] = rel_ids
    else:
        if not story.get("character_ids"):
            story["character_ids"] = ["char-unknown"]
        story.setdefault("relationship_ids", [])

    return story


def _assemble_catalog(
    manifest_path: Path, option_catalog_path: Path, output_path: Path
) -> dict[str, Any]:
    """从语义批处理 manifest 组装 Broad Story Catalog。

    LLM shards only contain narrative (FREE) fields.  This function
    injects all deterministic (CONST/ENUM) fields from the option
    catalog, then runs validate_broad_catalog as a final sanity check.
    """
    from autocut_core.semantic.granularity import validate_broad_catalog

    manifest = load_json(manifest_path)
    option_catalog = load_json(option_catalog_path)
    option_catalog_sha = sha256_file(option_catalog_path)

    if option_catalog.get("story_granularity") != _BROAD:
        raise ValueError("option catalog is not story_granularity=broad")

    expected_ids = list(option_catalog.get("recommended_option_ids", []) or [])
    if not expected_ids:
        raise ValueError("Broad option catalog has no recommended_option_ids")

    option_by_id = {
        item["subarc_option_id"]: item
        for item in option_catalog.get("options", []) or []
        if isinstance(item, dict) and isinstance(item.get("subarc_option_id"), str)
    }

    # Load series_bible for character/relationship computation
    bible = None
    # Try to find bible path from manifest jobs' context files
    for job in manifest.get("jobs", []) or []:
        ctx_file = job.get("context_file") if isinstance(job, dict) else None
        if isinstance(ctx_file, str):
            try:
                ctx = load_json(Path(ctx_file))
                bible = ctx.get("series_bible")
                if isinstance(bible, dict):
                    break
            except Exception:
                pass

    story_by_option: dict[str, dict[str, Any]] = {}
    for index, job in enumerate(manifest.get("jobs", []) or []):
        if not isinstance(job, dict) or job.get("task") != _CATALOG_TASK:
            continue
        option_id = job.get("subarc_option_id")
        if option_id not in expected_ids:
            raise ValueError(f"manifest contains unexpected Broad option job: {option_id!r}")
        output_value = job.get("output")
        if not isinstance(output_value, str):
            raise ValueError(f"{job.get('id')}: output path is missing")
        shard_path = Path(output_value).expanduser().resolve()
        if not shard_path.is_file():
            raise FileNotFoundError(f"Broad Catalog shard is missing: {shard_path}")
        shard = load_json(shard_path)
        stories = shard.get("stories", [])
        if len(stories) != 1:
            raise ValueError(f"{shard_path.name} must contain exactly one Story")
        story = stories[0]

        # Derive story_id from job or index
        story_id = job.get("story_id") or f"story-broad-{index + 1:03d}"

        # Inject all deterministic fields from option
        option = option_by_id.get(option_id)
        if option is None:
            raise ValueError(f"Option {option_id!r} not found in option catalog")
        _inject_deterministic_fields(story, option, story_id, bible=bible)

        if option_id in story_by_option:
            raise ValueError(f"duplicate Broad Catalog shard for {option_id}")
        story_by_option[option_id] = story

    missing = [oid for oid in expected_ids if oid not in story_by_option]
    extra = sorted(set(story_by_option) - set(expected_ids))
    if missing or extra:
        raise ValueError(f"Broad Catalog shard set mismatch: missing={missing}, extra={extra}")

    catalog = {
        "schema_version": "1.2",
        "story_granularity": _BROAD,
        "subarc_option_catalog_sha256": option_catalog_sha,
        "stories": [story_by_option[oid] for oid in expected_ids],
    }

    story_ids = [
        s.get("story_id") for s in catalog["stories"] if isinstance(s, dict)
    ]
    if len(set(story_ids)) != len(story_ids):
        raise ValueError("Broad Catalog contains duplicate story_id")

    # Final validation — all CONST fields were injected by code, so this
    # should always pass.  Catches bugs in _inject_deterministic_fields.
    errors = validate_broad_catalog(
        catalog, option_catalog, option_catalog_sha256=option_catalog_sha,
    )
    if errors:
        raise ValueError(
            f"Assembled catalog failed validate_broad_catalog: {'; '.join(errors[:5])}"
        )

    atomic_write_json(output_path, catalog)
    return catalog
