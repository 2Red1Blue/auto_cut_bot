"""story_plans_materialize Stage — 将选中的 Story Plan 物化为可 QC 的候选。

输入: story_plans
输出: story_plans_materialized
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.contracts.plan_validation import validate as validate_story_plans
from autocut_core.contracts.span_validation import validate as validate_span_candidates
from autocut_core.io import (
    atomic_write_json, atomic_write_text, load_json, sha256_file,
    update_project_stage, utc_now,
)
from autocut_core.libs.editorial_plan import (
    current_plan_generation,
    expand_option_selection,
    materialize_plan,
    plan_filename,
    render_review,
    validate_option_selection,
)
from autocut_core.schema.compat import validate_schema, validate_task_response


class MaterializeStage(Stage):
    """Materialize — 将选中的 Story Plan 物化为可 QC 的候选。"""

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="story_plans_materialize",
            input_artifacts=["story_plans"],
            output_artifacts=["story_plans_materialized"],
            description="Story Plan 物化",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="materialize", payload={
            "plan_batch": self.resolve_artifact_path(bus, "story_plans", "story_plans"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        p = tasks[0].payload
        batch_path = Path(p["plan_batch"])
        batch = load_json(batch_path) if batch_path.is_file() else {}
        allow_partial = cfg.mode == "auto"

        if not batch.get("jobs"):
            print(f"[{utc_now()}] [story_plans_materialize] 当前 Plan 批次 "
                  "jobs=[]; 历史 Plans 保持非激活")

        # --- 从 main() 提取的核心逻辑 ---
        manifest_path = batch_path.resolve()
        span_report = validate_span_candidates(root)
        if not span_report["ok"]:
            raise ValueError(
                "Span Candidate validation failed: "
                + "; ".join(span_report["errors"][:30])
            )
        manifest = load_json(manifest_path)
        preflight_path = root / "story-plan-preflight.json"
        if not preflight_path.is_file():
            raise FileNotFoundError(preflight_path)
        preflight = load_json(preflight_path)
        generation_sha256 = current_plan_generation(root)
        if (
            preflight.get("plan_generation_sha256") != generation_sha256
            or manifest.get("plan_generation_sha256") != generation_sha256
        ):
            raise ValueError(
                "Story Plan batch/preflight generation is stale; rerun "
                "prepare_story_stages.py plans"
            )

        approval_path = root / "story-approval.json"
        portfolio_path = root / "story-portfolio.json"
        evidence_index_path = root / "story-evidence" / "index.json"
        span_index_path = root / "span-candidates" / "index.json"
        approval = load_json(approval_path)
        evidence_index = load_json(evidence_index_path)
        span_index = load_json(span_index_path)
        approved_entries = {
            item["story_id"]: item
            for item in approval["stories"]
            if item.get("decision") == "approved"
        }
        evidence_entries = {
            item["story_id"]: item for item in evidence_index["packets"]
        }
        bundle_entries = {
            item["story_id"]: item for item in span_index["bundles"]
        }
        jobs = [
            item
            for item in manifest.get("jobs", [])
            if isinstance(item, dict)
            and item.get("task") == "story_plan_selection"
        ]
        job_by_story: dict[str, dict] = {}
        context_by_story: dict[str, dict] = {}
        for job in jobs:
            context_path = Path(job["context_file"]).expanduser().resolve()
            context = load_json(context_path)
            story_id = context.get("story_id")
            if (
                context.get("input_fingerprints", {}).get(
                    "plan_generation_sha256"
                )
                != generation_sha256
            ):
                raise ValueError(
                    "Story Plan context generation is stale: "
                    f"{context_path}"
                )
            if not isinstance(story_id, str) or story_id in job_by_story:
                raise ValueError("Story Plan batch has missing or duplicate story identity")
            job_by_story[story_id] = job
            context_by_story[story_id] = context
        if set(job_by_story) - set(bundle_entries):
            raise ValueError(
                "Story Plan batch references Story IDs missing from Span "
                "Candidate Bundles"
            )
        missing_bundles = set(bundle_entries) - set(job_by_story)
        if missing_bundles and not allow_partial:
            raise ValueError(
                "Story Plan batch does not cover every Span Candidate Bundle "
                "exactly; use --allow-partial to materialize only the Stories "
                "present in the batch. Missing: " + ", ".join(sorted(missing_bundles))
            )
        bundle_entries_to_process = {
            story_id: entry
            for story_id, entry in bundle_entries.items()
            if story_id in job_by_story
        }

        output_dir = root / "story-plans"
        generation_dir = output_dir / "generations" / generation_sha256
        plans: list[dict] = []
        index_entries: list[dict] = []
        common_fingerprints = {
            "story_approval_sha256": sha256_file(approval_path),
            "portfolio_sha256": sha256_file(portfolio_path),
            "story_evidence_index_sha256": sha256_file(evidence_index_path),
            "span_candidate_index_sha256": sha256_file(span_index_path),
            "plan_generation_sha256": generation_sha256,
        }
        for story_id, bundle_entry in sorted(
            bundle_entries_to_process.items(),
            key=lambda item: int(item[1]["production_slot"]),
        ):
            job = job_by_story[story_id]
            output_path = Path(job["output"]).expanduser().resolve()
            if not output_path.is_file():
                raise FileNotFoundError(f"missing Story Plan selection result: {output_path}")
            selection = load_json(output_path)
            selection_errors = validate_task_response("story_plan_selection", selection)
            response_format_value = job.get("response_format")
            if isinstance(response_format_value, dict):
                response_schema = response_format_value.get(
                    "json_schema", {}
                ).get("schema")
                if isinstance(response_schema, dict):
                    selection_errors.extend(
                        validate_schema(selection, response_schema)
                    )
            legal_options = context_by_story[story_id].get("legal_option_contract")
            if not isinstance(legal_options, dict):
                selection_errors.append("Story Plan context has no legal_option_contract")
            else:
                selection_errors.extend(
                    validate_option_selection(selection, legal_options)
                )
            if selection_errors:
                raise ValueError(
                    f"invalid Story Plan selection {output_path}: "
                    + "; ".join(selection_errors[:40])
                )
            expanded_selection = expand_option_selection(selection, legal_options)
            approved = approved_entries.get(story_id)
            evidence_entry = evidence_entries.get(story_id)
            if approved is None or evidence_entry is None:
                raise ValueError(f"{story_id}: missing approved Story inputs")
            script_path = Path(approved["script_path"]).expanduser().resolve()
            packet_path = Path(evidence_entry["path"]).expanduser().resolve()
            bundle_path = Path(bundle_entry["path"]).expanduser().resolve()
            script_sha256 = sha256_file(script_path)
            packet_sha256 = sha256_file(packet_path)
            bundle_sha256 = sha256_file(bundle_path)
            if script_sha256 != approved.get("approved_script_sha256"):
                raise ValueError(f"approved Story Script is stale: {story_id}")
            if packet_sha256 != evidence_entry.get("packet_sha256"):
                raise ValueError(f"Story Evidence Packet is stale: {story_id}")
            if bundle_sha256 != bundle_entry.get("bundle_sha256"):
                raise ValueError(f"Span Candidate Bundle is stale: {story_id}")
            fingerprints = {
                **common_fingerprints,
                "story_script_sha256": script_sha256,
                "story_evidence_packet_sha256": packet_sha256,
                "span_candidate_bundle_sha256": bundle_sha256,
                "selection_result_sha256": sha256_file(output_path),
            }
            plan = materialize_plan(
                expanded_selection,
                script=load_json(script_path),
                bundle=load_json(bundle_path),
                evidence_packet=load_json(packet_path),
                fingerprints=fingerprints,
            )
            plan_path = generation_dir / plan_filename(story_id)
            atomic_write_json(plan_path, plan)
            plans.append(plan)
            index_entries.append(
                {
                    "story_id": story_id,
                    "title": plan["title"],
                    "production_slot": plan["production_slot"],
                    "status": plan["status"],
                    "path": str(plan_path),
                    "plan_sha256": sha256_file(plan_path),
                    "story_script_sha256": script_sha256,
                    "span_candidate_bundle_sha256": bundle_sha256,
                    "selection_result_sha256": fingerprints["selection_result_sha256"],
                    "estimated_duration_seconds": plan["estimated_duration_seconds"],
                    "block_count": len(plan["blocks"]),
                    "clip_count": sum(len(item["clips"]) for item in plan["blocks"]),
                }
            )
        ready_count = sum(item["status"] == "ready_for_video_qc" for item in plans)
        if plans and ready_count == len(plans):
            index_status = "ready_for_video_qc"
        elif ready_count:
            index_status = "partially_ready"
        else:
            index_status = "blocked"
        index = {
            "schema_version": "1.0",
            "method": "legal-option-selection-local-materialization-v2",
            "status": index_status,
            "story_approval_sha256": common_fingerprints["story_approval_sha256"],
            "span_candidate_index_sha256": common_fingerprints["span_candidate_index_sha256"],
            "plan_generation_sha256": generation_sha256,
            "plan_count": len(plans),
            "ready_plan_count": ready_count,
            "blocked_plan_count": len(plans) - ready_count,
            "plans": index_entries,
        }
        index_errors = validate_task_response("story_plan_index", index)
        if index_errors:
            raise ValueError(
                "invalid Story Plan Index: " + "; ".join(index_errors[:40])
            )
        index_path = output_dir / "index.json"
        atomic_write_json(index_path, index)
        review_path = root / "story-plan-review.md"
        atomic_write_text(review_path, render_review(plans, index_status))

        update_project_stage(
            root / "project.json",
            "story_plans",
            index_status,
            inputs={
                "story_plan_batch": str(manifest_path),
                "span_candidate_index": str(span_index_path),
            },
            outputs={
                "story_plan_index": str(index_path),
                "story_plan_review": str(review_path),
            },
            note=(
                f"ready={ready_count}/{len(plans)}; "
                "video QC and transitions not generated"
            ),
        )

        validate_story_plans(root, allow_partial=allow_partial)

        index_path_out = (
            root / "story-plan-candidates" / "index.json"
            if batch.get("candidate_arena") is True
            else root / "story-plans" / "index.json"
        )
        ref = bus.put("story_plans_materialized", {"path": str(index_path_out)},
                      stage="story_plans_materialize")
        update_project_stage(root / "project.json", "story_plans_materialize", "completed",
                             outputs={"story_plans_materialized": str(index_path_out)})
        return [ref]