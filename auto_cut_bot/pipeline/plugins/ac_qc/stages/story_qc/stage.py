"""story_qc Stage — 物化 Plan 的视频质检 (语义 + 规则)。

输入: story_plans_materialized
输出: story_qc (story-qc/index.json)
"""

from __future__ import annotations

import shutil
from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.contracts.qc_validation import validate as validate_story_qc
from autocut_core.contracts.plan_validation import validate as validate_story_plans
from autocut_core.io import (
    atomic_write_json, load_json, sha256_file, update_project_stage,
)
from autocut_core.libs.qc_admission import ACCEPTED, validate_admission
from autocut_core.libs.qc_report import (
    assemble,
    audio_guard_default,
)
from autocut_core.libs.prepare_story_qc import (
    prepare_audio_boundary_with_repair,
    prepare_story,
    source_locators,
    probe_media,
)
from autocut_core.schema.compat import validate_task_response
from autocut_core.semantic.batch_orchestrator import run_batch


class StoryQCStage(Stage):
    """Story QC — 物化 Plan 的视频质检 (语义 + 规则)。"""

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="story_qc",
            input_artifacts=["story_plans_materialized"],
            output_artifacts=["story_qc"],
            description="Story 视频 QC 质检",
            db_reads=["boundaries", "episodes"],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="qc", payload={
            "plans_index": self.resolve_artifact_path(
                bus, "story_plans_materialize", "story_plans_materialized"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        allow_partial = cfg.mode == "auto"
        backend = cfg.backend

        # ── Phase 1: prepare_story_qc ──────────────────────────────────
        base_plan_index_path = root / "story-plans" / "index.json"
        plan_report = validate_story_plans(root, allow_partial=allow_partial)
        if not plan_report["ok"]:
            raise ValueError(
                "Story Plans are invalid: "
                + "; ".join(plan_report["errors"][:30])
            )
        plan_index_path = base_plan_index_path
        plan_index = load_json(plan_index_path)

        # Audio boundary
        audio_boundary_python = (
            cfg.audio_boundary_python.expanduser().resolve()
            if cfg.audio_boundary_python
            else root / ".venv-audio-boundary" / "bin" / "python"
        )
        audio_guard_script = audio_guard_default()
        cache_dir = root / ".audio-boundary-cache"
        local_audio_source_manifest = root / "local-source-manifest.json"
        local_audio_source_manifest = (
            local_audio_source_manifest
            if local_audio_source_manifest.is_file()
            else None
        )
        blocked_entries = [
            item
            for item in plan_index.get("plans", [])
            if isinstance(item, dict) and item.get("status") == "blocked"
        ]

        (
            plan_index_path,
            audio_boundary,
            boundary_repair,
        ) = prepare_audio_boundary_with_repair(
            root,
            base_plan_index_path=base_plan_index_path,
            local_source_manifest_path=local_audio_source_manifest,
            audio_python=audio_boundary_python,
            audio_guard_script=audio_guard_script,
            cache_dir=cache_dir,
            device="cpu",
            workers=3,
            force=False,
            auto_repair=True,
            max_repair_rounds=3,
            max_adjustment_seconds=30.0,
            include_blocked=bool(blocked_entries),
        )
        plan_index = load_json(plan_index_path)
        plan_index_sha256 = sha256_file(plan_index_path)

        # Admission
        approval_path = root / "story-approval.json"
        source_manifest_path = root / "source_manifest.json"
        approval = load_json(approval_path)
        source_manifest = load_json(source_manifest_path)
        admission_path = root / "story-plan-qc-admission.json"
        admission_entries: dict[str, dict] = {}
        admission_sha256: str | None = None
        if blocked_entries:
            if not admission_path.is_file():
                raise ValueError(
                    "blocked Story Plans require story-plan-qc-admission.json"
                )
            _, admission_entries, admission_errors = validate_admission(
                root, admission_path,
            )
            if admission_errors:
                raise ValueError(
                    "invalid Story Plan QC admission: "
                    + "; ".join(admission_errors[:30])
                )
            unaccepted = [
                item["story_id"]
                for item in blocked_entries
                if admission_entries.get(item["story_id"], {}).get("decision")
                != ACCEPTED
            ]
            if unaccepted:
                raise ValueError(
                    "blocked Story Plans lack human QC admission: "
                    + ", ".join(sorted(unaccepted))
                )
            admission_sha256 = sha256_file(admission_path)
        elif admission_path.is_file():
            _, admission_entries, admission_errors = validate_admission(
                root, admission_path,
            )
            if admission_errors:
                raise ValueError(
                    "invalid Story Plan QC admission: "
                    + "; ".join(admission_errors[:30])
                )
            admission_sha256 = sha256_file(admission_path)

        approval_entries = {
            item["story_id"]: item
            for item in approval.get("stories", [])
            if isinstance(item, dict) and item.get("decision") == "approved"
        }
        required_source_ids: set[str] = set()
        for entry in plan_index.get("plans", []):
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            plan = load_json(Path(entry["path"]).expanduser().resolve())
            required_source_ids.update(
                clip["source_id"]
                for block in plan.get("blocks", [])
                if isinstance(block, dict)
                for clip in block.get("clips", [])
                if isinstance(clip, dict) and isinstance(clip.get("source_id"), str)
            )
        if not required_source_ids:
            raise ValueError("Story Plans do not select any Sources")
        locators = source_locators(
            root,
            source_manifest,
            required_source_ids=required_source_ids,
            local_source_manifest=(
                load_json(local_audio_source_manifest)
                if local_audio_source_manifest is not None
                else None
            ),
        )
        source_infos = {
            source_id: probe_media(
                locator,
                ffprobe="ffprobe",
                label=source_id,
            )
            for source_id, locator in locators.items()
        }
        plan_index_sha256 = sha256_file(plan_index_path)
        source_manifest_sha256 = sha256_file(source_manifest_path)

        manifests: list[str] = []
        jobs: list[dict] = []
        for entry in sorted(
            plan_index["plans"], key=lambda item: item["production_slot"]
        ):
            approval_entry = approval_entries.get(entry["story_id"])
            if approval_entry is None:
                raise ValueError(
                    f"{entry['story_id']}: Story Plan is not currently approved"
                )
            manifest_path, story_jobs = prepare_story(
                job_root=root,
                plan_index_sha256=plan_index_sha256,
                plan_entry=entry,
                approval_entry=approval_entry,
                admission_entry=admission_entries.get(entry["story_id"]),
                admission_path=(
                    admission_path if admission_sha256 is not None else None
                ),
                admission_sha256=admission_sha256,
                source_manifest=source_manifest,
                source_manifest_sha256=source_manifest_sha256,
                locators=locators,
                source_infos=source_infos,
                ffmpeg="ffmpeg",
                ffprobe="ffprobe",
                width=360,
                height=640,
                fps=25,
                video_bitrate_kbps=180,
                audio_bitrate_kbps=48,
                review_width=540,
                review_height=960,
                review_video_bitrate_kbps=900,
                review_audio_bitrate_kbps=96,
                junction_handle_seconds=4.0,
                force=False,
            )
            manifests.append(str(manifest_path))
            jobs.extend(story_jobs)

        if not jobs:
            raise ValueError("Story QC has no video review jobs")
        batch = {
            "schema_version": "1.0",
            "backend": backend,
            "cache_dir": str((root / ".story-cache").resolve()),
            "stage_version": "story-qc-v4-dynamic-schema",
            "story_plan_index_path": str(plan_index_path),
            "story_plan_index_sha256": plan_index_sha256,
            "base_story_plan_index_path": str(base_plan_index_path),
            "base_story_plan_index_sha256": sha256_file(base_plan_index_path),
            "source_manifest_sha256": source_manifest_sha256,
            "audio_boundary": audio_boundary,
            "boundary_repair": boundary_repair,
            "story_plan_qc_admission_path": (
                str(admission_path) if admission_sha256 is not None else None
            ),
            "story_plan_qc_admission_sha256": admission_sha256,
            "proxy_manifests": manifests,
            "jobs": jobs,
        }
        batch_path = root / "story-qc-batch.json"
        atomic_write_json(batch_path, batch)

        # ── Phase 2: run semantic batch ────────────────────────────────
        run_batch(
            batch_path,
            backend=backend,
            workers=cfg.workers,
            requests_per_minute=cfg.requests_per_minute,
            semantic_retries=cfg.semantic_retries,
        )

        # ── Phase 3: assemble_story_qc ─────────────────────────────────
        index, __ = assemble(root)
        validate_story_qc(root)

        index_path = root / "story-qc" / "index.json"
        ref = bus.put("story_qc", {"path": str(index_path)}, stage="story_qc")
        update_project_stage(root / "project.json", "story_qc", "completed",
                             outputs={"story_qc": str(index_path)})
        return [ref]