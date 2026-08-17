"""autocut_core.semantic.prep.assignments — Series Assignment 批处理准备。

从 prepare_story_stages.py 提取 Assignments 相关函数。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from autocut_core.semantic.prep.chapters import _compact_event
from autocut_core.semantic.prep.utils import batch_payload, write_context


# ── public API ───────────────────────────────────────────────────────

def prepare_assignments(args: argparse.Namespace) -> Path:
    """准备 Series Assignment 语义批处理 manifest。

    原位置: prepare_story_stages.prepare_assignments (L689, 184L)
    """
    from autocut_core.semantic.registry_admission import load_and_validate_registry_admission
    from autocut_core.semantic.registry_contract import validate_series_registry_contract
    from autocut_core.io import atomic_write_json, json_sha256, load_jsonl, update_project_stage
    from autocut_core.schema.compat import build_series_assignment_schema, response_format, validate_task_response

    job_root = args.job_root.resolve()
    registry, registry_admission, registry_quarantine = (
        load_and_validate_registry_admission(
            args.series_registry,
            admission_path=args.registry_admission,
            quarantine_path=args.registry_quarantine,
        )
    )
    episodes = load_jsonl(args.episode_digests)
    chapters = load_jsonl(args.chapter_digests)
    events = load_jsonl(args.event_cards)
    schema_errors = validate_task_response("series_registry", registry)
    if schema_errors:
        raise ValueError("invalid Series Registry: " + "; ".join(schema_errors[:30]))
    registry_contract = validate_series_registry_contract(
        registry,
        known_event_ids={
            item.get("id")
            for item in events
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        },
        event_index=events,
    )
    if not registry_contract.ok:
        raise ValueError(
            "invalid Series Registry contract: "
            + "; ".join(registry_contract.errors[:30])
        )
    episode_by_id = {
        int(item["episode"]): item
        for item in episodes
        if isinstance(item, dict) and isinstance(item.get("episode"), int)
    }
    jobs = []
    context_dir = job_root / "intermediate" / "series-assignment-contexts"
    output_dir = job_root / "series-assignment-results"
    for chapter in sorted(
        chapters,
        key=lambda item: (item.get("episodes") or [10**9])[0],
    ):
        chapter_id = chapter.get("chapter_id")
        chapter_episodes = chapter.get("episodes", [])
        if not isinstance(chapter_id, str) or not chapter_id:
            raise ValueError("Chapter Digest is missing chapter_id")
        missing = sorted(set(chapter_episodes) - set(episode_by_id))
        if missing:
            raise ValueError(f"{chapter_id} is missing Episode Digests: {missing}")
        chapter_digests = [episode_by_id[item] for item in chapter_episodes]
        event_ids = {
            event_id
            for digest in chapter_digests
            for event_id in digest.get("event_ids", [])
            if isinstance(event_id, str)
        }
        event_ids.update(
            event_id
            for event_id in chapter.get("event_ids", [])
            if isinstance(event_id, str)
        )
        context = {
            "schema_version": "1.0",
            "chapter_id": chapter_id,
            "episodes": chapter_episodes,
            "series_registry": registry,
            "series_registry_admission": {
                "status": registry_admission.get("status"),
                "policy_version": registry_admission.get("policy_version"),
                "core_registry_sha256": registry_admission.get(
                    "core_registry_sha256"
                ),
                "quarantine_sha256": registry_admission.get(
                    "quarantine_sha256"
                ),
                "blocked_story_thread_ids": registry_admission.get(
                    "blocked_story_thread_ids", []
                ),
                "quarantined_character_ids": registry_quarantine.get(
                    "quarantined_character_ids", []
                ),
                "quarantined_event_ids": registry_quarantine.get(
                    "quarantined_event_ids", []
                ),
            },
            "chapter_digest": chapter,
            "episode_digests": chapter_digests,
            "event_index": [
                _compact_event(event)
                for event in events
                if event.get("id") in event_ids
            ],
            "assignment_contract": {
                "use_registry_thread_ids_only": True,
                "every_episode_must_be_assigned_or_typed_excluded": True,
                "event_episode_binding_is_strict": True,
                "excluded_episodes_are_whole_episode_only": True,
                "unused_events_require_no_accounting": True,
                "assigned_and_excluded_must_be_disjoint": True,
                "unassigned_episodes_are_errors_not_auto_exclusions": True,
                "fully_quarantined_episodes_are_locally_excluded": True,
                "quarantined_ids_are_forbidden_in_formal_outputs": True,
                "typed_coda_uses_only_terminal_phases": True,
                "typed_coda_requires_a_coda_phase_globally": True,
                "coverage_is_computed_locally": True,
            },
        }
        context_path = context_dir / f"{chapter_id}.json"
        output_path = output_dir / f"{chapter_id}.json"
        write_context(context_path, context, args.max_context_chars)
        assignment_schema = build_series_assignment_schema(
            chapter_id=chapter_id,
            episodes=chapter_episodes,
            thread_ids=[
                item["id"]
                for item in registry.get("story_threads", [])
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
            ],
            event_ids=[
                item["id"]
                for item in context["event_index"]
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
            ],
            quarantined_event_ids=[
                item
                for item in registry_quarantine.get(
                    "quarantined_event_ids", []
                )
                if isinstance(item, str)
            ],
        )
        jobs.append(
            {
                "id": f"series-assignment-{chapter_id}",
                "task": "series_assignment",
                # v6 consumes Registry thread_kind and rejects non-terminal
                # phases for typed coda Threads before persistence.
                "stage_version": "story-first-series-assignment-v6-typed-coda",
                "context_file": str(context_path.resolve()),
                "output": str(output_path.resolve()),
                "response_format": response_format(
                    "series_assignment",
                    schema_override=assignment_schema,
                    revision_override="v5_typed_coda",
                ),
            }
        )
    if not jobs:
        raise ValueError("no Chapter Digest is available for Series Assignment")
    manifest_path = job_root / "series-assignment-batch.json"
    atomic_write_json(manifest_path, batch_payload(job_root, args.backend, jobs))
    update_project_stage(
        job_root / "project.json",
        "series_assignment_jobs",
        "prepared",
        inputs={
            "series_registry": str(args.series_registry.resolve()),
            "series_registry_sha256": json_sha256(registry),
            "series_registry_admission": str(
                (
                    args.registry_admission
                    or args.series_registry.parent
                    / "series-registry-admission.json"
                ).resolve()
            ),
            "series_registry_admission_sha256": json_sha256(
                registry_admission
            ),
            "series_registry_quarantine": str(
                (
                    args.registry_quarantine
                    or args.series_registry.parent
                    / "series-registry-quarantine.json"
                ).resolve()
            ),
            "series_registry_quarantine_sha256": json_sha256(
                registry_quarantine
            ),
        },
        outputs={"batch_manifest": str(manifest_path)},
    )
    return manifest_path