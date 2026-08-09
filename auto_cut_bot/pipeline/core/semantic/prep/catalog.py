"""autocut_core.semantic.prep.catalog — Story Catalog 准备阶段。

从 prepare_story_stages.py 提取的 prepare_catalog 函数。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


from autocut_core.semantic.prep.utils import batch_payload, write_context
from autocut_core.io import (
    atomic_write_json,
    load_json,
    load_jsonl,
    sha256_file,
    update_project_stage,
)

from autocut_core.semantic.granularity import (
    BROAD,
    build_broad_catalog_schema,
    compile_broad_subarc_options,
    require_broad_story_granularity,
)
from autocut_core.schema.compat import SERIES_BIBLE_SCHEMA


def prepare_catalog(args: argparse.Namespace) -> Path:
    """准备 Story Catalog 批处理 manifest。

    原位置: prepare_story_stages.prepare_catalog (L874, 165L)
    """
    job_root = args.job_root.resolve()
    bible = load_json(args.series_bible)
    expected_bible_schema = SERIES_BIBLE_SCHEMA["properties"][
        "schema_version"
    ]["const"]
    if bible.get("schema_version") != expected_bible_schema:
        raise ValueError(
            "Story Catalog requires current Series Bible schema "
            f"{expected_bible_schema}; got {bible.get('schema_version')!r}. "
            "Rerun from Series Registry so typed thread_kind is rebuilt."
        )
    events = load_jsonl(args.event_cards)
    candidates = load_json(args.candidate_catalog)
    context = {
        "schema_version": "1.2",
        "story_inventory_policy": "discover_all_evidence_backed_subarcs",
        "duration_contract": {
            "minimum_seconds": 0,
            "preferred_minimum_seconds": 0,
            "soft_target_seconds": 0,
            "preferred_target_range_seconds": [0, 1200],
            "maximum_seconds": 1200,
            "padding_forbidden": True,
            "soft_target_semantics": (
                "撤除最短时长；只保留 1200s 硬上限。"
                "禁止靠重复、整集或无功能片段填长。"
                "成片时长不足 300 秒由 render 阶段的 filler tail 兜底。"
            ),
        },
        "cross_story_source_reuse_allowed": True,
        "series_bible": bible,
        "events": [
            {
                key: event.get(key)
                for key in (
                    "id",
                    "episode",
                    "source_id",
                    "source_ranges",
                    "summary",
                    "function",
                    "character_names",
                    "cause",
                    "effect",
                    "open_question",
                    "candidate_ids",
                )
            }
            for event in events
        ],
        "candidate_catalog": candidates,
    }
    jobs: list[dict[str, Any]] = []
    option_catalog_path: Path | None = None
    option_catalog = compile_broad_subarc_options(bible, events, candidates)
    option_catalog_path = job_root / "story-subarc-options.json"
    atomic_write_json(option_catalog_path, option_catalog)
    option_catalog_sha256 = sha256_file(option_catalog_path)
    option_by_id = {
        item["subarc_option_id"]: item
        for item in option_catalog["options"]
    }
    context_dir = job_root / "intermediate" / "story-catalog-contexts"
    output_dir = job_root / "story-catalog-results"
    for index, option_id in enumerate(
        option_catalog["recommended_option_ids"], start=1
    ):
        option = option_by_id[option_id]
        story_id = f"story-broad-{index:03d}"
        scoped_options = {
            **option_catalog,
            "options": [option],
            "recommended_option_ids": [option_id],
            "required_thread_beat_ids": option[
                "required_thread_beat_ids"
            ],
            "non_coda_thread_beat_ids": option[
                "non_coda_thread_beat_ids"
            ],
            "all_thread_beat_ids": option["source_thread_beat_ids"],
        }
        scoped_context = {
            **context,
            "story_inventory_policy": "coverage_first_broad_subarc_options",
            "story_granularity": BROAD,
            "subarc_option_catalog_sha256": option_catalog_sha256,
            "subarc_options": [option],
            "broad_validation_scope": {
                "mode": "single_option",
                "expected_option_ids": [option_id],
                "required_thread_beat_ids": option[
                    "required_thread_beat_ids"
                ],
                "non_coda_thread_beat_ids": option[
                    "non_coda_thread_beat_ids"
                ],
            },
            "broad_story_contract": {
                **option_catalog["coverage_contract"],
                "recommended_option_ids": [option_id],
                "option_selector": "local_coverage_compiler",
                "model_outputs_schema_locked_option_id": True,
                "exact_story_count": 1,
                "option_identity_fields_are_schema_locked": True,
                "estimated_source_seconds_is_computed_locally": True,
            },
        }
        context_path = context_dir / f"{story_id}.json"
        output_path = output_dir / f"{story_id}.json"
        write_context(context_path, scoped_context, args.max_context_chars)
        jobs.append(
            {
                "id": f"story-catalog-{story_id}",
                "task": "story_catalog",
                "stage_version": "story-first-story-catalog-v5-typed-coda",
                "subarc_option_id": option_id,
                "story_id": story_id,
                "context_file": str(context_path.resolve()),
                "output": str(output_path.resolve()),
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": (
                            "story_first_story_catalog_v5_typed_coda_"
                            f"{index:03d}"
                        ),
                        "strict": True,
                        "schema": build_broad_catalog_schema(
                            bible,
                            scoped_options,
                            option_catalog_sha256=option_catalog_sha256,
                            story_id_by_option={option_id: story_id},
                            exact_story_count=1,
                        ),
                    },
                },
            }
        )
    manifest_path = job_root / "story-catalog-batch.json"
    atomic_write_json(
        manifest_path,
        batch_payload(job_root, args.backend, jobs),
    )
    stage_outputs = {"batch_manifest": str(manifest_path)}
    if option_catalog_path is not None:
        stage_outputs["subarc_options"] = str(option_catalog_path)
    update_project_stage(
        job_root / "project.json",
        "story_catalog_job",
        "prepared",
        outputs=stage_outputs,
    )
    project_path = job_root / "project.json"
    project = load_json(project_path)
    project.pop("output_request", None)
    if not isinstance(project.get("fulfillment"), dict):
        project["fulfillment"] = {
            "proposal_count": 0,
            "primary_script_count": 0,
            "selected_story_count": 0,
            "status": "not_started",
        }
    atomic_write_json(project_path, project)
    return manifest_path